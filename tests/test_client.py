import sys
from pathlib import Path

CLIENT_SOURCE = Path(__file__).parents[1] / "example" / "camera-stream-client" / "src"
sys.path.insert(0, str(CLIENT_SOURCE))

from camera_stream_client.cli import parse_args


def test_client_requires_a_stream_endpoint() -> None:
    args = parse_args(["--endpoint=tcp://192.168.5.24:5555"])
    assert args.endpoint == "tcp://192.168.5.24:5555"
    assert args.camera == []
