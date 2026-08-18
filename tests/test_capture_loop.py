import queue
import threading
import time

import numpy as np

from camera_stream.config import CameraConfig
from camera_stream.drivers.base import CameraUnavailable
from camera_stream.worker import CaptureLoop, LatestFrameSlot


class FakeDriver:
    reads = 0

    def __init__(self, config: CameraConfig) -> None:
        self.config = config

    def open(self) -> None:
        return None

    def read(self) -> np.ndarray:
        FakeDriver.reads += 1
        if FakeDriver.reads > 3:
            raise CameraUnavailable("simulated disconnect")
        return np.full((2, 3, 3), FakeDriver.reads, dtype=np.uint8)

    def close(self) -> None:
        return None


def config() -> CameraConfig:
    return CameraConfig.model_validate(
        {
            "name": "fake",
            "driver": "opencv",
            "device": {"path": "/dev/video-fake"},
            "profile": {"width": 3, "height": 2, "fps": 30},
            "encoding": {"codec": "jpeg", "jpeg_quality": 80},
        }
    )


def test_capture_loop_reports_disconnect_and_keeps_latest_frame(monkeypatch) -> None:
    FakeDriver.reads = 0
    monkeypatch.setattr("camera_stream.worker.create_driver", FakeDriver)
    stop = threading.Event()
    slot = LatestFrameSlot()
    loop = CaptureLoop(config(), slot, stop)
    loop.start()
    deadline = time.monotonic() + 1
    events = []
    while time.monotonic() < deadline and not events:
        while True:
            try:
                events.append(loop.events.get_nowait())
            except queue.Empty:
                break
        time.sleep(0.01)
    stop.set()
    loop.join(1)
    assert any(event.get("state") == "ONLINE" for event in events)
    assert any(event.get("state") == "OFFLINE" for event in events)
    frame = slot.take(0)
    assert frame is not None
    assert int(frame.image[0, 0, 0]) == 3
