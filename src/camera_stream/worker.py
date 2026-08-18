from __future__ import annotations

import json
import queue
import signal
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any

import cv2
import zmq

from camera_stream.config import CameraConfig
from camera_stream.drivers import (
    DriverConfigurationError,
    create_driver,
)
from camera_stream.protocol import RawFrame, frame_header, json_bytes, now


@dataclass
class LatestFrameSlot:
    _frame: RawFrame | None = None
    _replaced: int = 0
    _condition: threading.Condition = field(default_factory=threading.Condition)

    def put(self, frame: RawFrame) -> None:
        with self._condition:
            if self._frame is not None:
                self._replaced += 1
            self._frame = frame
            self._condition.notify()

    def take(self, timeout: float) -> RawFrame | None:
        deadline = time.monotonic() + timeout
        with self._condition:
            while self._frame is None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                self._condition.wait(remaining)
            frame = self._frame
            self._frame = None
            return frame

    def replaced(self) -> int:
        with self._condition:
            return self._replaced


@dataclass
class WorkerMetrics:
    _lock: threading.Lock = field(default_factory=threading.Lock)
    captures: deque[int] = field(default_factory=lambda: deque(maxlen=240))
    last_capture_monotonic_ns: int = 0
    last_capture_utc_ns: int = 0
    ipc_published: int = 0
    ipc_dropped: int = 0

    def captured(self, monotonic_ns: int, utc_ns: int) -> None:
        with self._lock:
            self.captures.append(monotonic_ns)
            self.last_capture_monotonic_ns = monotonic_ns
            self.last_capture_utc_ns = utc_ns

    def ipc_ok(self) -> None:
        with self._lock:
            self.ipc_published += 1

    def ipc_drop(self) -> None:
        with self._lock:
            self.ipc_dropped += 1

    def snapshot(self) -> dict[str, Any]:
        cutoff = time.monotonic_ns() - 1_000_000_000
        with self._lock:
            while self.captures and self.captures[0] < cutoff:
                self.captures.popleft()
            return {
                "capture_fps": len(self.captures),
                "last_capture_monotonic_ns": self.last_capture_monotonic_ns,
                "last_capture_utc_ns": self.last_capture_utc_ns,
                "ipc_published": self.ipc_published,
                "ipc_dropped": self.ipc_dropped,
            }


class CaptureLoop:
    def __init__(
        self, config: CameraConfig, slot: LatestFrameSlot, stop: threading.Event
    ) -> None:
        self.config = config
        self.slot = slot
        self.stop = stop
        self.events: queue.SimpleQueue[dict[str, Any]] = queue.SimpleQueue()
        self.metrics = WorkerMetrics()
        self.thread = threading.Thread(
            target=self._run, name=f"capture-{config.name}", daemon=True
        )

    def start(self) -> None:
        self.thread.start()

    def join(self, timeout: float) -> None:
        self.thread.join(timeout)

    def _event(self, kind: str, **values: Any) -> None:
        self.events.put({"kind": kind, **values})

    def _run(self) -> None:
        attempt = 0
        while not self.stop.is_set():
            driver = None
            try:
                driver = create_driver(self.config)
                driver.open()
            except DriverConfigurationError as exc:
                if driver is not None:
                    driver.close()
                self._event(
                    "state",
                    state="CONFIG_ERROR",
                    error=str(exc),
                    reconnect_attempt=attempt,
                )
                return
            except Exception as exc:  # noqa: BLE001 - SDK errors are vendor-specific
                if driver is not None:
                    driver.close()
                attempt += 1
                self._event(
                    "state", state="OFFLINE", error=str(exc), reconnect_attempt=attempt
                )
                if self.stop.wait(min(30.0, 2 ** min(attempt - 1, 5))):
                    return
                self._event(
                    "state",
                    state="RECOVERING",
                    error=str(exc),
                    reconnect_attempt=attempt,
                )
                continue

            attempt = 0
            first_frame_after_open = True
            try:
                while not self.stop.is_set():
                    image = driver.read()
                    monotonic_ns, utc_ns = now()
                    self.slot.put(RawFrame(image, monotonic_ns, utc_ns))
                    self.metrics.captured(monotonic_ns, utc_ns)
                    if first_frame_after_open:
                        self._event(
                            "state",
                            state="ONLINE",
                            error=None,
                            reconnect_attempt=0,
                        )
                        self._event(
                            "capture",
                            captured_monotonic_ns=monotonic_ns,
                            captured_utc_ns=utc_ns,
                        )
                        first_frame_after_open = False
            except Exception as exc:  # noqa: BLE001 - SDK errors are vendor-specific
                if not self.stop.is_set():
                    self._event(
                        "state",
                        state="OFFLINE",
                        error=str(exc),
                        reconnect_attempt=attempt + 1,
                    )
            finally:
                driver.close()

            if not self.stop.is_set():
                attempt += 1
                self._event("state", state="RECOVERING", reconnect_attempt=attempt)
                if self.stop.wait(min(30.0, 2 ** min(attempt - 1, 5))):
                    return


def _send_control(socket: zmq.Socket, message: dict[str, Any]) -> None:
    try:
        socket.send(json_bytes(message), flags=zmq.DONTWAIT)
    except zmq.Again:
        pass


def run_worker(
    config_data: dict[str, Any], frame_endpoint: str, control_endpoint: str, stop: Any
) -> None:
    # The supervisor owns terminal signals and shuts workers down through stop.
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    config = CameraConfig.model_validate(config_data)
    context = zmq.Context()
    data_socket = context.socket(zmq.PUSH)
    data_socket.setsockopt(zmq.SNDHWM, 1)
    data_socket.setsockopt(zmq.LINGER, 0)
    data_socket.connect(frame_endpoint)
    control_socket = context.socket(zmq.DEALER)
    identity = f"{config.name}:{time.monotonic_ns()}".encode()
    control_socket.setsockopt(zmq.IDENTITY, identity)
    control_socket.setsockopt(zmq.SNDHWM, 10)
    control_socket.setsockopt(zmq.RCVHWM, 10)
    control_socket.setsockopt(zmq.LINGER, 0)
    control_socket.connect(control_endpoint)

    slot = LatestFrameSlot()
    capture = CaptureLoop(config, slot, stop)
    capture.start()
    poller = zmq.Poller()
    poller.register(control_socket, zmq.POLLIN)
    sequence = 0
    last_heartbeat = 0.0
    try:
        _send_control(
            control_socket,
            {"type": "hello", "camera": config.name, "pid": __import__("os").getpid()},
        )
        while not stop.is_set():
            for event in iter_queue(capture.events):
                event["type"] = event.pop("kind")
                event["camera"] = config.name
                _send_control(control_socket, event)

            if dict(poller.poll(0)):
                try:
                    command = json.loads(control_socket.recv().decode("utf-8"))
                    if command.get("type") == "stop":
                        stop.set()
                except (zmq.Again, json.JSONDecodeError, UnicodeDecodeError):
                    pass

            if time.monotonic() >= last_heartbeat:
                metrics = capture.metrics.snapshot()
                _send_control(
                    control_socket,
                    {
                        "type": "heartbeat",
                        "camera": config.name,
                        "pid": __import__("os").getpid(),
                        "metrics": {
                            **metrics,
                            "dropped_before_encode": slot.replaced(),
                        },
                    },
                )
                last_heartbeat = time.monotonic() + 1.0

            frame = slot.take(0.02)
            if frame is None:
                continue
            if config.encoding.codec == "jpeg":
                ok, encoded = cv2.imencode(
                    ".jpg",
                    frame.image,
                    [int(cv2.IMWRITE_JPEG_QUALITY), config.encoding.jpeg_quality or 85],
                )
                if not ok:
                    _send_control(
                        control_socket,
                        {
                            "type": "error",
                            "camera": config.name,
                            "error": "JPEG encoding failed",
                        },
                    )
                    continue
                payload = encoded.tobytes()
            else:
                payload = frame.image.tobytes(order="C")
            sequence += 1
            header = frame_header(
                camera=config.name,
                sequence=sequence,
                frame=frame,
                width=config.profile.width,
                height=config.profile.height,
                codec=config.encoding.codec,
                payload_size=len(payload),
            )
            try:
                data_socket.send_multipart([header, payload], flags=zmq.DONTWAIT)
                capture.metrics.ipc_ok()
            except zmq.Again:
                capture.metrics.ipc_drop()
    finally:
        stop.set()
        capture.join(2.0)
        control_socket.close(0)
        data_socket.close(0)
        context.term()


def iter_queue(events: queue.SimpleQueue[dict[str, Any]]):
    while True:
        try:
            yield events.get_nowait()
        except queue.Empty:
            return
