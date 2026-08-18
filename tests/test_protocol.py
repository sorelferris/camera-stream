import json

import numpy as np

from camera_stream.protocol import RawFrame, frame_header
from camera_stream.worker import LatestFrameSlot


def test_header_is_json_and_declares_codec() -> None:
    frame = RawFrame(np.zeros((2, 3, 3), dtype=np.uint8), 10, 20)
    header = json.loads(
        frame_header(
            camera="cam",
            sequence=4,
            frame=frame,
            width=3,
            height=2,
            codec="jpeg",
            payload_size=7,
        )
    )
    assert header["sequence"] == 4
    assert header["pixel_format"] == "bgr8"
    assert header["codec"] == "jpeg"


def test_latest_slot_replaces_old_frame() -> None:
    slot = LatestFrameSlot()
    first = RawFrame(np.zeros((1, 1, 3), dtype=np.uint8), 1, 1)
    second = RawFrame(np.ones((1, 1, 3), dtype=np.uint8), 2, 2)
    slot.put(first)
    slot.put(second)
    assert slot.take(0).captured_monotonic_ns == 2
    assert slot.replaced() == 1
