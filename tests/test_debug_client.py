import threading
import time

import cv2
import numpy as np
import pytest
import zmq

from camera_stream.client.cli import parse_args
from camera_stream.client.client import CameraStream, Frame, StreamClient
from camera_stream.client.dashboard import VideoWall
from camera_stream.client.protocol import (
    FrameMessage,
    ProtocolError,
    StatusEvent,
    StatusRemoved,
    StatusSnapshot,
    parse_message,
)
from camera_stream.client.state import CameraRegistry
from camera_stream.client.transport import StatusStore, StreamReceiver, client_endpoint


class _ClientStub:
    error: str | None = None

    def __init__(self) -> None:
        self.unsubscribed: list[str] = []

    def unsubscribe(self, topic: str) -> None:
        self.unsubscribed.append(topic)


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


def test_parse_remote_stream_removal_event() -> None:
    removed = parse_message(
        [
            b"status/removed",
            b'{"type":"stream_removed","topic":"remote/color","source":"remote"}',
        ]
    )

    assert isinstance(removed, StatusRemoved)
    assert removed.topic == "remote/color"


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


def test_four_camera_grid_prefers_two_by_two_layout(monkeypatch) -> None:
    wall = VideoWall("tcp://stream")
    views = [
        {
            "name": f"camera-{index}",
            "image": np.zeros((480, 640, 3), dtype=np.uint8),
            "header": {"width": 640, "height": 480},
        }
        for index in range(4)
    ]
    drawn: list[tuple[int, int, int, int]] = []
    monkeypatch.setattr(
        wall,
        "_draw_tile",
        lambda _canvas, _view, x, y, width, height: drawn.append((x, y, width, height)),
    )

    wall._draw_grid(np.zeros((900, 1440, 3), dtype=np.uint8), views)

    assert len(drawn) == 4
    assert {rect[0] for rect in drawn} == {8, 724}
    assert {rect[1] for rect in drawn} == {62, 477}
    assert all(rect[2] == 708 and rect[3] == 407 for rect in drawn)


def test_grid_shape_adapts_camera_count() -> None:
    views = [{"image": np.zeros((480, 640, 3), dtype=np.uint8)} for _ in range(5)]

    assert VideoWall._grid_shape(views[:4], 1440, 846, gap=8) == (2, 2)
    assert VideoWall._grid_shape(views, 1440, 846, gap=8) == (2, 3)


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


def test_public_camera_stream_keeps_only_the_newest_frame() -> None:
    client = _ClientStub()
    camera = CameraStream(client, "front/color")  # type: ignore[arg-type]
    first = parse_message([b"front/color", _header(sequence=1), b"\x01\x02\x03"])
    third = parse_message([b"front/color", _header(sequence=3), b"\x04\x05\x06"])
    assert isinstance(first, FrameMessage)
    assert isinstance(third, FrameMessage)

    camera._receive(first)
    camera._receive(third)
    latest = camera.latest()
    assert latest is not None
    assert latest.sequence == 3
    assert camera.last_frame is latest

    frame = camera.read(block=False)

    assert frame is latest
    assert frame.sequence == 3
    assert frame.image.tolist() == [[[4, 5, 6]]]
    assert camera.read(block=False) is frame
    assert camera.read(timeout=0).sequence == 3
    assert camera.read(block=False) is frame
    assert camera.latest() is frame
    assert camera.metrics["dropped_frames"] == 2
    assert camera.metrics["local_dropped_frames"] == 1
    assert camera.metrics["sequence_gap_frames"] == 1
    assert camera.metrics["drop_rate"] == pytest.approx(2 / 3)


def test_public_camera_stream_rejects_nonblocking_timeout() -> None:
    camera = CameraStream(_ClientStub(), "front/color")  # type: ignore[arg-type]

    with pytest.raises(ValueError, match="block=True"):
        camera.read(block=False, timeout=0)


def test_camera_stream_warm_up_retains_its_first_frame() -> None:
    camera = CameraStream(_ClientStub(), "front/color")  # type: ignore[arg-type]
    first = parse_message([b"front/color", _header(), b"\x01\x02\x03"])
    assert isinstance(first, FrameMessage)

    camera._receive(first)
    frame = camera.warm_up(timeout=0)

    assert frame.sequence == 1
    assert camera.read(block=False) is frame


def test_stream_client_subscribe_warms_up_by_default(monkeypatch) -> None:
    calls: list[float | None] = []

    def warm_up(self: CameraStream, timeout: float | None = None) -> Frame:
        calls.append(timeout)
        return Frame(
            image=np.zeros((1, 1, 3), dtype=np.uint8),
            header={},
            received_monotonic_ns=0,
            received_utc_ns=0,
        )

    monkeypatch.setattr(CameraStream, "warm_up", warm_up)
    with StreamClient("tcp://127.0.0.1:1") as client:
        client.subscribe("front/color", warm_up_timeout=0.5)
        client.subscribe("side/color", warm_up=False)

    assert calls == [0.5]


def test_public_camera_stream_status_wait_and_close() -> None:
    client = _ClientStub()
    camera = CameraStream(client, "front/color")  # type: ignore[arg-type]

    assert not camera.wait_for_state("ONLINE", timeout=0)
    camera._apply_event(StatusEvent("front", "ONLINE", None))
    assert camera.wait_for_state("ONLINE", timeout=0)
    assert camera.state == "ONLINE"
    camera.unsubscribe()

    assert client.unsubscribed == ["front/color"]
