from camera_stream import StreamClient
from camera_stream.client.cli import parse_args


def test_client_requires_a_stream_endpoint() -> None:
    args = parse_args(["--endpoint=tcp://192.168.5.24:5555"])
    assert args.endpoint == "tcp://192.168.5.24:5555"
    assert args.camera == []


def test_stream_client_is_exported_from_the_unified_package() -> None:
    assert StreamClient.__module__ == "camera_stream.client.client"
