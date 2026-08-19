"""Thread-owned ZeroMQ SUB receiver for video and server status."""

from __future__ import annotations

import threading
import time
from typing import Any
from urllib.parse import urlsplit

import zmq

from .protocol import ProtocolError, StatusEvent, StatusSnapshot, parse_message
from .state import CameraRegistry


def client_endpoint(endpoint: str) -> str:
    """Translate local wildcard bind addresses into valid client destinations."""
    parsed = urlsplit(endpoint)
    if parsed.scheme != "tcp" or parsed.hostname not in {"0.0.0.0", "::"}:
        return endpoint
    try:
        port = parsed.port
    except ValueError:
        return endpoint
    return endpoint if port is None else f"tcp://127.0.0.1:{port}"


class StatusStore:
    """The latest PUB snapshot, safe to read from the UI thread."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._snapshot: dict[str, Any] | None = None
        self._last_success_ns = 0

    def success(self, snapshot: dict[str, Any]) -> None:
        with self._lock:
            self._snapshot = snapshot
            self._last_success_ns = time.monotonic_ns()

    def view(self, now_ns: int) -> dict[str, Any]:
        with self._lock:
            age_s = (
                None
                if not self._last_success_ns
                else (now_ns - self._last_success_ns) / 1_000_000_000
            )
            return {
                "snapshot": self._snapshot,
                "last_success_age_s": age_s,
            }


class StreamReceiver(threading.Thread):
    """Owns the SUB socket and forwards only the latest validated frames."""

    def __init__(
        self,
        endpoint: str,
        cameras: set[str],
        registry: CameraRegistry,
        status_store: StatusStore,
        stop: threading.Event,
    ) -> None:
        super().__init__(name="camera-stream-receiver", daemon=True)
        self.endpoint = endpoint
        self.cameras = cameras
        self.registry = registry
        self.status_store = status_store
        self.stop = stop
        self.error: str | None = None

    def run(self) -> None:
        context = zmq.Context()
        socket = context.socket(zmq.SUB)
        socket.setsockopt(zmq.RCVHWM, 1)
        socket.setsockopt(zmq.LINGER, 0)
        socket.setsockopt(zmq.SUBSCRIBE, b"status/")
        if self.cameras:
            for camera in sorted(self.cameras):
                socket.setsockopt(zmq.SUBSCRIBE, f"{camera}/color".encode())
        else:
            socket.setsockopt(zmq.SUBSCRIBE, b"")
        socket.connect(self.endpoint)
        poller = zmq.Poller()
        poller.register(socket, zmq.POLLIN)
        try:
            while not self.stop.is_set():
                if socket not in dict(poller.poll(100)):
                    continue
                self._drain(socket)
        except zmq.ZMQError as exc:
            if not self.stop.is_set():
                self.error = str(exc)
        finally:
            poller.unregister(socket)
            socket.close(0)
            context.term()

    def _drain(self, socket: zmq.Socket) -> None:
        while not self.stop.is_set():
            try:
                parts = socket.recv_multipart(flags=zmq.DONTWAIT)
            except zmq.Again:
                return
            try:
                message = parse_message(parts)
            except ProtocolError as exc:
                self.registry.record_invalid(str(exc), exc.camera)
                continue
            if isinstance(message, StatusEvent):
                self.registry.apply_stream_status(message)
                continue
            if isinstance(message, StatusSnapshot):
                self.status_store.success(message.snapshot)
                self.registry.apply_status_snapshot(message.snapshot)
                continue
            self.registry.receive(message, time.monotonic_ns(), time.time_ns())
