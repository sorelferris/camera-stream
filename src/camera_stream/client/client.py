"""Ergonomic latest-frame-wins subscriptions for camera-stream applications."""

from __future__ import annotations

import queue
import re
import threading
import time
from dataclasses import dataclass
from typing import Any

import numpy as np
import zmq

from .protocol import (
    FrameMessage,
    ProtocolError,
    StatusEvent,
    StatusSnapshot,
    decode_frame,
    parse_message,
)
from .transport import client_endpoint

_TOPIC = re.compile(r"^[A-Za-z0-9_.-]+/color$")
_STOP = object()


class StreamClientError(RuntimeError):
    """The background ZeroMQ receiver could not continue."""


@dataclass(frozen=True)
class Frame:
    """A decoded frame and a copied wire header."""

    image: np.ndarray
    header: dict[str, Any]
    received_monotonic_ns: int
    received_utc_ns: int

    @property
    def sequence(self) -> int:
        return int(self.header["sequence"])

    @property
    def captured_utc_ns(self) -> int:
        return int(self.header["captured_utc_ns"])

    @property
    def age_ms(self) -> float:
        """End-to-end frame age; meaningful across hosts with synchronized clocks."""
        return max(0.0, (time.time_ns() - self.captured_utc_ns) / 1_000_000)


class CameraStream:
    """One camera topic with latest-frame-wins reads and live diagnostics."""

    def __init__(self, client: StreamClient, topic: str) -> None:
        self._client = client
        self.topic = topic
        self.name = topic.removesuffix("/color")
        self._condition = threading.Condition()
        self._frame: Frame | None = None
        self._closed = False
        self._state: str | None = None
        self._error: str | None = None
        self._status: dict[str, Any] = {}
        self._received_frames = 0
        self._overwritten_frames = 0
        self._sequence_gap_frames = 0
        self._invalid_frames = 0
        self._last_sequence: int | None = None
        self._last_received_ns = 0
        self._intervals_ns: list[int] = []

    def read(self, timeout: float | None = None) -> Frame:
        """Wait for and consume the next available frame.

        Each call returns the newest frame received since the prior successful
        call. Intermediate frames are deliberately replaced, never queued.
        ``TimeoutError`` means no newer frame arrived before ``timeout``.
        """
        deadline = None if timeout is None else time.monotonic() + timeout
        with self._condition:
            while self._frame is None:
                self._raise_if_unavailable()
                remaining = None if deadline is None else deadline - time.monotonic()
                if remaining is not None and remaining <= 0:
                    raise TimeoutError(f"timed out waiting for {self.topic}")
                self._condition.wait(remaining)
            frame = self._frame
            self._frame = None
            return frame

    def latest(self) -> Frame | None:
        """Consume the latest available frame without waiting, or return ``None``."""
        with self._condition:
            frame, self._frame = self._frame, None
            return frame

    read_latest = latest

    def wait_for_state(self, state: str, timeout: float | None = None) -> bool:
        """Wait until the server reports ``state``; return ``False`` on timeout."""
        deadline = None if timeout is None else time.monotonic() + timeout
        with self._condition:
            while self._state != state:
                self._raise_if_unavailable()
                remaining = None if deadline is None else deadline - time.monotonic()
                if remaining is not None and remaining <= 0:
                    return False
                self._condition.wait(remaining)
            return True

    @property
    def state(self) -> str | None:
        """Latest server-reported lifecycle state, such as ``ONLINE``."""
        with self._condition:
            return self._state

    @property
    def error(self) -> str | None:
        """Latest server-reported camera error, if any."""
        with self._condition:
            return self._error

    @property
    def status(self) -> dict[str, Any]:
        """Copy of the latest per-camera status record published by the server."""
        with self._condition:
            return dict(self._status)

    @property
    def last_frame(self) -> Frame | None:
        """The newest frame without consuming it, or ``None`` before first receipt."""
        with self._condition:
            return self._frame

    @property
    def is_closed(self) -> bool:
        with self._condition:
            return self._closed

    @property
    def metrics(self) -> dict[str, Any]:
        """A stable snapshot of local receive and drop measurements."""
        with self._condition:
            intervals = self._intervals_ns
            average_fps = (
                None
                if not intervals
                else 1_000_000_000 * len(intervals) / sum(intervals)
            )
            return {
                "received_frames": self._received_frames,
                "dropped_frames": self._overwritten_frames + self._sequence_gap_frames,
                "local_dropped_frames": self._overwritten_frames,
                "sequence_gap_frames": self._sequence_gap_frames,
                "invalid_frames": self._invalid_frames,
                "drop_rate": (
                    0.0
                    if not self._received_frames + self._sequence_gap_frames
                    else (self._overwritten_frames + self._sequence_gap_frames)
                    / (self._received_frames + self._sequence_gap_frames)
                ),
                "local_drop_rate": (
                    0.0
                    if not self._received_frames
                    else self._overwritten_frames / self._received_frames
                ),
                "gap_drop_rate": (
                    0.0
                    if not self._received_frames + self._sequence_gap_frames
                    else self._sequence_gap_frames
                    / (self._received_frames + self._sequence_gap_frames)
                ),
                "last_received_monotonic_ns": self._last_received_ns or None,
                "average_fps": average_fps,
            }

    def unsubscribe(self) -> None:
        """Stop this topic subscription. Further reads raise ``RuntimeError``."""
        self._client.unsubscribe(self.topic)

    close = unsubscribe

    def _receive(self, message: FrameMessage) -> None:
        received_monotonic_ns = time.monotonic_ns()
        try:
            image = decode_frame(message)
        except ProtocolError:
            with self._condition:
                self._invalid_frames += 1
            return
        frame = Frame(
            image=image,
            header=dict(message.header),
            received_monotonic_ns=received_monotonic_ns,
            received_utc_ns=time.time_ns(),
        )
        with self._condition:
            if self._closed:
                return
            if self._frame is not None:
                self._overwritten_frames += 1
            if self._last_received_ns:
                self._intervals_ns.append(
                    received_monotonic_ns - self._last_received_ns
                )
                del self._intervals_ns[:-300]
            if (
                self._last_sequence is not None
                and frame.sequence > self._last_sequence + 1
            ):
                self._sequence_gap_frames += frame.sequence - self._last_sequence - 1
            self._last_sequence = frame.sequence
            self._last_received_ns = received_monotonic_ns
            self._received_frames += 1
            self._frame = frame
            self._condition.notify_all()

    def _apply_status(self, status: dict[str, Any]) -> None:
        with self._condition:
            self._status = dict(status)
            value = status.get("state")
            self._state = value if isinstance(value, str) else self._state
            error = status.get("last_error", status.get("error"))
            self._error = str(error) if error else None
            self._condition.notify_all()

    def _apply_event(self, event: StatusEvent) -> None:
        with self._condition:
            self._state = event.state
            self._error = event.error
            self._status.update({"state": event.state, "last_error": event.error})
            self._condition.notify_all()

    def _close(self) -> None:
        with self._condition:
            self._closed = True
            self._frame = None
            self._condition.notify_all()

    def _raise_if_unavailable(self) -> None:
        if self._closed:
            raise RuntimeError(f"subscription {self.topic!r} is closed")
        error = self._client.error
        if error is not None:
            raise StreamClientError(error)


class StreamClient:
    """Threaded ZeroMQ client for independent, latest-frame-wins camera streams.

    The receiver starts on construction. Use it as a context manager, or call
    :meth:`close` when all subscriptions are no longer needed.
    """

    def __init__(self, endpoint: str) -> None:
        if not endpoint:
            raise ValueError("endpoint must not be empty")
        self.endpoint = client_endpoint(endpoint)
        self._lock = threading.Lock()
        self._streams: dict[str, CameraStream] = {}
        self._commands: queue.Queue[tuple[str, str] | object] = queue.Queue()
        self._ready = threading.Event()
        self._closed = False
        self._error: str | None = None
        self._thread = threading.Thread(
            target=self._run, name="camera-stream-client", daemon=True
        )
        self._thread.start()
        self._ready.wait(timeout=1.0)

    def subscribe(self, topic: str) -> CameraStream:
        """Subscribe to a ``<camera>/color`` topic and return its stream object."""
        if not _TOPIC.fullmatch(topic):
            raise ValueError("topic must have the form '<camera>/color'")
        with self._lock:
            self._raise_if_closed()
            stream = self._streams.get(topic)
            if stream is None:
                stream = CameraStream(self, topic)
                self._streams[topic] = stream
                self._commands.put(("subscribe", topic))
            return stream

    def unsubscribe(self, topic: str) -> None:
        """Stop one topic subscription; silently accept an already absent topic."""
        with self._lock:
            stream = self._streams.pop(topic, None)
            if stream is None:
                return
            stream._close()
            if not self._closed:
                self._commands.put(("unsubscribe", topic))

    @property
    def error(self) -> str | None:
        with self._lock:
            return self._error

    def close(self) -> None:
        """Close every subscription and stop the background receiver."""
        with self._lock:
            if self._closed:
                return
            self._closed = True
            streams = list(self._streams.values())
            self._streams.clear()
            self._commands.put(_STOP)
        for stream in streams:
            stream._close()
        self._thread.join(timeout=1.0)

    def __enter__(self) -> StreamClient:  # noqa: PYI034 - Python 3.10 support
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def _run(self) -> None:
        context = zmq.Context()
        socket = context.socket(zmq.SUB)
        socket.setsockopt(zmq.RCVHWM, 1)
        socket.setsockopt(zmq.LINGER, 0)
        socket.setsockopt(zmq.SUBSCRIBE, b"status/")
        socket.connect(self.endpoint)
        poller = zmq.Poller()
        poller.register(socket, zmq.POLLIN)
        self._ready.set()
        try:
            while True:
                if not self._apply_commands(socket):
                    return
                if socket not in dict(poller.poll(50)):
                    continue
                while True:
                    try:
                        parts = socket.recv_multipart(flags=zmq.DONTWAIT)
                    except zmq.Again:
                        break
                    self._dispatch(parts)
        except zmq.ZMQError as exc:
            with self._lock:
                if not self._closed:
                    self._error = str(exc)
            self._wake_streams()
        finally:
            poller.unregister(socket)
            socket.close(0)
            context.term()
            self._ready.set()

    def _apply_commands(self, socket: zmq.Socket) -> bool:
        while True:
            try:
                command = self._commands.get_nowait()
            except queue.Empty:
                return True
            if command is _STOP:
                return False
            action, topic = command
            option = zmq.SUBSCRIBE if action == "subscribe" else zmq.UNSUBSCRIBE
            socket.setsockopt(option, topic.encode("utf-8"))

    def _dispatch(self, parts: list[bytes]) -> None:
        try:
            message = parse_message(parts)
        except ProtocolError:
            return
        if isinstance(message, FrameMessage):
            with self._lock:
                stream = self._streams.get(message.topic)
            if stream is not None:
                stream._receive(message)
            return
        if isinstance(message, StatusEvent):
            stream = self._stream_for_camera(message.camera)
            if stream is not None:
                stream._apply_event(message)
            return
        if isinstance(message, StatusSnapshot):
            for status in message.snapshot.get("cameras", []):
                if not isinstance(status, dict) or not isinstance(
                    status.get("name"), str
                ):
                    continue
                stream = self._stream_for_camera(status["name"])
                if stream is not None:
                    stream._apply_status(status)

    def _stream_for_camera(self, name: str) -> CameraStream | None:
        with self._lock:
            return self._streams.get(f"{name}/color")

    def _wake_streams(self) -> None:
        with self._lock:
            streams = list(self._streams.values())
        for stream in streams:
            with stream._condition:
                stream._condition.notify_all()

    def _raise_if_closed(self) -> None:
        if self._closed:
            raise RuntimeError("StreamClient is closed")
