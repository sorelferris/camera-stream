from __future__ import annotations

import json
import multiprocessing as mp
import os
import shutil
import signal
import tempfile
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import zmq

from camera_stream.config import CameraConfig, ServiceConfig
from camera_stream.dashboard import Dashboard
from camera_stream.protocol import json_bytes
from camera_stream.worker import run_worker

RECOVERY_WATCHDOG_TIMEOUT_NS = 10_000_000_000


@dataclass
class WorkerRecord:
    config: CameraConfig
    stop: Any
    process: mp.Process | None = None
    identity: bytes | None = None
    restart_attempt: int = 0
    next_restart_at: float = 0.0
    status: dict[str, Any] = field(default_factory=dict)


class Supervisor:
    def __init__(self, config: ServiceConfig) -> None:
        self.config = config
        self.context = zmq.Context()
        self.stop_requested = False
        self._shutdown_complete = False
        self.status_revision = 0
        self.started_monotonic_ns = time.monotonic_ns()
        self.published_frames: deque[int] = deque(maxlen=240)
        self.last_publish_latency_ms: float | None = None
        self.runtime_dir = Path(tempfile.mkdtemp(prefix="camera-stream-"))
        os.chmod(self.runtime_dir, 0o700)
        self.frame_endpoint = f"ipc://{self.runtime_dir / 'frames.sock'}"
        self.control_endpoint = f"ipc://{self.runtime_dir / 'control.sock'}"
        self.frame_pull = self._socket(zmq.PULL, rcvhwm=1)
        self.frame_pull.bind(self.frame_endpoint)
        self.control_router = self._socket(zmq.ROUTER, rcvhwm=10, sndhwm=10)
        self.control_router.bind(self.control_endpoint)
        self.stream_pub = self._socket(zmq.PUB, sndhwm=1)
        self.stream_pub.bind(config.endpoints.stream_pub)
        self.status_rep = self._socket(zmq.REP, rcvhwm=10, sndhwm=10)
        self.status_rep.bind(config.endpoints.status_rep)
        self.poller = zmq.Poller()
        for socket in (self.frame_pull, self.control_router, self.status_rep):
            self.poller.register(socket, zmq.POLLIN)
        self.records = {
            camera.name: WorkerRecord(
                camera,
                mp.get_context("spawn").Event(),
                status=self._initial_status(camera),
            )
            for camera in config.cameras
        }

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
            "last_sequence": 0,
            "capture_fps": 0,
            "publish_fps": 0,
            "dropped_before_encode": 0,
            "dropped_ipc": 0,
            "dropped_pub": 0,
            "reconnect_attempt": 0,
            "last_error": None,
            "pid": None,
        }

    def start_workers(self) -> None:
        for record in self.records.values():
            self._start_worker(record)

    def _start_worker(self, record: WorkerRecord) -> None:
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
        self._set_state(record, "STARTING")

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
                "type": "status",
                "status_revision": self.status_revision,
                "camera": record.config.name,
                "state": state,
                "at_monotonic_ns": time.monotonic_ns(),
                "error": error,
            }
            self.stream_pub.send_multipart(
                [f"status/{record.config.name}".encode(), json_bytes(event)],
                flags=zmq.DONTWAIT,
            )

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
        record.identity = identity
        record.status["last_heartbeat_ns"] = time.monotonic_ns()
        if message.get("type") == "hello":
            record.status["pid"] = message.get("pid")
        elif message.get("type") == "state":
            if message.get("state") == "ONLINE":
                record.restart_attempt = 0
            self._set_state(
                record,
                message.get("state", "OFFLINE"),
                error=message.get("error"),
                attempt=message.get("reconnect_attempt"),
            )
        elif message.get("type") == "capture":
            record.status["last_capture_monotonic_ns"] = message.get(
                "captured_monotonic_ns", 0
            )
            record.status["last_capture_utc_ns"] = message.get("captured_utc_ns", 0)
            if record.status.get("state") != "ONLINE":
                self._set_state(record, "ONLINE")
        elif message.get("type") == "heartbeat":
            metrics = message.get("metrics", {})
            record.status.update(
                {
                    "last_capture_monotonic_ns": metrics.get(
                        "last_capture_monotonic_ns", 0
                    ),
                    "last_capture_utc_ns": metrics.get("last_capture_utc_ns", 0),
                    "capture_fps": metrics.get("capture_fps", 0),
                    "dropped_before_encode": metrics.get("dropped_before_encode", 0),
                    "dropped_ipc": metrics.get("ipc_dropped", 0),
                }
            )
        elif message.get("type") == "error":
            self._set_state(record, "OFFLINE", error=message.get("error"))

    def _publish_frame(self) -> None:
        header, payload = self.frame_pull.recv_multipart()
        try:
            frame = json.loads(header.decode("utf-8"))
            name = frame["camera"]
            record = self.records[name]
        except (KeyError, TypeError, UnicodeDecodeError, json.JSONDecodeError):
            return
        topic = f"{name}/color".encode()
        try:
            self.stream_pub.send_multipart([topic, header, payload], flags=zmq.DONTWAIT)
        except zmq.Again:
            record.status["dropped_pub"] += 1
            return
        now_ns = time.monotonic_ns()
        now_utc_ns = time.time_ns()
        previous = record.status.get("last_published_ns", 0)
        record.status["last_published_ns"] = now_ns
        record.status["last_sequence"] = frame.get("sequence", 0)
        captured_utc_ns = int(frame.get("captured_utc_ns", 0))
        if captured_utc_ns:
            latency_ms = max(0.0, (now_utc_ns - captured_utc_ns) / 1_000_000)
            record.status["last_capture_to_publish_ms"] = round(latency_ms, 2)
            self.last_publish_latency_ms = round(latency_ms, 2)
        self.published_frames.append(now_ns)
        cutoff = now_ns - 1_000_000_000
        while self.published_frames and self.published_frames[0] < cutoff:
            self.published_frames.popleft()
        if previous:
            delta = now_ns - previous
            if delta > 0:
                record.status["publish_fps"] = round(1_000_000_000 / delta, 2)

    def _status_snapshot(self) -> dict[str, Any]:
        now_ns = time.monotonic_ns()
        while (
            self.published_frames and self.published_frames[0] < now_ns - 1_000_000_000
        ):
            self.published_frames.popleft()
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
                "aggregate_publish_fps": len(self.published_frames),
                "last_publish_latency_ms": self.last_publish_latency_ms,
                "stream_pub": self.config.endpoints.stream_pub,
                "status_rep": self.config.endpoints.status_rep,
            },
            "cameras": [dict(record.status) for record in self.records.values()],
        }

    def status_snapshot(self) -> dict[str, Any]:
        """Return the current in-process status for the REP API and dashboard."""
        return self._status_snapshot()

    def _handle_status(self) -> None:
        try:
            request = json.loads(self.status_rep.recv().decode("utf-8"))
            if request.get("op") != "get_status":
                raise ValueError("only get_status is supported")
            response = self._status_snapshot()
        except (
            ValueError,
            AttributeError,
            UnicodeDecodeError,
            json.JSONDecodeError,
        ) as exc:
            response = {"schema_version": 1, "error": str(exc)}
        self.status_rep.send(json_bytes(response))

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
                record.status.get("state") in {"OFFLINE", "RECOVERING"}
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
                if self.status_rep in events:
                    self._handle_status()
                self._monitor_workers()
                if dashboard is not None:
                    dashboard.update()
        finally:
            if dashboard is not None:
                dashboard.stop()
            self.shutdown()

    def shutdown(self) -> None:
        if self._shutdown_complete:
            return
        self.stop_requested = True
        for record in self.records.values():
            record.stop.set()
            if record.identity is not None:
                try:
                    self.control_router.send_multipart(
                        [record.identity, json_bytes({"type": "stop"})],
                        flags=zmq.DONTWAIT,
                    )
                except zmq.Again:
                    pass
        for record in self.records.values():
            if record.process is not None:
                record.process.join(timeout=2.0)
                if record.process.is_alive():
                    record.process.terminate()
                    record.process.join(timeout=1.0)
        for socket in (
            self.status_rep,
            self.stream_pub,
            self.control_router,
            self.frame_pull,
        ):
            socket.close(0)
        self.context.term()
        shutil.rmtree(self.runtime_dir, ignore_errors=True)
        self._shutdown_complete = True


def install_signal_handlers(supervisor: Supervisor) -> None:
    def stop_handler(_signum: int, _frame: Any) -> None:
        supervisor.stop_requested = True

    signal.signal(signal.SIGINT, stop_handler)
    signal.signal(signal.SIGTERM, stop_handler)
