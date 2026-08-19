"""Read-only ROS-like topic diagnostics for a running camera-stream service."""

from __future__ import annotations

import argparse
import json
import sys
import time
from collections.abc import Iterable
from typing import Any
from urllib.parse import urlsplit

import zmq

STATUS_PREFIX = b"status/"
SNAPSHOT_TOPIC = b"status/snapshot"


def add_topic_subcommands(subparsers: argparse._SubParsersAction[Any]) -> None:
    """Add ``camera-stream-server topic`` commands to the root CLI parser."""
    topic = subparsers.add_parser("topic", help="inspect a running stream endpoint")
    commands = topic.add_subparsers(dest="topic_command", required=True)

    list_command = commands.add_parser("list", help="list configured image topics")
    _add_endpoint(list_command)
    list_command.add_argument("--verbose", action="store_true", help="include state")
    list_command.add_argument(
        "--timeout", type=float, default=2.0, help="snapshot wait timeout in seconds"
    )

    info_command = commands.add_parser(
        "info", help="show a topic frame header and state"
    )
    _add_endpoint(info_command)
    _add_topic(info_command)
    info_command.add_argument(
        "--timeout", type=float, default=2.0, help="message wait timeout in seconds"
    )

    echo_command = commands.add_parser("echo", help="print frame headers as JSON")
    _add_endpoint(echo_command)
    _add_topic(echo_command)
    _add_count(echo_command)

    hz_command = commands.add_parser("hz", help="report received frame rate")
    _add_endpoint(hz_command)
    _add_topic(hz_command)
    _add_count(hz_command)
    _add_window(hz_command)

    bw_command = commands.add_parser("bw", help="report encoded payload bandwidth")
    _add_endpoint(bw_command)
    _add_topic(bw_command)
    _add_count(bw_command)
    _add_window(bw_command)


def run_topic_command(args: argparse.Namespace) -> int:
    """Execute one read-only topic command."""
    try:
        if args.topic_command == "list":
            return _list_topics(args)
        if args.topic_command == "info":
            return _topic_info(args)
        if args.topic_command == "echo":
            return _echo(args)
        if args.topic_command == "hz":
            return _measure(args, mode="hz")
        if args.topic_command == "bw":
            return _measure(args, mode="bw")
    except KeyboardInterrupt:
        return 0
    raise ValueError(f"unsupported topic command: {args.topic_command!r}")


def _add_endpoint(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--endpoint",
        required=True,
        help="server stream endpoint, e.g. tcp://192.168.5.24:5555",
    )


def _add_topic(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("topic", help="image topic, e.g. base_camera/color")


def _add_count(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--count",
        type=int,
        default=0,
        help="stop after this many frames; 0 runs until interrupted",
    )


def _add_window(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--window", type=float, default=1.0, help="report interval in seconds"
    )


def _list_topics(args: argparse.Namespace) -> int:
    snapshot = _wait_for_snapshot(args.endpoint, args.timeout)
    if snapshot is None:
        return _timeout("status snapshot", args.endpoint)
    cameras = snapshot.get("cameras", [])
    for camera in sorted(cameras, key=lambda item: str(item.get("name", ""))):
        name = camera.get("name") if isinstance(camera, dict) else None
        if not isinstance(name, str) or not name:
            continue
        if args.verbose:
            print(f"{name}/color\t{camera.get('state', 'UNKNOWN')}")
        else:
            print(f"{name}/color")
    return 0


def _topic_info(args: argparse.Namespace) -> int:
    context, socket = _subscribe(args.endpoint, [STATUS_PREFIX, args.topic.encode()])
    snapshot: dict[str, Any] | None = None
    header: dict[str, Any] | None = None
    try:
        deadline = time.monotonic() + _positive(args.timeout, "--timeout")
        while time.monotonic() < deadline and (header is None or snapshot is None):
            parts = _receive(socket, deadline)
            if parts is None:
                break
            if parts[0] == SNAPSHOT_TOPIC and len(parts) == 2:
                snapshot = _decode_object(parts[1])
            elif len(parts) == 3 and parts[0] == args.topic.encode():
                header = _decode_object(parts[1])
    finally:
        socket.close(0)
        context.term()

    camera = args.topic.removesuffix("/color")
    status = _camera_status(snapshot, camera)
    result: dict[str, Any] = {
        "topic": args.topic,
        "message_type": "camera-stream/frame-v1",
        "status": status,
        "header": header,
    }
    print(json.dumps(result, indent=2, sort_keys=True))
    return (
        0
        if header is not None or status is not None
        else _timeout("topic", args.endpoint)
    )


def _echo(args: argparse.Namespace) -> int:
    return _frames(
        args, lambda _parts, header: print(json.dumps(header, sort_keys=True))
    )


def _measure(args: argparse.Namespace, *, mode: str) -> int:
    window_s = _positive(args.window, "--window")
    window_started = time.monotonic()
    frames = 0
    payload_bytes = 0

    def report(final: bool = False) -> None:
        nonlocal window_started, frames, payload_bytes
        elapsed = max(time.monotonic() - window_started, 1e-9)
        if frames:
            if mode == "hz":
                print(f"average rate: {frames / elapsed:.2f} Hz")
            else:
                print(
                    f"average bandwidth: {payload_bytes * 8 / elapsed / 1_000_000:.2f} Mbps"
                )
        if not final:
            window_started = time.monotonic()
            frames = 0
            payload_bytes = 0

    def measure(parts: list[bytes], _header: dict[str, Any]) -> None:
        nonlocal frames, payload_bytes
        frames += 1
        payload_bytes += len(parts[2])
        if time.monotonic() - window_started >= window_s:
            report()

    return _frames(args, measure, on_finish=lambda: report(final=True))


def _frames(
    args: argparse.Namespace,
    consume: Any,
    *,
    on_finish: Any | None = None,
) -> int:
    count = _nonnegative(args.count, "--count")
    context, socket = _subscribe(args.endpoint, [args.topic.encode()])
    received = 0
    try:
        while not count or received < count:
            parts = socket.recv_multipart()
            if len(parts) != 3 or parts[0] != args.topic.encode():
                continue
            consume(parts, _decode_object(parts[1]))
            received += 1
    finally:
        socket.close(0)
        context.term()
    if on_finish is not None:
        on_finish()
    return 0


def _wait_for_snapshot(endpoint: str, timeout: float) -> dict[str, Any] | None:
    context, socket = _subscribe(endpoint, [STATUS_PREFIX])
    try:
        deadline = time.monotonic() + _positive(timeout, "--timeout")
        while True:
            parts = _receive(socket, deadline)
            if parts is None:
                return None
            if parts[0] == SNAPSHOT_TOPIC and len(parts) == 2:
                return _decode_object(parts[1])
    finally:
        socket.close(0)
        context.term()


def _subscribe(
    endpoint: str, topics: Iterable[bytes]
) -> tuple[zmq.Context, zmq.Socket]:
    context = zmq.Context()
    socket = context.socket(zmq.SUB)
    socket.setsockopt(zmq.RCVHWM, 1)
    socket.setsockopt(zmq.LINGER, 0)
    for topic in topics:
        socket.setsockopt(zmq.SUBSCRIBE, topic)
    socket.connect(_client_endpoint(endpoint))
    return context, socket


def _receive(socket: zmq.Socket, deadline: float) -> list[bytes] | None:
    remaining_ms = max(0, int((deadline - time.monotonic()) * 1_000))
    if not socket.poll(remaining_ms, zmq.POLLIN):
        return None
    return socket.recv_multipart()


def _decode_object(payload: bytes) -> dict[str, Any]:
    try:
        value = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("message payload is not JSON") from exc
    if not isinstance(value, dict):
        raise TypeError("message payload must be a JSON object")
    return value


def _camera_status(snapshot: dict[str, Any] | None, name: str) -> dict[str, Any] | None:
    if snapshot is None or not isinstance(snapshot.get("cameras"), list):
        return None
    for camera in snapshot["cameras"]:
        if isinstance(camera, dict) and camera.get("name") == name:
            return camera
    return None


def _client_endpoint(endpoint: str) -> str:
    parsed = urlsplit(endpoint)
    if parsed.scheme != "tcp" or parsed.hostname not in {"0.0.0.0", "::"}:
        return endpoint
    return f"tcp://127.0.0.1:{parsed.port}" if parsed.port is not None else endpoint


def _positive(value: float, name: str) -> float:
    if value <= 0:
        raise ValueError(f"{name} must be greater than zero")
    return value


def _nonnegative(value: int, name: str) -> int:
    if value < 0:
        raise ValueError(f"{name} must not be negative")
    return value


def _timeout(kind: str, endpoint: str) -> int:
    print(f"timed out waiting for {kind} on {endpoint}", file=sys.stderr)
    return 2
