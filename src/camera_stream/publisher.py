"""Latest-frame-wins remote camera publisher for the private ingest endpoint."""

from __future__ import annotations

import json
import logging
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass
from typing import Any

import cv2
import numpy as np
import zmq
from zmq.utils.monitor import recv_monitor_message

from camera_stream.ingest import INGEST_SCHEMA_VERSION
from camera_stream.protocol import RawFrame, frame_header, json_bytes, now

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class _PendingFrame:
    header: bytes
    payload: bytes


class _LatestPayloadSlot:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._frame: _PendingFrame | None = None
        self.replaced = 0

    def put(self, frame: _PendingFrame) -> bool:
        with self._lock:
            overwritten = self._frame is not None
            if overwritten:
                self.replaced += 1
            self._frame = frame
            return overwritten

    def take(self) -> _PendingFrame | None:
        with self._lock:
            frame, self._frame = self._frame, None
            return frame


class PublishedStream:
    """One camera topic managed by :class:`StreamPublisher`."""

    def __init__(
        self,
        publisher: StreamPublisher,
        camera: str,
        codec: str,
        jpeg_quality: int | None,
    ) -> None:
        self._publisher = publisher
        self.camera = camera
        self.topic = f"{camera}/color"
        self.codec = codec
        self.jpeg_quality = jpeg_quality
        self._slot = _LatestPayloadSlot()
        self._lock = threading.Lock()
        self._sequence = 0
        self._state = "WAITING_FOR_SERVER"
        self._error: str | None = None
        self._lease_token: str | None = None
        self._last_sent: _PendingFrame | None = None
        self._lease_retry_used = False
        self._encoded = 0
        self._encode_cost_ms: float | None = None
        self._socket_dropped = 0
        self._disconnected_dropped = 0

    def publish(self, image: np.ndarray) -> None:
        """Synchronously encode one BGR image; network delivery never blocks."""
        if not isinstance(image, np.ndarray) or image.ndim != 3 or image.shape[2] != 3:
            raise ValueError("image must be a HxWx3 NumPy BGR array")
        with self._lock:
            if self._state == "REJECTED":
                return
        if not self._publisher.connected:
            with self._lock:
                self._disconnected_dropped += 1
            return
        started_ns = time.monotonic_ns()
        frame = RawFrame(image, *now())
        if self.codec == "jpeg":
            ok, encoded = cv2.imencode(
                ".jpg", image, [int(cv2.IMWRITE_JPEG_QUALITY), self.jpeg_quality or 85]
            )
            if not ok:
                raise ValueError("JPEG encoding failed")
            payload = encoded.tobytes()
        else:
            payload = image.tobytes(order="C")
        with self._lock:
            self._sequence += 1
            header = frame_header(
                camera=self.camera,
                sequence=self._sequence,
                frame=frame,
                width=int(image.shape[1]),
                height=int(image.shape[0]),
                codec=self.codec,
                payload_size=len(payload),
            )
            self._encoded += 1
            self._encode_cost_ms = round(
                (time.monotonic_ns() - started_ns) / 1_000_000, 2
            )
            if self._state in {"WAITING_FOR_SERVER", "CONNECTING"}:
                self._state = "STARTING"
        self._slot.put(_PendingFrame(header, payload))
        self._publisher._wake()

    @property
    def state(self) -> str:
        with self._lock:
            return self._state

    @property
    def error(self) -> str | None:
        with self._lock:
            return self._error

    @property
    def metrics(self) -> dict[str, Any]:
        with self._lock:
            return {
                "encoded_frames": self._encoded,
                "encode_cost_ms": self._encode_cost_ms,
                "dropped_slot": self._slot.replaced,
                "dropped_socket": self._socket_dropped,
                "dropped_disconnected": self._disconnected_dropped,
            }

    def _take(self) -> _PendingFrame | None:
        with self._lock:
            if self._state == "REJECTED":
                return None
        return self._slot.take()

    def _metadata(self) -> bytes:
        with self._lock:
            metadata: dict[str, Any] = {
                "type": "frame",
                "ingest_schema_version": INGEST_SCHEMA_VERSION,
            }
            if self._publisher.token is not None:
                # Keep the access credential available when a server restart or
                # lease expiry turns the next frame into a fresh claim.
                metadata["token"] = self._publisher.token
            if self._lease_token is not None:
                metadata["lease_token"] = self._lease_token
        return json_bytes(metadata)

    def _sent(self, frame: _PendingFrame) -> None:
        with self._lock:
            self._last_sent = frame
            if self._lease_token is not None and self._state != "REJECTED":
                self._state = "ONLINE"
                self._error = None

    def _socket_drop(self) -> None:
        with self._lock:
            self._socket_dropped += 1

    def _set_connection_state(self, connected: bool) -> None:
        with self._lock:
            if self._state != "REJECTED":
                self._state = "CONNECTING" if connected else "WAITING_FOR_SERVER"
                if not connected:
                    self._error = None

    def _reply(self, message: dict[str, Any]) -> None:
        code = message.get("code")
        with self._lock:
            if code == "ACCEPTED":
                token = message.get("lease_token")
                if isinstance(token, str):
                    self._lease_token = token
                    self._state = "ONLINE"
                    self._error = None
                    self._lease_retry_used = False
            elif code == "LEASE_EXPIRED":
                if self._lease_retry_used:
                    self._state = "REJECTED"
                    self._error = "lease expired repeatedly"
                else:
                    self._lease_token = None
                    self._lease_retry_used = True
                    self._state = "STARTING"
                    if self._last_sent is not None:
                        self._slot.put(self._last_sent)
            elif isinstance(code, str):
                self._state = "REJECTED"
                self._error = str(message.get("error") or code)

    def _close_message(self) -> bytes | None:
        with self._lock:
            if self._lease_token is None:
                return None
            return json_bytes(
                {
                    "type": "close",
                    "ingest_schema_version": INGEST_SCHEMA_VERSION,
                    "topic": self.topic,
                    "lease_token": self._lease_token,
                }
            )

    def _local_error(self, state: str, error: str) -> None:
        with self._lock:
            if self._state != "REJECTED":
                self._state = state
                self._error = error


class StreamPublisher:
    """Thread-safe multi-topic publisher using one background DEALER socket."""

    def __init__(self, endpoint: str, *, token: str | None = None) -> None:
        if not endpoint.startswith("tcp://"):
            raise ValueError("endpoint must use tcp://")
        self.endpoint = endpoint
        self.token = token
        self._streams: dict[str, PublishedStream] = {}
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._wake_event = threading.Event()
        self._connected = threading.Event()
        self._thread: threading.Thread | None = None
        self._round_robin = deque[str]()
        self._identity = f"camera-stream-push-{uuid.uuid4().hex}".encode()

    def __enter__(self) -> StreamPublisher:  # noqa: PYI034 - Python 3.10 support
        self.start()
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    @property
    def connected(self) -> bool:
        return self._connected.is_set()

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._run, name="stream-publisher", daemon=True
        )
        self._thread.start()

    def open_stream(
        self, *, camera: str, codec: str = "jpeg", jpeg_quality: int | None = 85
    ) -> PublishedStream:
        if (
            not camera
            or not camera.replace("_", "a")
            .replace("-", "a")
            .replace(".", "a")
            .isalnum()
        ):
            raise ValueError("camera must contain only letters, digits, _, -, or .")
        if codec not in {"jpeg", "raw_bgr8"}:
            raise ValueError("codec must be jpeg or raw_bgr8")
        if codec == "jpeg" and (jpeg_quality is None or not 1 <= jpeg_quality <= 100):
            raise ValueError("jpeg_quality must be from 1 through 100 for jpeg")
        if codec == "raw_bgr8" and jpeg_quality is not None:
            raise ValueError("jpeg_quality is only valid for jpeg")
        with self._lock:
            if camera in self._streams:
                raise ValueError(f"stream already open: {camera}")
            stream = PublishedStream(self, camera, codec, jpeg_quality)
            self._streams[camera] = stream
            self._round_robin.append(camera)
        self.start()
        return stream

    def close(self) -> None:
        self._stop.set()
        self._wake()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None

    def _wake(self) -> None:
        self._wake_event.set()

    def _run(self) -> None:
        context = zmq.Context()
        socket = context.socket(zmq.DEALER)
        socket.setsockopt(zmq.IDENTITY, self._identity)
        socket.setsockopt(zmq.LINGER, 0)
        socket.setsockopt(zmq.IMMEDIATE, 1)
        socket.setsockopt(zmq.SNDHWM, 1)
        socket.setsockopt(zmq.RCVHWM, 10)
        monitor = socket.get_monitor_socket(
            zmq.EVENT_CONNECTED | zmq.EVENT_DISCONNECTED
        )
        socket.connect(self.endpoint)
        poller = zmq.Poller()
        poller.register(socket, zmq.POLLIN)
        poller.register(monitor, zmq.POLLIN)
        try:
            while not self._stop.is_set():
                events = dict(poller.poll(25))
                if monitor in events:
                    self._drain_monitor(monitor)
                if socket in events:
                    self._drain_replies(socket)
                if self.connected:
                    self._send_round(socket)
                self._wake_event.wait(0.01)
                self._wake_event.clear()
            self._send_closes(socket)
        except zmq.ZMQError as exc:
            logger.warning("publisher socket failed: %s", exc)
        finally:
            self._connected.clear()
            self._set_connection_state(False)
            monitor.close(0)
            socket.close(0)
            context.term()

    def _drain_monitor(self, monitor: zmq.Socket) -> None:
        while True:
            try:
                event = recv_monitor_message(monitor, flags=zmq.DONTWAIT)["event"]
            except zmq.Again:
                return
            if event == zmq.EVENT_CONNECTED:
                self._connected.set()
                self._set_connection_state(True)
                logger.info("push connected: ingest=%s", self.endpoint)
            elif event == zmq.EVENT_DISCONNECTED:
                self._connected.clear()
                self._set_connection_state(False)
                logger.warning("push disconnected: ingest=%s", self.endpoint)

    def _set_connection_state(self, connected: bool) -> None:
        with self._lock:
            streams = list(self._streams.values())
        for stream in streams:
            stream._set_connection_state(connected)

    def _send_round(self, socket: zmq.Socket) -> None:
        with self._lock:
            names = list(self._round_robin)
            if self._round_robin:
                self._round_robin.rotate(-1)
            streams = [self._streams[name] for name in names]
        for stream in streams:
            frame = stream._take()
            if frame is None:
                continue
            try:
                socket.send_multipart(
                    [stream._metadata(), frame.header, frame.payload],
                    flags=zmq.DONTWAIT,
                )
                stream._sent(frame)
            except zmq.Again:
                stream._socket_drop()

    def _drain_replies(self, socket: zmq.Socket) -> None:
        while True:
            try:
                payload = socket.recv(flags=zmq.DONTWAIT)
                message = json.loads(payload.decode("utf-8"))
            except zmq.Again:
                return
            except (UnicodeDecodeError, json.JSONDecodeError):
                continue
            if not isinstance(message, dict) or message.get("type") != "ingest_reply":
                continue
            topic = message.get("topic")
            if not isinstance(topic, str) or not topic.endswith("/color"):
                continue
            camera = topic.removesuffix("/color")
            with self._lock:
                stream = self._streams.get(camera)
            if stream is not None:
                stream._reply(message)

    def _send_closes(self, socket: zmq.Socket) -> None:
        with self._lock:
            messages = [stream._close_message() for stream in self._streams.values()]
        for message in messages:
            if message is None:
                continue
            try:
                socket.send(message, flags=zmq.DONTWAIT)
            except zmq.Again:
                return
