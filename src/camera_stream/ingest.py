"""Private remote-camera ingest protocol and latest-frame registry.

The public PUB protocol remains ``[topic, header, payload]``.  Only the
ROUTER-facing ingest side sees the extra metadata frame and lease credentials.
"""

from __future__ import annotations

import json
import secrets
from collections import deque
from dataclasses import dataclass, field
from typing import Any

from camera_stream.config import IngestPolicyConfig
from camera_stream.protocol import json_bytes

INGEST_SCHEMA_VERSION = 1
SUPPORTED_CODECS = {"jpeg", "raw_bgr8"}


class IngestError(ValueError):
    """A rejected private ingest message with a stable machine-readable code."""

    def __init__(self, code: str, message: str, *, camera: str | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.camera = camera


@dataclass
class RemoteStream:
    topic: str
    name: str
    lease_token: str
    owner_identity: bytes
    width: int
    height: int
    codec: str
    created_ns: int
    last_frame_ns: int
    last_captured_utc_ns: int
    accepted_times_ns: deque[int] = field(default_factory=lambda: deque(maxlen=240))
    bitrate_samples: deque[tuple[int, int]] = field(
        default_factory=lambda: deque(maxlen=120)
    )
    dropped_rate_limit: int = 0
    dropped_pub: int = 0
    last_error: str | None = None


@dataclass(frozen=True)
class IngestFrame:
    metadata: dict[str, Any]
    header: dict[str, Any]
    header_bytes: bytes
    payload: bytes

    @property
    def topic(self) -> str:
        return f"{self.header['camera']}/color"


def parse_ingest_frame(parts: list[bytes], policy: IngestPolicyConfig) -> IngestFrame:
    """Validate an ingress message without decoding JPEG payloads."""
    if len(parts) != 3:
        raise IngestError("FRAME_REJECTED", "expected metadata, header, payload")
    try:
        metadata = json.loads(parts[0].decode("utf-8"))
        header = json.loads(parts[1].decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise IngestError("FRAME_REJECTED", "metadata and header must be JSON") from exc
    if not isinstance(metadata, dict) or not isinstance(header, dict):
        raise IngestError("FRAME_REJECTED", "metadata and header must be objects")
    if metadata.get("type") != "frame":
        raise IngestError("FRAME_REJECTED", "metadata type must be frame")
    if metadata.get("ingest_schema_version") != INGEST_SCHEMA_VERSION:
        raise IngestError("UNSUPPORTED_PROTOCOL", "unsupported ingest schema version")
    _validate_header(header, parts[2], policy)
    return IngestFrame(metadata, header, parts[1], parts[2])


def reply(code: str, *, topic: str | None = None, **values: Any) -> bytes:
    message: dict[str, Any] = {
        "type": "ingest_reply",
        "ingest_schema_version": INGEST_SCHEMA_VERSION,
        "code": code,
    }
    if topic is not None:
        message["topic"] = topic
    message.update(values)
    return json_bytes(message)


class RemoteStreamRegistry:
    """Own remote topic leases, validation state, and low-cost rate metrics."""

    def __init__(self, policy: IngestPolicyConfig, local_topics: set[str]) -> None:
        self.policy = policy
        self.local_topics = local_topics
        self.streams: dict[str, RemoteStream] = {}

    def accept(
        self, identity: bytes, frame: IngestFrame, now_ns: int
    ) -> tuple[RemoteStream | None, bytes | None]:
        topic = frame.topic
        stream = self.streams.get(topic)
        metadata = frame.metadata
        if stream is None:
            if topic in self.local_topics:
                return None, reply("TOPIC_EXISTS", topic=topic)
            if len(self.streams) >= self.policy.max_remote_topics:
                return None, reply(
                    "FRAME_REJECTED", topic=topic, error="topic limit reached"
                )
            owners = {item.owner_identity for item in self.streams.values()}
            if identity not in owners and len(owners) >= self.policy.max_connections:
                return None, reply(
                    "FRAME_REJECTED", topic=topic, error="connection limit reached"
                )
            if (
                self.policy.token is not None
                and metadata.get("token") != self.policy.token
            ):
                return None, reply("AUTH_FAILED", topic=topic)
            token = secrets.token_urlsafe(24)
            stream = RemoteStream(
                topic=topic,
                name=str(frame.header["camera"]),
                lease_token=token,
                owner_identity=identity,
                width=int(frame.header["width"]),
                height=int(frame.header["height"]),
                codec=str(frame.header["codec"]),
                created_ns=now_ns,
                last_frame_ns=now_ns,
                last_captured_utc_ns=int(frame.header["captured_utc_ns"]),
            )
            self.streams[topic] = stream
            accepted_reply: bytes | None = reply(
                "ACCEPTED", topic=topic, lease_token=token
            )
        else:
            lease_token = metadata.get("lease_token")
            pending_claim = lease_token is None and identity == stream.owner_identity
            if lease_token != stream.lease_token and not pending_claim:
                code = (
                    "LEASE_EXPIRED" if isinstance(lease_token, str) else "TOPIC_EXISTS"
                )
                return None, reply(code, topic=topic)
            accepted_reply = None

        if not self._within_rate(stream, now_ns):
            stream.dropped_rate_limit += 1
            return None, accepted_reply
        stream.last_frame_ns = now_ns
        stream.last_captured_utc_ns = int(frame.header["captured_utc_ns"])
        stream.accepted_times_ns.append(now_ns)
        stream.bitrate_samples.append((now_ns, len(frame.payload)))
        return stream, accepted_reply

    def close(self, topic: str, lease_token: str | None) -> RemoteStream | None:
        stream = self.streams.get(topic)
        if stream is None or lease_token != stream.lease_token:
            return None
        return self.streams.pop(topic)

    def expire(self, now_ns: int) -> list[RemoteStream]:
        timeout_ns = int(self.policy.topic_lease_s * 1_000_000_000)
        expired = [
            stream
            for stream in self.streams.values()
            if now_ns - stream.last_frame_ns >= timeout_ns
        ]
        for stream in expired:
            self.streams.pop(stream.topic, None)
        return expired

    def status(self, now_ns: int, now_utc_ns: int) -> list[dict[str, Any]]:
        return [
            self._status(stream, now_ns, now_utc_ns) for stream in self.streams.values()
        ]

    def _within_rate(self, stream: RemoteStream, now_ns: int) -> bool:
        window_ns = 1_000_000_000
        while (
            stream.accepted_times_ns
            and stream.accepted_times_ns[0] < now_ns - window_ns
        ):
            stream.accepted_times_ns.popleft()
        return len(stream.accepted_times_ns) < self.policy.max_fps

    @staticmethod
    def _mbps(samples: deque[tuple[int, int]], now_ns: int) -> float:
        cutoff = now_ns - 1_000_000_000
        while samples and samples[0][0] < cutoff:
            samples.popleft()
        return round(sum(size for _, size in samples) * 8 / 1_000_000, 2)

    def _status(
        self, stream: RemoteStream, now_ns: int, now_utc_ns: int
    ) -> dict[str, Any]:
        age_ms = max(0.0, (now_utc_ns - stream.last_captured_utc_ns) / 1_000_000)
        return {
            "id": f"remote:{stream.topic}",
            "name": stream.name,
            "topic": stream.topic,
            "source": "remote",
            "state": "ONLINE",
            "driver": "remote",
            "codec": stream.codec,
            "width": stream.width,
            "height": stream.height,
            "received_fps": len(stream.accepted_times_ns),
            "ingest_bitrate_mbps": self._mbps(stream.bitrate_samples, now_ns),
            "last_frame_age_ms": round(age_ms, 2),
            "dropped_rate_limit": stream.dropped_rate_limit,
            "dropped_pub": stream.dropped_pub,
            "last_error": stream.last_error,
        }


def _validate_header(
    header: dict[str, Any], payload: bytes, policy: IngestPolicyConfig
) -> None:
    camera = header.get("camera") if isinstance(header.get("camera"), str) else None
    if header.get("schema_version") != 1:
        raise IngestError("FRAME_REJECTED", "unsupported frame schema", camera=camera)
    if (
        not camera
        or not camera.replace("_", "a").replace("-", "a").replace(".", "a").isalnum()
    ):
        raise IngestError("FRAME_REJECTED", "invalid camera name", camera=camera)
    if header.get("stream") != "color" or header.get("pixel_format") != "bgr8":
        raise IngestError(
            "FRAME_REJECTED", "only bgr8 color frames are supported", camera=camera
        )
    if header.get("timestamp_source") != "host":
        raise IngestError(
            "FRAME_REJECTED", "only host timestamps are supported", camera=camera
        )
    if header.get("codec") not in SUPPORTED_CODECS:
        raise IngestError("FRAME_REJECTED", "unsupported codec", camera=camera)
    for name in (
        "sequence",
        "width",
        "height",
        "payload_size",
        "captured_monotonic_ns",
        "captured_utc_ns",
    ):
        if not isinstance(header.get(name), int) or int(header[name]) < 0:
            raise IngestError("FRAME_REJECTED", f"invalid {name}", camera=camera)
    width, height = int(header["width"]), int(header["height"])
    if (
        not width
        or not height
        or width > policy.max_width
        or height > policy.max_height
    ):
        raise IngestError(
            "FRAME_REJECTED", "frame dimensions exceed policy", camera=camera
        )
    if len(payload) > policy.max_payload_bytes or header["payload_size"] != len(
        payload
    ):
        raise IngestError("FRAME_REJECTED", "payload exceeds policy", camera=camera)
    if header["codec"] == "raw_bgr8" and len(payload) != width * height * 3:
        raise IngestError(
            "FRAME_REJECTED",
            "raw payload length does not match dimensions",
            camera=camera,
        )
