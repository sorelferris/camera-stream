"""Visual debugger and ergonomic latest-frame-wins client for camera-stream."""

from .client import CameraStream, Frame, StreamClient, StreamClientError

__all__ = ["CameraStream", "Frame", "StreamClient", "StreamClientError"]

__version__ = "0.1.8"
