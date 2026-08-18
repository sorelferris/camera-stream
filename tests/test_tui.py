import time

from camera_stream.tui import FrameActivity, client_endpoint


def test_client_endpoint_rewrites_wildcard_bind_address() -> None:
    assert client_endpoint("tcp://0.0.0.0:5556") == "tcp://127.0.0.1:5556"
    assert client_endpoint("tcp://[::]:5556") == "tcp://127.0.0.1:5556"
    assert client_endpoint("tcp://10.0.0.4:5556") == "tcp://10.0.0.4:5556"


def test_frame_activity_tracks_rate_payload_and_sequence_gaps() -> None:
    activity = FrameActivity()
    now = time.monotonic_ns()
    activity.record(
        {
            "camera": "cam",
            "sequence": 1,
            "captured_utc_ns": time.time_ns(),
            "width": 640,
            "height": 480,
            "codec": "jpeg",
        },
        2048,
        now,
    )
    activity.record(
        {
            "camera": "cam",
            "sequence": 3,
            "captured_utc_ns": time.time_ns(),
            "width": 640,
            "height": 480,
            "codec": "jpeg",
        },
        1024,
        now + 1_000_000,
    )
    snapshot = activity.snapshot(now + 2_000_000)
    assert snapshot["rx_fps"] == 2
    assert snapshot["rx_mbps"] > 0
    assert snapshot["sequence_gaps"] == 1
    assert snapshot["last_dimensions"] == "640x480"
