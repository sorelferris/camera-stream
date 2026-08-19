"""Thread-owned ZeroMQ clients for video and optional server status."""

from __future__ import annotations

import json
import threading
import time
from typing import Any
from urllib.parse import urlsplit

import zmq

from .protocol import ProtocolError, StatusEvent, parse_message
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
    """The latest independent REP snapshot, safe to read from the UI thread."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._snapshot: dict[str, Any] | None = None
        self._last_success_ns = 0
        self._last_error: str | None = None

    def success(self, snapshot: dict[str, Any]) -> None:
        with self._lock:
            self._snapshot = snapshot
            self._last_success_ns = time.monotonic_ns()
            self._last_error = None

    def failure(self, error: str) -> None:
        with self._lock:
            self._last_error = error

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
                "last_error": self._last_error,
            }


class StreamReceiver(threading.Thread):
    """Owns the SUB socket and forwards only the latest validated frames."""

    def __init__(
        self,
        endpoint: str,
        cameras: set[str],
        registry: CameraRegistry,
        stop: threading.Event,
    ) -> None:
        super().__init__(name="camera-stream-receiver", daemon=True)
        self.endpoint = endpoint
        self.cameras = cameras
        self.registry = registry
        self.stop = stop
        self.error: str | None = None

    def run(self) -> None:
        context = zmq.Context()
        socket = context.socket(zmq.SUB)
        socket.setsockopt(zmq.RCVHWM, 1)
        socket.setsockopt(zmq.LINGER, 0)
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
            self.registry.receive(message, time.monotonic_ns(), time.time_ns())


class StatusPoller(threading.Thread):
    """Owns the REQ socket; a slow status endpoint cannot stall video."""

    def __init__(
        self,
        endpoint: str,
        registry: CameraRegistry,
        store: StatusStore,
        stop: threading.Event,
    ) -> None:
        super().__init__(name="camera-stream-status", daemon=True)
        self.endpoint = endpoint
        self.registry = registry
        self.store = store
        self.stop = stop

    def run(self) -> None:
        context = zmq.Context()
        socket: zmq.Socket | None = None
        try:
            while not self.stop.is_set():
                socket, healthy = self._request(context, socket)
                if not healthy:
                    socket = None
                self.stop.wait(1.0)
        finally:
            if socket is not None:
                socket.close(0)
            context.term()

    def _request(
        self, context: zmq.Context, socket: zmq.Socket | None
    ) -> tuple[zmq.Socket | None, bool]:
        try:
            if socket is None:
                socket = context.socket(zmq.REQ)
                socket.setsockopt(zmq.LINGER, 0)
                socket.connect(self.endpoint)
            socket.send_json({"op": "get_status"})
            poller = zmq.Poller()
            poller.register(socket, zmq.POLLIN)
            if socket not in dict(poller.poll(500)):
                socket.close(0)
                self.store.failure("status endpoint timeout")
                return None, False
            response = socket.recv_json()
            if not isinstance(response, dict) or response.get("error"):
                raise ValueError(str(response.get("error", "invalid status response")))
            self.store.success(response)
            self.registry.apply_status_snapshot(response)
            return socket, True
        except (ValueError, zmq.ZMQError, json.JSONDecodeError) as exc:
            if socket is not None:
                socket.close(0)
            self.store.failure(str(exc))
            return None, False
