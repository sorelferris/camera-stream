import json
import threading
import time

import numpy as np
import pytest
import zmq

from camera_stream.config import IngestPolicyConfig, ServiceConfig
from camera_stream.ingest import (
    IngestError,
    RemoteStreamRegistry,
    parse_ingest_frame,
)
from camera_stream.publisher import StreamPublisher
from camera_stream.supervisor import Supervisor


def frame_parts(
    *, camera: str = "remote", token: str | None = None, lease_token: str | None = None
) -> list[bytes]:
    metadata = {"type": "frame", "ingest_schema_version": 1}
    if token is not None:
        metadata["token"] = token
    if lease_token is not None:
        metadata["lease_token"] = lease_token
    payload = b"abc"
    header = {
        "schema_version": 1,
        "camera": camera,
        "stream": "color",
        "sequence": 1,
        "captured_monotonic_ns": 1,
        "captured_utc_ns": time.time_ns(),
        "timestamp_source": "host",
        "width": 1,
        "height": 1,
        "pixel_format": "bgr8",
        "codec": "jpeg",
        "payload_size": len(payload),
    }
    return [json.dumps(metadata).encode(), json.dumps(header).encode(), payload]


def reply_code(payload: bytes) -> str:
    return str(json.loads(payload.decode())["code"])


def test_first_remote_frame_claims_topic_and_preserves_public_header() -> None:
    policy = IngestPolicyConfig(token="secret")
    registry = RemoteStreamRegistry(policy, set())
    frame = parse_ingest_frame(frame_parts(token="secret"), policy)

    stream, response = registry.accept(b"one", frame, 100)

    assert stream is not None
    assert stream.topic == "remote/color"
    assert response is not None and reply_code(response) == "ACCEPTED"
    assert json.loads(frame.header_bytes.decode())["camera"] == "remote"


def test_local_topics_and_later_remote_claims_are_rejected() -> None:
    policy = IngestPolicyConfig()
    registry = RemoteStreamRegistry(policy, {"local/color"})

    local, local_reply = registry.accept(
        b"one", parse_ingest_frame(frame_parts(camera="local"), policy), 100
    )
    first, accepted = registry.accept(
        b"one", parse_ingest_frame(frame_parts(camera="remote"), policy), 100
    )
    later, later_reply = registry.accept(
        b"two", parse_ingest_frame(frame_parts(camera="remote"), policy), 101
    )

    assert local is None and local_reply is not None
    assert reply_code(local_reply) == "TOPIC_EXISTS"
    assert first is not None and accepted is not None
    assert later is None and later_reply is not None
    assert reply_code(later_reply) == "TOPIC_EXISTS"


def test_lease_expiry_removes_topic_and_stale_lease_can_reclaim_once() -> None:
    policy = IngestPolicyConfig(topic_lease_s=1)
    registry = RemoteStreamRegistry(policy, set())
    first, response = registry.accept(
        b"one", parse_ingest_frame(frame_parts(camera="remote"), policy), 100
    )
    assert first is not None and response is not None
    lease = json.loads(response.decode())["lease_token"]

    assert registry.expire(1_000_000_100) == [first]
    stale, stale_response = registry.accept(
        b"one",
        parse_ingest_frame(frame_parts(camera="remote", lease_token=lease), policy),
        1_000_000_200,
    )

    assert stale is not None
    assert stale_response is not None
    assert reply_code(stale_response) == "ACCEPTED"


def test_invalid_raw_frame_is_rejected_before_topic_claim() -> None:
    policy = IngestPolicyConfig()
    parts = frame_parts()
    header = json.loads(parts[1].decode())
    header.update({"codec": "raw_bgr8", "width": 2, "height": 1})
    parts[1] = json.dumps(header).encode()

    with pytest.raises(IngestError, match="raw payload length"):
        parse_ingest_frame(parts, policy)


def test_stream_publisher_claims_a_remote_topic_through_supervisor() -> None:
    config = ServiceConfig.model_validate(
        {
            "endpoints": {
                "stream_pub": "tcp://127.0.0.1:*",
                "ingest_api": "tcp://127.0.0.1:*",
            },
            "cameras": [],
        }
    )
    supervisor = Supervisor(config)
    thread = threading.Thread(target=supervisor.run, daemon=True)
    thread.start()
    assert supervisor.ingest_router is not None
    endpoint = supervisor.ingest_router.getsockopt_string(zmq.LAST_ENDPOINT)
    try:
        with StreamPublisher(endpoint) as publisher:
            stream = publisher.open_stream(camera="remote")
            deadline = time.monotonic() + 2
            while not publisher.connected and time.monotonic() < deadline:
                time.sleep(0.01)
            assert publisher.connected
            stream.publish(np.zeros((2, 2, 3), dtype=np.uint8))
            while stream.state != "ONLINE" and time.monotonic() < deadline:
                time.sleep(0.01)
            assert stream.state == "ONLINE"
            statuses = supervisor.status_snapshot()["cameras"]
            assert statuses[0]["id"] == "remote:remote/color"
            assert statuses[0]["source"] == "remote"
    finally:
        supervisor.stop_requested = True
        thread.join(timeout=2)
        if not supervisor._shutdown_complete:
            supervisor.shutdown()
