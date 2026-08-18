from __future__ import annotations

import cv2
import numpy as np

from camera_stream.config import CameraConfig

from .base import CameraDriver, CameraUnavailable, DriverConfigurationError


class OpenCVCamera(CameraDriver):
    def __init__(self, config: CameraConfig) -> None:
        super().__init__(config)
        self._capture: cv2.VideoCapture | None = None

    def open(self) -> None:
        capture = cv2.VideoCapture(self.config.device.path, cv2.CAP_V4L2)
        if not capture.isOpened():
            capture.release()
            raise CameraUnavailable(f"cannot open {self.config.device.path}")
        profile = self.config.profile
        capture.set(cv2.CAP_PROP_FRAME_WIDTH, profile.width)
        capture.set(cv2.CAP_PROP_FRAME_HEIGHT, profile.height)
        capture.set(cv2.CAP_PROP_FPS, profile.fps)
        actual_width = round(capture.get(cv2.CAP_PROP_FRAME_WIDTH))
        actual_height = round(capture.get(cv2.CAP_PROP_FRAME_HEIGHT))
        actual_fps = capture.get(cv2.CAP_PROP_FPS)
        if (actual_width, actual_height) != (profile.width, profile.height) or abs(
            actual_fps - profile.fps
        ) > 0.5:
            capture.release()
            raise DriverConfigurationError(
                f"requested {profile.width}x{profile.height}@{profile.fps}, "
                f"got {actual_width}x{actual_height}@{actual_fps:g}"
            )
        self._capture = capture

    def read(self) -> np.ndarray:
        if self._capture is None:
            raise CameraUnavailable("camera is not open")
        ok, image = self._capture.read()
        if not ok or image is None:
            raise CameraUnavailable(f"read failed for {self.config.name}")
        if image.ndim != 3 or image.shape[2] != 3:
            raise DriverConfigurationError(
                "OpenCV camera did not return a 3-channel image"
            )
        return np.ascontiguousarray(image, dtype=np.uint8).copy()

    def close(self) -> None:
        if self._capture is not None:
            self._capture.release()
            self._capture = None
