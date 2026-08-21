"""Low-latency multi-camera ZeroMQ streaming service and client library."""

from .client.client import CameraStream, Frame, StreamClient, StreamClientError
from .publisher import PublishedStream, StreamPublisher

__all__ = [
    "CameraStream",
    "Frame",
    "PublishedStream",
    "StreamClient",
    "StreamClientError",
    "StreamPublisher",
]

__version__ = "0.3.4"
