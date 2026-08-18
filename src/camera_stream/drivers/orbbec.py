from __future__ import annotations

import numpy as np

from camera_stream.config import CameraConfig

from .base import CameraDriver, CameraUnavailable, DriverConfigurationError


class OrbbecCamera(CameraDriver):
    """Orbbec color adapter using the pyorbbecsdk v1 pipeline API."""

    def __init__(self, config: CameraConfig) -> None:
        super().__init__(config)
        self._pipeline = None
        self._ob = None
        self._context = None

    def open(self) -> None:
        try:
            import pyorbbecsdk as ob
        except ImportError as exc:
            raise DriverConfigurationError("pyorbbecsdk is not installed") from exc
        self._ob = ob
        try:
            self._context = ob.Context()
            devices = self._context.query_devices()
            device = devices.get_device_by_serial_number(self.config.device.serial)
            self._pipeline = ob.Pipeline(device)
            stream_config = ob.Config()
            profile = self.config.profile
            stream_config.enable_video_stream(
                ob.OBSensorType.COLOR_SENSOR,
                profile.width,
                profile.height,
                ob.OBFormat.BGR,
                profile.fps,
            )
            self._pipeline.start(stream_config)
        except DriverConfigurationError:
            raise
        except Exception as exc:
            self._pipeline = None
            self._context = None
            raise CameraUnavailable(
                f"cannot start Orbbec {self.config.device.serial}: {exc}"
            ) from exc

    def read(self) -> np.ndarray:
        if self._pipeline is None:
            raise CameraUnavailable("Orbbec pipeline is not open")
        try:
            frames = self._pipeline.wait_for_frames(1000)
            color = frames.get_color_frame()
            if color is None:
                raise CameraUnavailable("Orbbec color frame unavailable")
            image = np.asanyarray(color.get_data())
        except CameraUnavailable:
            raise
        except Exception as exc:
            raise CameraUnavailable(f"Orbbec read failed: {exc}") from exc
        expected = (self.config.profile.height, self.config.profile.width, 3)
        if image.shape != expected:
            raise DriverConfigurationError(
                f"Orbbec returned {image.shape}, expected {expected}"
            )
        return np.ascontiguousarray(image, dtype=np.uint8).copy()

    def close(self) -> None:
        if self._pipeline is not None:
            try:
                self._pipeline.stop()
            finally:
                self._pipeline = None
                self._context = None
