from __future__ import annotations

from abc import ABC, abstractmethod

import numpy as np

from camera_stream.config import CameraConfig


class DriverConfigurationError(RuntimeError):
    """The device exists, but cannot satisfy the requested static profile."""


class CameraUnavailable(RuntimeError):
    """The camera is temporarily unavailable and may be retried."""


class CameraDriver(ABC):
    def __init__(self, config: CameraConfig) -> None:
        self.config = config

    @abstractmethod
    def open(self) -> None:
        raise NotImplementedError

    @abstractmethod
    def read(self) -> np.ndarray:
        """Return one copied, contiguous BGR8 frame."""
        raise NotImplementedError

    @abstractmethod
    def close(self) -> None:
        raise NotImplementedError


def create_driver(config: CameraConfig) -> CameraDriver:
    if config.driver == "opencv":
        from .opencv import OpenCVCamera

        return OpenCVCamera(config)
    if config.driver == "realsense":
        from .realsense import RealSenseCamera

        return RealSenseCamera(config)
    if config.driver == "orbbec":
        from .orbbec import OrbbecCamera

        return OrbbecCamera(config)
    raise DriverConfigurationError(f"unsupported driver: {config.driver}")
