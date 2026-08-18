import importlib.util
import sys
from pathlib import Path

client_path = Path(__file__).parents[1] / "example" / "client.py"
client_spec = importlib.util.spec_from_file_location("client_demo", client_path)
assert client_spec is not None
assert client_spec.loader is not None
client = importlib.util.module_from_spec(client_spec)
sys.modules[client_spec.name] = client
client_spec.loader.exec_module(client)


def test_client_requires_a_stream_endpoint() -> None:
    args = client.parse_args(["--endpoint=tcp://192.168.5.24:5555"])
    assert args.endpoint == "tcp://192.168.5.24:5555"
    assert args.camera is None
