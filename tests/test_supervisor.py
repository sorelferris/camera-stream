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


def service_config() -> ServiceConfig:
    return ServiceConfig.model_validate(
        {
            "endpoints": {
                "stream_pub": "tcp://127.0.0.1:*",
                "status_rep": "tcp://localhost:*",
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
