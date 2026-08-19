import sys
import threading
import time
from pathlib import Path

import cv2
import numpy as np
import pytest
import zmq

CLIENT_SOURCE = Path(__file__).parents[1] / "example" / "camera-stream-client" / "src"
sys.path.insert(0, str(CLIENT_SOURCE))

from camera_stream_client.cli import parse_args
from camera_stream_client.dashboard import VideoWall
from camera_stream_client.protocol import (
    FrameMessage,
    ProtocolError,
    StatusEvent,
    StatusSnapshot,
    parse_message,
)
from camera_stream_client.state import CameraRegistry
from camera_stream_client.transport import StatusStore, StreamReceiver, client_endpoint


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


def test_parse_single_endpoint_status_messages() -> None:
    event = parse_message(
        [
            b"status/camera/front",
            b'{"type":"camera_state","camera":"front","state":"ONLINE","error":null}',
        ]
    )
    snapshot = parse_message(
        [
            b"status/snapshot",
            b'{"type":"snapshot","schema_version":1,"cameras":[{"name":"front"}]}',
        ]
    )

    assert isinstance(event, StatusEvent)
    assert event.camera == "front"
    assert isinstance(snapshot, StatusSnapshot)
    assert snapshot.snapshot["cameras"][0]["name"] == "front"


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
            "--camera=front",
            "--camera=side",
        ]
    )

    assert args.camera == ["front", "side"]
    assert client_endpoint(args.endpoint) == "tcp://127.0.0.1:5555"


def test_chart_range_labels_keep_compact_fps_readable() -> None:
    assert VideoWall._chart_label(30.4) == "30"
    assert VideoWall._chart_label(9.75) == "9.8"


def test_metric_row_keeps_value_anchors_stable(monkeypatch) -> None:
    wall = VideoWall("tcp://stream")
    calls: list[tuple[str, tuple[int, int]]] = []
    monkeypatch.setattr(
        wall,
        "_text",
        lambda _canvas, value, point, _scale, _color, **_kwargs: calls.append(
            (value, point)
        ),
    )
    canvas = np.zeros((60, 420, 3), dtype=np.uint8)
    fields = [("RX", "9.9 fps"), ("AVG", "9.9 fps"), ("RATE", "1.0 Mbps")]
    wall._draw_metric_row(canvas, fields, 8, 20, 404, 0.4)
    wall._draw_metric_row(
        canvas,
        [("RX", "100.0 fps"), ("AVG", "240.0 fps"), ("RATE", "99.9 Mbps")],
        8,
        40,
        404,
        0.4,
    )

    labels = {"RX", "AVG", "RATE"}
    value_positions = [point[0] for value, point in calls if value not in labels]
    assert value_positions[:3] == value_positions[3:]


def test_chart_statistics_are_drawn_above_the_chart_box(monkeypatch) -> None:
    wall = VideoWall("tcp://stream")
    label_baselines: list[int] = []
    chart_tops: list[int] = []
    monkeypatch.setattr(
        wall,
        "_draw_metric_row",
        lambda _canvas, _fields, _x, baseline, _width, _scale, **_kwargs: (
            label_baselines.append(baseline)
        ),
    )
    original_rectangle = cv2.rectangle

    def record_rectangle(image, first, second, *args, **kwargs):
        chart_tops.append(first[1])
        return original_rectangle(image, first, second, *args, **kwargs)

    monkeypatch.setattr(cv2, "rectangle", record_rectangle)
    wall._draw_chart(
        np.zeros((80, 160, 3), dtype=np.uint8), [28.0, 30.0, 29.0], 10, 10, 120, 40
    )

    assert label_baselines == [21]
    assert chart_tops == [24]


def test_focused_tile_uses_the_full_window() -> None:
    registry = CameraRegistry(set())
    frame = parse_message([b"front/color", _header(), b"\x01\x02\x03"])
    assert isinstance(frame, FrameMessage)
    registry.receive(frame, time.monotonic_ns(), time.time_ns())
    registry.consume_latest()
    view = registry.views(time.monotonic_ns(), time.time_ns())[0]
    wall = VideoWall("tcp://stream")
    wall.focused = "front"

    canvas = wall.render([view], {"last_success_age_s": None}, None)

    assert wall._hits[0].rect == (0, 0, 1440, 900)
    assert canvas.shape == (900, 1440, 3)


def test_server_state_label_explains_missing_status_source() -> None:
    unknown = {"server": {}, "stream_state": None}

    assert VideoWall("tcp://stream")._server_state_label(unknown) == "srv:WAITING"
    assert (
        VideoWall("tcp://stream")._server_state_label(
            {"server": {"state": "ONLINE"}, "stream_state": None}
        )
        == "srv:ONLINE"
    )


def test_immediate_status_event_overrides_an_older_status_snapshot() -> None:
    registry = CameraRegistry({"front"})
    registry.apply_status_snapshot(
        {"cameras": [{"name": "front", "state": "STARTING"}]}
    )
    registry.apply_stream_status(StatusEvent("front", "ONLINE", None))

    view = registry.views(time.monotonic_ns(), time.time_ns())[0]
    assert VideoWall("tcp://stream")._server_state_label(view) == "srv:ONLINE"


def test_discovery_subscribes_to_color_topics_without_using_empty_prefix() -> None:
    class SubscriptionSocket:
        def __init__(self) -> None:
            self.subscriptions: list[bytes] = []

        def setsockopt(self, option: int, value: bytes) -> None:
            assert option == zmq.SUBSCRIBE
            self.subscriptions.append(value)

    registry = CameraRegistry(set())
    receiver = StreamReceiver(
        "tcp://stream", set(), registry, StatusStore(), threading.Event()
    )
    socket = SubscriptionSocket()

    receiver._subscribe_discovered_cameras(
        socket,  # type: ignore[arg-type]
        {"cameras": [{"name": "front"}, {"name": "side"}, {"name": "front"}]},
    )

    assert socket.subscriptions == [b"front/color", b"side/color"]
