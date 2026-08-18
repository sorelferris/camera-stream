from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Any

import numpy as np

SCHEMA_VERSION = 1


@dataclass(frozen=True)
class RawFrame:
    image: np.ndarray
    captured_monotonic_ns: int
    captured_utc_ns: int


def frame_header(
    *,
    camera: str,
    sequence: int,
    frame: RawFrame,
    width: int,
    height: int,
    codec: str,
    payload_size: int,
) -> bytes:
    header: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "camera": camera,
        "stream": "color",
        "sequence": sequence,
        "captured_monotonic_ns": frame.captured_monotonic_ns,
        "captured_utc_ns": frame.captured_utc_ns,
        "timestamp_source": "host",
        "width": width,
        "height": height,
        "pixel_format": "bgr8",
        "codec": codec,
        "payload_size": payload_size,
    }
    return json.dumps(header, separators=(",", ":"), sort_keys=True).encode("utf-8")


def json_bytes(value: dict[str, Any]) -> bytes:
    return json.dumps(value, separators=(",", ":"), sort_keys=True).encode("utf-8")


def now() -> tuple[int, int]:
    return time.monotonic_ns(), time.time_ns()
