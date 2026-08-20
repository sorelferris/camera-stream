from __future__ import annotations

import json
import multiprocessing as mp
import os
import shutil
import signal
import socket as socket_lib
import tempfile
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import zmq
from zmq.utils.monitor import recv_monitor_message

from camera_stream.config import CameraConfig, ServiceConfig
from camera_stream.dashboard import Dashboard
from camera_stream.demand import TopicDemand
from camera_stream.protocol import json_bytes
from camera_stream.worker import run_worker

RECOVERY_WATCHDOG_TIMEOUT_NS = 10_000_000_000
BITRATE_WINDOW_NS = 1_000_000_000
BITRATE_BUCKET_NS = 100_000_000
BITRATE_BUCKET_COUNT = BITRATE_WINDOW_NS // BITRATE_BUCKET_NS
STATUS_SNAPSHOT_INTERVAL_NS = 1_000_000_000


@dataclass
class WorkerRecord:
    config: CameraConfig
    stop: Any
    process: mp.Process | None = None
    identity: bytes | None = None
    restart_attempt: int = 0
    next_restart_at: float = 0.0
    demand_subscriptions: int = 0
    idle_since_monotonic_ns: int | None = None
    idle_resume_state: str | None = None
    accept_after_monotonic_ns: int = 0
    status: dict[str, Any] = field(default_factory=dict)


class Supervisor:
    def __init__(self, config: ServiceConfig) -> None:
        self.config = config
        self.context = zmq.Context()
        self.stop_requested = False
        self._shutdown_complete = False
        self.status_revision = 0
        self.started_monotonic_ns = time.monotonic_ns()
        self.last_published_frame_ns = 0
        self.last_status_snapshot_ns = 0
        self.last_service_cost_ms: float | None = None
        self.last_supervisor_cost_ms: float | None = None
        self.publish_bitrate_buckets: deque[tuple[int, int]] = deque(
            maxlen=BITRATE_BUCKET_COUNT
        )
        self.runtime_dir = Path(tempfile.mkdtemp(prefix="camera-stream-"))
        os.chmod(self.runtime_dir, 0o700)
        self.frame_endpoint = f"ipc://{self.runtime_dir / 'frames.sock'}"
        self.control_endpoint = f"ipc://{self.runtime_dir / 'control.sock'}"
        self.frame_pull = self._socket(zmq.PULL, rcvhwm=1)
        self.frame_pull.bind(self.frame_endpoint)
        self.control_router = self._socket(zmq.ROUTER, rcvhwm=10, sndhwm=10)
        self.control_router.bind(self.control_endpoint)
        self.stream_pub = self._socket(zmq.XPUB, sndhwm=1)
        self.stream_pub.setsockopt(zmq.XPUB_VERBOSE, 1)
        self.stream_pub.bind(config.endpoints.stream_pub)
        self.stream_monitor = self.stream_pub.get_monitor_socket(
            zmq.EVENT_ACCEPTED | zmq.EVENT_DISCONNECTED
        )
        self.poller = zmq.Poller()
        for socket in (
            self.frame_pull,
            self.control_router,
            self.stream_pub,
        ):
            self.poller.register(socket, zmq.POLLIN)
        self.poller.register(self.stream_monitor, zmq.POLLIN)
        self.records = {
            camera.name: WorkerRecord(
                camera,
                mp.get_context("spawn").Event(),
                status=self._initial_status(camera),
            )
            for camera in config.cameras
        }
        for record in self.records.values():
            if config.idle_policy.enabled:
                record.status["idle_after_s"] = config.idle_policy.sleep_after_s
        self.clients: dict[int, dict[str, Any]] = {}
        self.topic_demand = TopicDemand([camera.name for camera in config.cameras])

    def _socket(
        self, kind: int, *, rcvhwm: int | None = None, sndhwm: int | None = None
    ) -> zmq.Socket:
        socket = self.context.socket(kind)
        socket.setsockopt(zmq.LINGER, 0)
        if rcvhwm is not None:
            socket.setsockopt(zmq.RCVHWM, rcvhwm)
        if sndhwm is not None:
            socket.setsockopt(zmq.SNDHWM, sndhwm)
        return socket

    @staticmethod
    def _initial_status(camera: CameraConfig) -> dict[str, Any]:
        return {
            "name": camera.name,
            "driver": camera.driver,
            "state": "STARTING",
            "state_since_monotonic_ns": time.monotonic_ns(),
            "last_heartbeat_ns": 0,
            "last_capture_monotonic_ns": 0,
            "last_capture_utc_ns": 0,
            "last_published_ns": 0,
            "last_capture_to_publish_ms": None,
            "capture_cost_ms": None,
            "ipc_cost_ms": None,
            "last_sequence": 0,
            "capture_fps": 0,
            "publish_fps": 0,
            "dropped_before_encode": 0,
            "dropped_ipc": 0,
            "dropped_pub": 0,
            "reconnect_attempt": 0,
            "last_error": None,
            "pid": None,
            "demand_subscriptions": 0,
            "idle_after_s": None,
        }

    def start_workers(self) -> None:
        for record in self.records.values():
            self._start_worker(record)

    def _start_worker(self, record: WorkerRecord, *, state: str = "STARTING") -> None:
        record.stop.clear()
        process = mp.get_context("spawn").Process(
            target=run_worker,
            args=(
                record.config.model_dump(mode="json"),
                self.frame_endpoint,
                self.control_endpoint,
                record.stop,
            ),
            name=f"camera-worker-{record.config.name}",
        )
        process.start()
        record.process = process
        record.identity = None
        record.status["pid"] = process.pid
        self._set_state(record, state)

    def _set_state(
        self,
        record: WorkerRecord,
        state: str,
        *,
        error: str | None = None,
        attempt: int | None = None,
    ) -> None:
        changed = (
            record.status.get("state") != state
            or record.status.get("last_error") != error
        )
        record.status["state"] = state
        record.status["last_error"] = error
        if attempt is not None:
            record.status["reconnect_attempt"] = attempt
        if changed:
            record.status["state_since_monotonic_ns"] = time.monotonic_ns()
            self.status_revision += 1
            event = {
                "schema_version": 1,
                "type": "camera_state",
                "status_revision": self.status_revision,
                "camera": record.config.name,
                "state": state,
                "at_monotonic_ns": time.monotonic_ns(),
                "error": error,
            }
            try:
                self.stream_pub.send_multipart(
                    [
                        f"status/camera/{record.config.name}".encode(),
                        json_bytes(event),
                    ],
                    flags=zmq.DONTWAIT,
                )
            except zmq.Again:
                # The next periodic snapshot lets slow subscribers converge.
                pass

    def _set_worker_state(
        self,
        record: WorkerRecord,
        state: str,
        *,
        error: str | None = None,
        attempt: int | None = None,
    ) -> None:
        """Keep an idle overlay while an otherwise healthy worker becomes online."""
        if (
            self.config.idle_policy.enabled
            and not record.demand_subscriptions
            and record.status.get("state") == "IDLE_PENDING"
            and state == "ONLINE"
        ):
            record.idle_resume_state = state
            return
        self._set_state(record, state, error=error, attempt=attempt)

    def _handle_stream_subscriptions(self) -> None:
        """Apply standard SUB topic changes emitted by the XPUB socket."""
        changed_names: set[str] = set()
        snapshot_requested = False
        while True:
            try:
                event = self.stream_pub.recv(flags=zmq.DONTWAIT)
            except zmq.Again:
                break
            if event[:1] == b"\x01" and b"status/snapshot".startswith(event[1:]):
                snapshot_requested = True
            changed_names.update(self.topic_demand.apply(event))
        for name in changed_names:
            record = self.records[name]
            record.demand_subscriptions = self.topic_demand.count(name)
            record.status["demand_subscriptions"] = record.demand_subscriptions
            self._reconcile_camera_idle(record, time.monotonic_ns())
        if snapshot_requested:
            self._publish_status_snapshot(force=True)

    def _reconcile_idle_policy(self) -> None:
        if not self.config.idle_policy.enabled:
            return
        now_ns = time.monotonic_ns()
        for record in self.records.values():
            self._reconcile_camera_idle(record, now_ns)

    def _reconcile_camera_idle(self, record: WorkerRecord, now_ns: int) -> None:
        """Move one camera between active, pending, sleeping, and waking states."""
        if not self.config.idle_policy.enabled:
            return
        if record.demand_subscriptions:
            record.idle_since_monotonic_ns = None
            if record.process is None or not record.process.is_alive():
                record.idle_resume_state = None
                self._wake_worker(record)
            elif record.status.get("state") == "IDLE_PENDING":
                self._set_state(record, record.idle_resume_state or "ONLINE")
                record.idle_resume_state = None
            return

        state = record.status.get("state")
        if state in {"SLEEPING", "CONFIG_ERROR"}:
            return
        if record.idle_since_monotonic_ns is None:
            record.idle_since_monotonic_ns = now_ns
            record.idle_resume_state = state
            self._set_state(record, "IDLE_PENDING")
            return
        sleep_after_ns = int(self.config.idle_policy.sleep_after_s * 1_000_000_000)
        if now_ns - record.idle_since_monotonic_ns >= sleep_after_ns:
            self._sleep_worker(record)

    def _wake_worker(self, record: WorkerRecord) -> None:
        if record.status.get("state") == "CONFIG_ERROR":
            return
        if record.process is not None and not record.process.is_alive():
            record.process.join(timeout=0)
            record.process = None
            record.identity = None
        record.restart_attempt = 0
        record.next_restart_at = 0.0
        record.status["last_heartbeat_ns"] = 0
        record.accept_after_monotonic_ns = time.monotonic_ns()
        self._start_worker(record, state="WAKING")

    def _sleep_worker(self, record: WorkerRecord) -> None:
        self._stop_worker(record)
        record.idle_since_monotonic_ns = None
        record.idle_resume_state = None
        record.status.update(
            {
                "pid": None,
                "capture_fps": 0,
                "publish_fps": 0,
                "last_heartbeat_ns": 0,
            }
        )
        self._set_state(record, "SLEEPING")

    def _stop_worker(self, record: WorkerRecord) -> None:
        record.stop.set()
        if record.identity is not None:
            try:
                self.control_router.send_multipart(
                    [record.identity, json_bytes({"type": "stop"})],
                    flags=zmq.DONTWAIT,
                )
            except zmq.Again:
                pass
        process = record.process
        if process is not None:
            process.join(timeout=2.0)
            if process.is_alive():
                process.terminate()
                process.join(timeout=1.0)
        record.process = None
        record.identity = None

    def _handle_control(self) -> None:
        identity, payload = self.control_router.recv_multipart()
        try:
            message = json.loads(payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return
        name = message.get("camera")
        record = self.records.get(name)
        if record is None:
            return
        if (
            self.config.idle_policy.enabled
            and not record.demand_subscriptions
            and record.status.get("state") == "SLEEPING"
        ):
            return
        record.identity = identity
        record.status["last_heartbeat_ns"] = time.monotonic_ns()
        if message.get("type") == "hello":
            record.status["pid"] = message.get("pid")
        elif message.get("type") == "state":
            worker_state = message.get("state", "OFFLINE")
            if worker_state == "ONLINE":
                record.restart_attempt = 0
            self._set_worker_state(
                record,
                worker_state,
                error=message.get("error"),
                attempt=message.get("reconnect_attempt"),
            )
        elif message.get("type") == "capture":
            record.status["last_capture_monotonic_ns"] = message.get(
                "captured_monotonic_ns", 0
            )
            record.status["last_capture_utc_ns"] = message.get("captured_utc_ns", 0)
            if record.status.get("state") != "ONLINE":
                self._set_worker_state(record, "ONLINE")
        elif message.get("type") == "heartbeat":
            metrics = message.get("metrics", {})
            record.status.update(
                {
                    "last_capture_monotonic_ns": metrics.get(
                        "last_capture_monotonic_ns", 0
                    ),
                    "last_capture_utc_ns": metrics.get("last_capture_utc_ns", 0),
                    "capture_fps": metrics.get("capture_fps", 0),
                    "capture_cost_ms": metrics.get("capture_cost_ms"),
                    "ipc_cost_ms": metrics.get("ipc_cost_ms"),
                    "dropped_before_encode": metrics.get("dropped_before_encode", 0),
                    "dropped_ipc": metrics.get("ipc_dropped", 0),
                }
            )
        elif message.get("type") == "error":
            self._set_state(record, "OFFLINE", error=message.get("error"))

    def _publish_frame(self) -> None:
        header, payload = self.frame_pull.recv_multipart()
        received_ns = time.monotonic_ns()
        try:
            frame = json.loads(header.decode("utf-8"))
            name = frame["camera"]
            record = self.records[name]
        except (KeyError, TypeError, UnicodeDecodeError, json.JSONDecodeError):
            return
        if (
            int(frame.get("captured_monotonic_ns", 0))
            < record.accept_after_monotonic_ns
        ):
            return
        topic = f"{name}/color".encode()
        service_started_ns = time.monotonic_ns()
        try:
            self.stream_pub.send_multipart([topic, header, payload], flags=zmq.DONTWAIT)
        except zmq.Again:
            record.status["dropped_pub"] += 1
            return
        now_ns = time.monotonic_ns()
        self.last_supervisor_cost_ms = round(
            (service_started_ns - received_ns) / 1_000_000, 2
        )
        self.last_service_cost_ms = round((now_ns - service_started_ns) / 1_000_000, 2)
        now_utc_ns = time.time_ns()
        previous = record.status.get("last_published_ns", 0)
        record.status["last_published_ns"] = now_ns
        record.status["last_sequence"] = frame.get("sequence", 0)
        captured_utc_ns = int(frame.get("captured_utc_ns", 0))
        if captured_utc_ns:
            latency_ms = max(0.0, (now_utc_ns - captured_utc_ns) / 1_000_000)
            record.status["last_capture_to_publish_ms"] = round(latency_ms, 2)
        self.last_published_frame_ns = now_ns
        self._record_published_bytes(now_ns, len(topic) + len(header) + len(payload))
        if previous:
            delta = now_ns - previous
            if delta > 0:
                record.status["publish_fps"] = round(1_000_000_000 / delta, 2)

    def _status_snapshot(self) -> dict[str, Any]:
        now_ns = time.monotonic_ns()
        stream_bitrate_mbps = self._stream_bitrate_mbps(now_ns)
        available_codecs = ", ".join(
            sorted({camera.encoding.codec.upper() for camera in self.config.cameras})
        )
        clients = [
            {
                "ip": client["ip"],
                "port": client.get("port"),
                "fd": client["fd"],
                "endpoint": client["endpoint"],
                "available_streams": len(self.records),
                "codecs": available_codecs,
                "connected_s": round(
                    (now_ns - client["connected_monotonic_ns"]) / 1_000_000_000, 1
                ),
                "estimated_bitrate_mbps": stream_bitrate_mbps,
            }
            for client in self.clients.values()
        ]
        return {
            "schema_version": 1,
            "server_time_ns": time.time_ns(),
            "server_monotonic_ns": time.monotonic_ns(),
            "status_revision": self.status_revision,
            "service_uptime_s": round(
                (time.monotonic_ns() - self.started_monotonic_ns) / 1_000_000_000,
                3,
            ),
            "service": {
                "worker_count": len(self.records),
                "active_worker_count": sum(
                    1
                    for record in self.records.values()
                    if record.process is not None and record.process.is_alive()
                ),
                "client_count": len(self.clients),
                "last_service_cost_ms": self.last_service_cost_ms,
                "last_supervisor_cost_ms": self.last_supervisor_cost_ms,
                "stream_bitrate_mbps": stream_bitrate_mbps,
                "estimated_egress_mbps": round(
                    stream_bitrate_mbps * len(self.clients), 2
                ),
                "stream_pub": self.config.endpoints.stream_pub,
                "idle_policy": {
                    "enabled": self.config.idle_policy.enabled,
                    "sleep_after_s": self.config.idle_policy.sleep_after_s,
                },
            },
            "clients": clients,
            "cameras": [dict(record.status) for record in self.records.values()],
        }

    def status_snapshot(self) -> dict[str, Any]:
        """Return the current in-process status for PUB snapshots and the dashboard."""
        return self._status_snapshot()

    def _stream_bitrate_mbps(self, now_ns: int) -> float:
        cutoff_ns = now_ns - BITRATE_WINDOW_NS
        while (
            self.publish_bitrate_buckets
            and self.publish_bitrate_buckets[0][0] < cutoff_ns
        ):
            self.publish_bitrate_buckets.popleft()
        return round(
            sum(size for _, size in self.publish_bitrate_buckets) * 8 / 1_000_000,
            2,
        )

    def _record_published_bytes(self, now_ns: int, size: int) -> None:
        bucket_start_ns = now_ns - now_ns % BITRATE_BUCKET_NS
        if (
            self.publish_bitrate_buckets
            and self.publish_bitrate_buckets[-1][0] == bucket_start_ns
        ):
            _, previous_size = self.publish_bitrate_buckets.pop()
            self.publish_bitrate_buckets.append((bucket_start_ns, previous_size + size))
            return
        self.publish_bitrate_buckets.append((bucket_start_ns, size))

    def _publish_status_snapshot(self, *, force: bool = False) -> None:
        """Broadcast the full state periodically so late SUB clients converge."""
        now_ns = time.monotonic_ns()
        if (
            not force
            and now_ns - self.last_status_snapshot_ns < STATUS_SNAPSHOT_INTERVAL_NS
        ):
            return
        snapshot = self._status_snapshot()
        snapshot["type"] = "snapshot"
        try:
            self.stream_pub.send_multipart(
                [b"status/snapshot", json_bytes(snapshot)], flags=zmq.DONTWAIT
            )
        except zmq.Again:
            return
        self.last_status_snapshot_ns = now_ns

    def _handle_stream_monitor(self) -> None:
        while True:
            try:
                message = recv_monitor_message(self.stream_monitor, flags=zmq.DONTWAIT)
            except zmq.Again:
                return
            event = message["event"]
            fd = int(message["value"])
            if event == zmq.EVENT_ACCEPTED:
                ip, port = self._client_peer(fd)
                self.clients[fd] = {
                    "ip": ip,
                    "port": port,
                    "fd": fd,
                    "endpoint": message["endpoint"].decode("utf-8", "replace"),
                    "connected_monotonic_ns": time.monotonic_ns(),
                }
            elif event == zmq.EVENT_DISCONNECTED:
                self.clients.pop(fd, None)

    @staticmethod
    def _client_peer(fd: int) -> tuple[str, int | None]:
        try:
            with socket_lib.socket(fileno=os.dup(fd)) as connection:
                peer = connection.getpeername()
        except OSError:
            return "unknown", None
        if isinstance(peer, tuple):
            return str(peer[0]), int(peer[1]) if len(peer) > 1 else None
        return str(peer), None

    @classmethod
    def _client_ip(cls, fd: int) -> str:
        return cls._client_peer(fd)[0]

    def _monitor_workers(self) -> None:
        now_seconds = time.monotonic()
        now_ns = time.monotonic_ns()
        for record in self.records.values():
            process = record.process
            if process is None:
                continue
            if not process.is_alive():
                process.join(timeout=0)
                if record.status.get("state") == "CONFIG_ERROR":
                    continue
                if self.config.idle_policy.enabled and not record.demand_subscriptions:
                    record.process = None
                    record.identity = None
                    record.idle_resume_state = None
                    self._set_state(record, "SLEEPING")
                    continue
                record.restart_attempt += 1
                if now_seconds < record.next_restart_at:
                    continue
                self._set_state(
                    record,
                    "RECOVERING",
                    error=f"worker exited with code {process.exitcode}",
                )
                record.next_restart_at = now_seconds + min(
                    30.0, 2 ** min(record.restart_attempt - 1, 5)
                )
                self._start_worker(record)
            elif (
                record.status.get("state") in {"OFFLINE", "RECOVERING", "WAKING"}
                and now_ns - record.status.get("state_since_monotonic_ns", now_ns)
                > RECOVERY_WATCHDOG_TIMEOUT_NS
            ):
                self._restart_stuck_worker(record)
            elif (
                record.status.get("last_heartbeat_ns")
                and now_ns - record.status["last_heartbeat_ns"] > 5_000_000_000
            ):
                self._set_state(record, "OFFLINE", error="worker heartbeat timeout")

    def _restart_stuck_worker(self, record: WorkerRecord) -> None:
        process = record.process
        if process is None:
            return
        record.stop.set()
        process.join(timeout=1.0)
        if process.is_alive():
            process.terminate()
            process.join(timeout=1.0)
        record.restart_attempt += 1
        self._set_state(
            record,
            "RECOVERING",
            error="worker recovery watchdog restart",
            attempt=record.restart_attempt,
        )
        self._start_worker(record)

    def run(self, *, tui: bool = False) -> None:
        self.start_workers()
        dashboard = Dashboard(self) if tui else None
        try:
            if dashboard is not None:
                dashboard.start()
            while not self.stop_requested:
                events = dict(self.poller.poll(100))
                if self.frame_pull in events:
                    self._publish_frame()
                if self.control_router in events:
                    self._handle_control()
                if self.stream_monitor in events:
                    self._handle_stream_monitor()
                if self.stream_pub in events:
                    self._handle_stream_subscriptions()
                self._monitor_workers()
                self._reconcile_idle_policy()
                self._publish_status_snapshot()
                if dashboard is not None and dashboard.update():
                    self.stop_requested = True
        finally:
            if dashboard is not None:
                dashboard.stop()
            self.shutdown()

    def shutdown(self) -> None:
        if self._shutdown_complete:
            return
        self.stop_requested = True
        for record in self.records.values():
            self._stop_worker(record)
        for socket in (
            self.stream_pub,
            self.control_router,
            self.frame_pull,
        ):
            socket.close(0)
        self.stream_monitor.close(0)
        self.context.term()
        shutil.rmtree(self.runtime_dir, ignore_errors=True)
        self._shutdown_complete = True


def install_signal_handlers(supervisor: Supervisor) -> None:
    def stop_handler(_signum: int, _frame: Any) -> None:
        supervisor.stop_requested = True

    signal.signal(signal.SIGINT, stop_handler)
    signal.signal(signal.SIGTERM, stop_handler)
