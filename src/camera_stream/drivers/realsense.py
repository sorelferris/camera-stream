from __future__ import annotations

import numpy as np

from camera_stream.config import CameraConfig

from .base import CameraDriver, CameraUnavailable, DriverConfigurationError


class RealSenseCamera(CameraDriver):
    def __init__(self, config: CameraConfig) -> None:
        super().__init__(config)
        self._rs = None
        self._pipeline = None

    def open(self) -> None:
        try:
            import pyrealsense2 as rs
        except ImportError as exc:
            raise DriverConfigurationError("pyrealsense2 is not installed") from exc
        self._rs = rs
        self._pipeline = rs.pipeline()
        device = rs.config()
        device.enable_device(self.config.device.serial)
        profile = self.config.profile
        device.enable_stream(
            rs.stream.color, profile.width, profile.height, rs.format.bgr8, profile.fps
        )
        try:
            active = self._pipeline.start(device)
            color = active.get_stream(rs.stream.color).as_video_stream_profile()
            actual = (color.width(), color.height(), color.fps())
            expected = (profile.width, profile.height, profile.fps)
            if actual != expected:
                self._pipeline.stop()
                self._pipeline = None
                raise DriverConfigurationError(
                    f"requested {expected[0]}x{expected[1]}@{expected[2]}, "
                    f"got {actual[0]}x{actual[1]}@{actual[2]}"
                )
        except Exception as exc:
            if isinstance(exc, DriverConfigurationError):
                raise
            self._pipeline = None
            raise CameraUnavailable(
                f"cannot start RealSense {self.config.device.serial}: {exc}"
            ) from exc

    def read(self) -> np.ndarray:
        if self._pipeline is None:
            raise CameraUnavailable("RealSense pipeline is not open")
        try:
            frames = self._pipeline.wait_for_frames(1000)
            color = frames.get_color_frame()
            if not color:
                raise CameraUnavailable("RealSense color frame unavailable")
            image = np.asanyarray(color.get_data())
        except CameraUnavailable:
            raise
        except Exception as exc:
            raise CameraUnavailable(f"RealSense read failed: {exc}") from exc
        expected = (self.config.profile.height, self.config.profile.width, 3)
        if image.shape != expected:
            raise DriverConfigurationError(
                f"RealSense returned {image.shape}, expected {expected}"
            )
        return np.ascontiguousarray(image, dtype=np.uint8).copy()

    def close(self) -> None:
        if self._pipeline is not None:
            try:
                self._pipeline.stop()
            finally:
                self._pipeline = None
