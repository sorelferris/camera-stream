"""Low-latency multi-camera ZeroMQ streaming service and client library."""

from .client.client import CameraStream, Frame, StreamClient, StreamClientError

__all__ = ["CameraStream", "Frame", "StreamClient", "StreamClientError"]

__version__ = "0.3.4"
