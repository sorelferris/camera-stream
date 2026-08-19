"""Camera lifecycle state and capacity-one frame storage."""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Any

import numpy as np

from .metrics import CameraMetrics
from .protocol import FrameMessage, StatusEvent, decode_frame

MIN_STALE_NS = 2_000_000_000


@dataclass(frozen=True)
class ReceivedFrame:
    message: FrameMessage
    received_monotonic_ns: int
    received_utc_ns: int


class LatestFrameSlot:
    """A one-element handoff whose producer always wins over an old frame."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._frame: ReceivedFrame | None = None

    def put(self, frame: ReceivedFrame) -> bool:
        with self._lock:
            overwritten = self._frame is not None
            self._frame = frame
            return overwritten

    def take(self) -> ReceivedFrame | None:
        with self._lock:
            frame, self._frame = self._frame, None
            return frame


class CameraState:
    """One stream's frame, server state, and client measurements."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.metrics = CameraMetrics()
        self.slot = LatestFrameSlot()
        self._lock = threading.Lock()
        self._server: dict[str, Any] = {}
        self._stream_state: str | None = None
        self._stream_error: str | None = None
        self._image: np.ndarray | None = None
        self._image_header: dict[str, Any] | None = None

    def receive(
        self, message: FrameMessage, received_ns: int, received_utc_ns: int
    ) -> None:
        frame = ReceivedFrame(message, received_ns, received_utc_ns)
        overwritten = self.slot.put(frame)
        self.metrics.record_received(
            message.header, len(message.payload), received_ns, overwritten=overwritten
        )

    def consume_latest(self) -> None:
        frame = self.slot.take()
        if frame is None:
            return
        decode_started_ns = time.monotonic_ns()
        try:
            image = decode_frame(frame.message)
        except ValueError as exc:
            self.metrics.record_invalid(f"decode: {exc}")
            return
        displayed_ns = time.monotonic_ns()
        decode_ms = (displayed_ns - decode_started_ns) / 1_000_000
        receive_to_display_ms = (displayed_ns - frame.received_monotonic_ns) / 1_000_000
        self.metrics.record_displayed(displayed_ns, decode_ms, receive_to_display_ms)
        self._image = image
        self._image_header = dict(frame.message.header)

    def apply_server_status(self, status: dict[str, Any]) -> None:
        with self._lock:
            self._server = dict(status)

    def apply_stream_status(self, event: StatusEvent) -> None:
        with self._lock:
            self._stream_state = event.state
            self._stream_error = event.error
            # State events are sent immediately; do not wait for the next
            # periodic snapshot before updating the HUD's authoritative view.
            self._server.update({"state": event.state, "last_error": event.error})

    def record_invalid(self, reason: str) -> None:
        self.metrics.record_invalid(reason)

    def view(self, now_monotonic_ns: int, now_utc_ns: int) -> dict[str, Any]:
        metrics = self.metrics.snapshot(now_monotonic_ns)
        last_received_ns = int(metrics["last_received_ns"])
        stale_after_ns = self._stale_after_ns(metrics)
        if not last_received_ns:
            local_state = "WAITING"
            stale_for_s = None
        elif now_monotonic_ns - last_received_ns > stale_after_ns:
            local_state = "STALE"
            stale_for_s = (now_monotonic_ns - last_received_ns) / 1_000_000_000
        else:
            local_state = "LIVE"
            stale_for_s = 0.0
        header = self._image_header or metrics["last_header"]
        frame_age_ms = None
        if header and int(header.get("captured_utc_ns", 0)):
            frame_age_ms = max(
                0.0, (now_utc_ns - int(header["captured_utc_ns"])) / 1_000_000
            )
        with self._lock:
            server = dict(self._server)
            stream_state = self._stream_state
            stream_error = self._stream_error
        return {
            "name": self.name,
            "metrics_ref": self.metrics,
            "local_state": local_state,
            "stale_for_s": stale_for_s,
            "server": server,
            "stream_state": stream_state,
            "stream_error": stream_error,
            "header": header,
            "image": self._image,
            "frame_age_ms": frame_age_ms,
            "metrics": metrics,
        }

    @staticmethod
    def _stale_after_ns(metrics: dict[str, Any]) -> int:
        interval_ms = metrics.get("frame_interval_p50_ms")
        if interval_ms is None:
            return MIN_STALE_NS
        return max(MIN_STALE_NS, int(float(interval_ms) * 3 * 1_000_000))


class CameraRegistry:
    """Owns the camera set and filters it to requested stream names."""

    def __init__(self, requested: set[str]) -> None:
        self.requested = requested
        self._lock = threading.Lock()
        self._states: dict[str, CameraState] = {
            name: CameraState(name) for name in sorted(requested)
        }

    def accepts(self, name: str) -> bool:
        return not self.requested or name in self.requested

    def receive(
        self, message: FrameMessage, received_ns: int, received_utc_ns: int
    ) -> None:
        state = self._ensure(message.camera)
        if state is not None:
            state.receive(message, received_ns, received_utc_ns)

    def apply_stream_status(self, event: StatusEvent) -> None:
        state = self._ensure(event.camera)
        if state is not None:
            state.apply_stream_status(event)

    def apply_status_snapshot(self, snapshot: dict[str, Any]) -> None:
        cameras = snapshot.get("cameras", [])
        if not isinstance(cameras, list):
            return
        for status in cameras:
            if not isinstance(status, dict) or not isinstance(status.get("name"), str):
                continue
            state = self._ensure(status["name"])
            if state is not None:
                state.apply_server_status(status)

    def record_invalid(self, reason: str, camera: str | None = None) -> None:
        if camera:
            state = self._ensure(camera)
            if state is not None:
                state.record_invalid(reason)
            return
        for state in self.states():
            state.record_invalid(reason)

    def consume_latest(self) -> None:
        for state in self.states():
            state.consume_latest()

    def views(self, now_monotonic_ns: int, now_utc_ns: int) -> list[dict[str, Any]]:
        return [
            state.view(now_monotonic_ns, now_utc_ns)
            for state in sorted(self.states(), key=lambda item: item.name)
        ]

    def states(self) -> list[CameraState]:
        with self._lock:
            return list(self._states.values())

    def _ensure(self, name: str) -> CameraState | None:
        if not self.accepts(name):
            return None
        with self._lock:
            state = self._states.get(name)
            if state is None:
                state = CameraState(name)
                self._states[name] = state
            return state
