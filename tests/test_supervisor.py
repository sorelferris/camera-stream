import json
import socket
import time

import zmq

from camera_stream.config import ServiceConfig
from camera_stream.supervisor import Supervisor


class StuckWorkerProcess:
    def __init__(self) -> None:
        self.terminated = False

    def is_alive(self) -> bool:
        return not self.terminated

    def join(self, timeout: float | None = None) -> None:
        return None

    def terminate(self) -> None:
        self.terminated = True


class ExitedWorkerProcess:
    def __init__(self) -> None:
        self.joined = False

    def is_alive(self) -> bool:
        return False

    def join(self, timeout: float | None = None) -> None:
        self.joined = True


def service_config() -> ServiceConfig:
    return ServiceConfig.model_validate(
        {
            "endpoints": {
                "stream_pub": "tcp://127.0.0.1:*",
            },
            "cameras": [
                {
                    "name": "cam",
                    "driver": "opencv",
                    "device": {"path": "/dev/video0"},
                    "profile": {"width": 640, "height": 480, "fps": 30},
                    "encoding": {"codec": "jpeg", "jpeg_quality": 85},
                }
            ],
        }
    )


def idle_service_config() -> ServiceConfig:
    document = service_config().model_dump(mode="json")
    document["idle_policy"] = {"enabled": True, "sleep_after_s": 1}
    return ServiceConfig.model_validate(document)


def test_first_capture_updates_status_without_waiting_for_heartbeat() -> None:
    supervisor = Supervisor(service_config())
    context = zmq.Context()
    worker = context.socket(zmq.DEALER)
    worker.connect(supervisor.control_endpoint)
    captured_monotonic_ns = time.monotonic_ns()
    captured_utc_ns = time.time_ns()
    try:
        worker.send_json(
            {
                "type": "capture",
                "camera": "cam",
                "captured_monotonic_ns": captured_monotonic_ns,
                "captured_utc_ns": captured_utc_ns,
            }
        )
        assert supervisor.control_router.poll(1000) == zmq.POLLIN
        supervisor._handle_control()
        status = supervisor.records["cam"].status
        assert status["last_capture_monotonic_ns"] == captured_monotonic_ns
        assert status["last_capture_utc_ns"] == captured_utc_ns
        assert status["state"] == "ONLINE"
    finally:
        worker.close(0)
        context.term()
        supervisor.shutdown()


def test_monitor_restarts_a_worker_stuck_offline(monkeypatch) -> None:
    supervisor = Supervisor(service_config())
    process = StuckWorkerProcess()
    record = supervisor.records["cam"]
    record.process = process
    record.status.update(
        {
            "state": "OFFLINE",
            "state_since_monotonic_ns": time.monotonic_ns() - 11_000_000_000,
            "last_heartbeat_ns": time.monotonic_ns(),
        }
    )
    restarts = []
    monkeypatch.setattr(supervisor, "_start_worker", restarts.append)
    try:
        supervisor._monitor_workers()
        assert process.terminated
        assert restarts == [record]
    finally:
        supervisor.shutdown()


def test_status_snapshot_includes_connected_pub_clients() -> None:
    supervisor = Supervisor(service_config())
    supervisor.clients[17] = {
        "ip": "192.168.5.21",
        "port": 54321,
        "fd": 17,
        "endpoint": "tcp://0.0.0.0:5555",
        "connected_monotonic_ns": time.monotonic_ns() - 2_000_000_000,
    }
    supervisor._record_published_bytes(time.monotonic_ns(), 125_000)
    try:
        snapshot = supervisor.status_snapshot()
        clients = snapshot["clients"]
        assert clients[0]["ip"] == "192.168.5.21"
        assert clients[0]["port"] == 54321
        assert clients[0]["available_streams"] == 1
        assert clients[0]["codecs"] == "JPEG"
        assert clients[0]["fd"] == 17
        assert clients[0]["connected_s"] >= 2
        assert clients[0]["estimated_bitrate_mbps"] == 1
        assert snapshot["service"]["stream_bitrate_mbps"] == 1
        assert snapshot["service"]["estimated_egress_mbps"] == 1
    finally:
        supervisor.shutdown()


def test_status_snapshot_is_published_to_standard_subscribers() -> None:
    supervisor = Supervisor(service_config())
    context = zmq.Context()
    subscriber = context.socket(zmq.SUB)
    subscriber.setsockopt(zmq.LINGER, 0)
    subscriber.setsockopt(zmq.SUBSCRIBE, b"status/")
    subscriber.connect(supervisor.stream_pub.getsockopt_string(zmq.LAST_ENDPOINT))
    try:
        assert supervisor.stream_pub.poll(1000) == zmq.POLLIN
        supervisor._handle_stream_subscriptions()
        assert subscriber.poll(1000) == zmq.POLLIN
        topic, payload = subscriber.recv_multipart()
        snapshot = json.loads(payload.decode("utf-8"))
        assert topic == b"status/snapshot"
        assert snapshot["type"] == "snapshot"
        assert snapshot["cameras"][0]["name"] == "cam"
        assert supervisor.records["cam"].demand_subscriptions == 0
    finally:
        subscriber.close(0)
        context.term()
        supervisor.shutdown()


def test_client_ip_reads_tcp_peer_address() -> None:
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    client = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        client.connect(listener.getsockname())
        connection, _ = listener.accept()
        try:
            assert Supervisor._client_peer(connection.fileno())[0] == "127.0.0.1"
            assert (
                Supervisor._client_peer(connection.fileno())[1]
                == client.getsockname()[1]
            )
        finally:
            connection.close()
    finally:
        client.close()
        listener.close()


def test_idle_camera_sleeps_after_grace_period(monkeypatch) -> None:
    supervisor = Supervisor(idle_service_config())
    record = supervisor.records["cam"]
    record.process = StuckWorkerProcess()
    stopped = []
    monkeypatch.setattr(supervisor, "_stop_worker", stopped.append)
    try:
        supervisor._reconcile_camera_idle(record, time.monotonic_ns())
        assert record.status["state"] == "IDLE_PENDING"
        supervisor._reconcile_camera_idle(record, time.monotonic_ns() + 1_000_000_000)
        assert stopped == [record]
        assert record.status["state"] == "SLEEPING"
        assert record.status["capture_fps"] == 0
    finally:
        supervisor.shutdown()


def test_subscription_wakes_sleeping_camera(monkeypatch) -> None:
    supervisor = Supervisor(idle_service_config())
    record = supervisor.records["cam"]
    record.status["state"] = "SLEEPING"
    starts = []
    monkeypatch.setattr(
        supervisor,
        "_start_worker",
        lambda item, **kwargs: starts.append((item, kwargs)),
    )
    try:
        record.demand_subscriptions = 1
        supervisor._reconcile_camera_idle(record, time.monotonic_ns())
        assert starts == [(record, {"state": "WAKING"})]
    finally:
        supervisor.shutdown()


def test_subscription_restores_idle_pending_worker_without_restarting() -> None:
    supervisor = Supervisor(idle_service_config())
    record = supervisor.records["cam"]
    record.process = StuckWorkerProcess()
    record.status["state"] = "ONLINE"
    try:
        supervisor._reconcile_camera_idle(record, time.monotonic_ns())
        assert record.status["state"] == "IDLE_PENDING"

        record.demand_subscriptions = 1
        supervisor._reconcile_camera_idle(record, time.monotonic_ns())
        assert record.process is not None
        assert record.status["state"] == "ONLINE"
    finally:
        supervisor.shutdown()


def test_subscription_restores_starting_state_while_worker_is_still_alive() -> None:
    supervisor = Supervisor(idle_service_config())
    record = supervisor.records["cam"]
    record.process = StuckWorkerProcess()
    try:
        supervisor._reconcile_camera_idle(record, time.monotonic_ns())
        assert record.status["state"] == "IDLE_PENDING"

        record.demand_subscriptions = 1
        supervisor._reconcile_camera_idle(record, time.monotonic_ns())
        assert record.process is not None
        assert record.status["state"] == "STARTING"
    finally:
        supervisor.shutdown()


def test_initial_worker_online_before_first_subscription_is_restored() -> None:
    supervisor = Supervisor(idle_service_config())
    record = supervisor.records["cam"]
    record.process = StuckWorkerProcess()
    context = zmq.Context()
    worker = context.socket(zmq.DEALER)
    worker.connect(supervisor.control_endpoint)
    try:
        supervisor._reconcile_camera_idle(record, time.monotonic_ns())
        assert record.status["state"] == "IDLE_PENDING"

        worker.send_json({"type": "state", "camera": "cam", "state": "ONLINE"})
        assert supervisor.control_router.poll(1000) == zmq.POLLIN
        supervisor._handle_control()
        worker.send_json(
            {
                "type": "capture",
                "camera": "cam",
                "captured_monotonic_ns": time.monotonic_ns(),
                "captured_utc_ns": time.time_ns(),
            }
        )
        assert supervisor.control_router.poll(1000) == zmq.POLLIN
        supervisor._handle_control()

        record.demand_subscriptions = 1
        supervisor._reconcile_camera_idle(record, time.monotonic_ns())
        assert record.status["state"] == "ONLINE"
    finally:
        worker.close(0)
        context.term()
        supervisor.shutdown()


def test_idle_pending_does_not_hide_worker_errors() -> None:
    supervisor = Supervisor(idle_service_config())
    record = supervisor.records["cam"]
    record.process = StuckWorkerProcess()
    context = zmq.Context()
    worker = context.socket(zmq.DEALER)
    worker.connect(supervisor.control_endpoint)
    try:
        supervisor._reconcile_camera_idle(record, time.monotonic_ns())
        assert record.status["state"] == "IDLE_PENDING"

        worker.send_json(
            {
                "type": "state",
                "camera": "cam",
                "state": "OFFLINE",
                "error": "device disconnected",
            }
        )
        assert supervisor.control_router.poll(1000) == zmq.POLLIN
        supervisor._handle_control()
        assert record.status["state"] == "OFFLINE"
        assert record.status["last_error"] == "device disconnected"
    finally:
        worker.close(0)
        context.term()
        supervisor.shutdown()


def test_waking_reaps_an_exited_worker_before_starting(monkeypatch) -> None:
    supervisor = Supervisor(idle_service_config())
    record = supervisor.records["cam"]
    exited = ExitedWorkerProcess()
    record.process = exited
    starts = []
    monkeypatch.setattr(
        supervisor,
        "_start_worker",
        lambda item, **kwargs: starts.append((item, kwargs)),
    )
    try:
        supervisor._wake_worker(record)
        assert exited.joined
        assert record.process is None
        assert starts == [(record, {"state": "WAKING"})]
    finally:
        supervisor.shutdown()


def test_xpub_subscription_tracks_only_the_requested_camera(monkeypatch) -> None:
    supervisor = Supervisor(idle_service_config())
    monkeypatch.setattr(
        supervisor,
        "_start_worker",
        lambda record, **kwargs: supervisor._set_state(record, kwargs["state"]),
    )
    context = zmq.Context()
    subscriber = context.socket(zmq.SUB)
    subscriber.setsockopt(zmq.LINGER, 0)
    subscriber.setsockopt(zmq.SUBSCRIBE, b"cam/color")
    subscriber.connect(supervisor.stream_pub.getsockopt_string(zmq.LAST_ENDPOINT))
    try:
        assert supervisor.stream_pub.poll(1000) == zmq.POLLIN
        supervisor._handle_stream_subscriptions()
        assert supervisor.records["cam"].demand_subscriptions == 1
        assert supervisor.records["cam"].status["state"] == "WAKING"
    finally:
        subscriber.close(0)
        context.term()
        supervisor.shutdown()
