"""Validation and decoding for the public camera-stream wire protocol."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

import cv2
import numpy as np

SCHEMA_VERSION = 1
SUPPORTED_CODECS = {"jpeg", "raw_bgr8"}


class ProtocolError(ValueError):
    """A malformed or unsupported message received from the stream."""

    def __init__(self, message: str, *, camera: str | None = None) -> None:
        super().__init__(message)
        self.camera = camera


@dataclass(frozen=True)
class FrameMessage:
    topic: str
    header: dict[str, Any]
    payload: bytes

    @property
    def camera(self) -> str:
        return str(self.header["camera"])


@dataclass(frozen=True)
class StatusEvent:
    camera: str
    state: str
    error: str | None


@dataclass(frozen=True)
class StatusSnapshot:
    snapshot: dict[str, Any]


def parse_message(parts: list[bytes]) -> FrameMessage | StatusEvent | StatusSnapshot:
    """Parse one PUB message without decoding its image payload."""
    if not parts:
        raise ProtocolError("message has no topic")
    try:
        topic = parts[0].decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ProtocolError("topic is not UTF-8") from exc

    if len(parts) == 2:
        if topic == "status/snapshot":
            return _parse_status_snapshot(parts[1])
        if topic.startswith("status/camera/"):
            return _parse_status_event(topic, parts[1])
    if len(parts) != 3:
        raise ProtocolError(f"expected 3 image parts, got {len(parts)}")

    try:
        header = json.loads(parts[1].decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProtocolError("header is not valid JSON") from exc
    if not isinstance(header, dict):
        raise ProtocolError("header must be an object")
    _validate_frame(topic, header, parts[2])
    return FrameMessage(topic=topic, header=header, payload=parts[2])


def decode_frame(frame: FrameMessage) -> np.ndarray:
    """Decode a validated JPEG or raw BGR payload into an OpenCV image."""
    codec = frame.header["codec"]
    width = int(frame.header["width"])
    height = int(frame.header["height"])
    if codec == "jpeg":
        image = cv2.imdecode(
            np.frombuffer(frame.payload, dtype=np.uint8), cv2.IMREAD_COLOR
        )
        if image is None:
            raise ProtocolError("OpenCV could not decode JPEG", camera=frame.camera)
        return image
    expected = width * height * 3
    if len(frame.payload) != expected:
        raise ProtocolError(
            f"raw payload is {len(frame.payload)} bytes, expected {expected}",
            camera=frame.camera,
        )
    return np.frombuffer(frame.payload, dtype=np.uint8).reshape((height, width, 3))


def _parse_status_event(topic: str, payload: bytes) -> StatusEvent:
    camera = topic.removeprefix("status/camera/")
    if not camera:
        raise ProtocolError("status event has no camera")
    try:
        event = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProtocolError("status event is not valid JSON", camera=camera) from exc
    if not isinstance(event, dict) or event.get("type") != "camera_state":
        raise ProtocolError("unsupported status event", camera=camera)
    state = event.get("state")
    if not isinstance(state, str):
        raise ProtocolError("status event has no state", camera=camera)
    error = event.get("error")
    return StatusEvent(camera=camera, state=state, error=str(error) if error else None)


def _parse_status_snapshot(payload: bytes) -> StatusSnapshot:
    try:
        snapshot = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProtocolError("status snapshot is not valid JSON") from exc
    if not isinstance(snapshot, dict) or snapshot.get("type") != "snapshot":
        raise ProtocolError("unsupported status snapshot")
    if not isinstance(snapshot.get("cameras"), list):
        raise ProtocolError("status snapshot has no cameras")
    return StatusSnapshot(snapshot=snapshot)


def _validate_frame(topic: str, header: dict[str, Any], payload: bytes) -> None:
    camera = header.get("camera") if isinstance(header.get("camera"), str) else None
    if header.get("schema_version") != SCHEMA_VERSION:
        raise ProtocolError(
            f"unsupported schema_version {header.get('schema_version')!r}",
            camera=camera,
        )
    required_strings = {
        "camera",
        "stream",
        "pixel_format",
        "codec",
        "timestamp_source",
    }
    missing = [
        name for name in required_strings if not isinstance(header.get(name), str)
    ]
    if missing:
        raise ProtocolError(f"header fields must be strings: {', '.join(missing)}")
    if not camera:
        raise ProtocolError("header camera is empty")
    if topic != f"{camera}/color" or header["stream"] != "color":
        raise ProtocolError("topic and color stream header do not match", camera=camera)
    if header["pixel_format"] != "bgr8":
        raise ProtocolError("only bgr8 pixels are supported", camera=camera)
    if header["timestamp_source"] != "host":
        raise ProtocolError("only host timestamps are supported", camera=camera)
    if header["codec"] not in SUPPORTED_CODECS:
        raise ProtocolError(f"unsupported codec {header['codec']!r}", camera=camera)
    for field in (
        "sequence",
        "width",
        "height",
        "payload_size",
        "captured_monotonic_ns",
        "captured_utc_ns",
    ):
        if not isinstance(header.get(field), int):
            raise ProtocolError(f"header {field} must be an integer", camera=camera)
    if header["width"] <= 0 or header["height"] <= 0:
        raise ProtocolError("frame dimensions must be positive", camera=camera)
    if header["payload_size"] != len(payload):
        raise ProtocolError(
            f"payload_size is {header['payload_size']}, actual is {len(payload)}",
            camera=camera,
        )
