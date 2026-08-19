import sys
import time
from pathlib import Path

import pytest

CLIENT_SOURCE = Path(__file__).parents[1] / "example" / "camera-stream-client" / "src"
sys.path.insert(0, str(CLIENT_SOURCE))

from camera_stream_client.cli import parse_args
from camera_stream_client.dashboard import VideoWall
from camera_stream_client.protocol import FrameMessage, ProtocolError, parse_message
from camera_stream_client.state import CameraRegistry
from camera_stream_client.transport import client_endpoint


def _header(*, camera: str = "front", sequence: int = 1) -> bytes:
    return (
        "{"
        '"camera":"' + camera + '",'
        '"captured_monotonic_ns":1,'
        '"captured_utc_ns":1,'
        '"codec":"raw_bgr8",'
        '"height":1,'
        '"payload_size":3,'
        '"pixel_format":"bgr8",'
        '"schema_version":1,'
        '"sequence":' + str(sequence) + ","
        '"stream":"color",'
        '"timestamp_source":"host",'
        '"width":1'
        "}"
    ).encode()


def test_parse_frame_validates_public_protocol() -> None:
    message = parse_message([b"front/color", _header(), b"\x01\x02\x03"])

    assert isinstance(message, FrameMessage)
    assert message.camera == "front"
    assert message.header["codec"] == "raw_bgr8"


def test_parse_frame_rejects_topic_header_mismatch() -> None:
    with pytest.raises(ProtocolError, match="do not match"):
        parse_message([b"other/color", _header(), b"\x01\x02\x03"])


def test_registry_tracks_local_overwrite_and_sequence_gap() -> None:
    registry = CameraRegistry(set())
    first = parse_message([b"front/color", _header(sequence=1), b"\x01\x02\x03"])
    third = parse_message([b"front/color", _header(sequence=3), b"\x04\x05\x06"])
    assert isinstance(first, FrameMessage)
    assert isinstance(third, FrameMessage)

    registry.receive(first, time.monotonic_ns(), time.time_ns())
    registry.receive(third, time.monotonic_ns(), time.time_ns())
    registry.consume_latest()
    view = registry.views(time.monotonic_ns(), time.time_ns())[0]

    assert view["metrics"]["local_loss_percent"] == 50.0
    assert view["metrics"]["gap_loss_percent"] == pytest.approx(100 / 3)
    assert view["image"].tolist() == [[[4, 5, 6]]]


def test_cli_and_wildcard_endpoint() -> None:
    args = parse_args(
        [
            "--endpoint=tcp://0.0.0.0:5555",
            "--status-endpoint=tcp://127.0.0.1:5556",
            "--camera=front",
            "--camera=side",
        ]
    )

    assert args.camera == ["front", "side"]
    assert client_endpoint(args.endpoint) == "tcp://127.0.0.1:5555"


def test_chart_range_labels_keep_compact_fps_readable() -> None:
    assert VideoWall._chart_label(30.4) == "30"
    assert VideoWall._chart_label(9.75) == "9.8"


def test_focused_tile_uses_the_full_window() -> None:
    registry = CameraRegistry(set())
    frame = parse_message([b"front/color", _header(), b"\x01\x02\x03"])
    assert isinstance(frame, FrameMessage)
    registry.receive(frame, time.monotonic_ns(), time.time_ns())
    registry.consume_latest()
    view = registry.views(time.monotonic_ns(), time.time_ns())[0]
    wall = VideoWall("tcp://stream", None)
    wall.focused = "front"

    canvas = wall.render([view], {"last_success_age_s": None}, None)

    assert wall._hits[0].rect == (0, 0, 1440, 900)
    assert canvas.shape == (900, 1440, 3)


def test_server_state_label_explains_missing_status_source() -> None:
    unknown = {"server": {}, "stream_state": None}

    assert (
        VideoWall("tcp://stream", None)._server_state_label(unknown) == "srv:DISABLED"
    )
    assert (
        VideoWall("tcp://stream", "tcp://status")._server_state_label(unknown)
        == "srv:WAITING"
    )
    assert (
        VideoWall("tcp://stream", "tcp://status")._server_state_label(
            {"server": {"state": "ONLINE"}, "stream_state": None}
        )
        == "srv:ONLINE"
    )
