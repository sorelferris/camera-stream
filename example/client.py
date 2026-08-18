"""Multi-camera ZeroMQ client demo.

Run the server first, then display every camera in a single OpenCV mosaic:

    uv run python example/client.py --config config.demo.yaml

Press q or Escape in the OpenCV window to exit. Use --no-display on a headless
machine; it still subscribes to every configured stream and prints live rates.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import cv2
import numpy as np
import zmq

from camera_stream.config import ServiceConfig, load_config


def client_endpoint(endpoint: str) -> str:
    """Replace a local wildcard bind address with a usable connect address."""
    parsed = urlsplit(endpoint)
    if parsed.scheme != "tcp" or parsed.hostname not in {"0.0.0.0", "::"}:
        return endpoint
    try:
        port = parsed.port
    except ValueError:
        return endpoint
    return endpoint if port is None else f"tcp://127.0.0.1:{port}"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="camera-stream multi-camera client demo"
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=Path("config.yaml"),
        help="YAML config used to choose cameras and default endpoints",
    )
    parser.add_argument("--stream", help="override the server stream PUB endpoint")
    parser.add_argument("--status", help="override the server status REP endpoint")
    parser.add_argument(
        "--camera",
        action="append",
        help="camera name to display; repeat to select a subset (default: all)",
    )
    parser.add_argument(
        "--no-display",
        action="store_true",
        help="do not open an OpenCV window; print statistics only",
    )
    parser.add_argument(
        "--status-interval",
        type=float,
        default=2.0,
        help="seconds between status requests",
    )
    return parser.parse_args()


class StatusClient:
    def __init__(self, context: zmq.Context, endpoint: str, poller: zmq.Poller) -> None:
        self.context = context
        self.endpoint = endpoint
        self.poller = poller
        self.socket: zmq.Socket | None = None
        self.pending = False
        self.deadline = 0.0
        self.next_request_at = 0.0
        self._connect()

    def _connect(self) -> None:
        if self.socket is not None:
            self.poller.unregister(self.socket)
            self.socket.close(0)
        self.socket = self.context.socket(zmq.REQ)
        self.socket.setsockopt(zmq.LINGER, 0)
        self.socket.connect(self.endpoint)
        self.poller.register(self.socket, zmq.POLLIN)
        self.pending = False

    def request_if_due(self, now: float, interval: float) -> None:
        if self.socket is None:
            return
        if self.pending:
            if now >= self.deadline:
                self._connect()
            return
        if now < self.next_request_at:
            return
        try:
            self.socket.send_json({"op": "get_status"}, flags=zmq.DONTWAIT)
        except zmq.Again:
            self._connect()
            return
        self.pending = True
        self.deadline = now + max(1.0, interval)
        self.next_request_at = now + interval

    def receive_if_ready(self, events: dict[zmq.Socket, int]) -> dict[str, Any] | None:
        if self.socket is None or self.socket not in events:
            return None
        try:
            response = self.socket.recv_json(flags=zmq.DONTWAIT)
        except (zmq.Again, ValueError):
            return None
        self.pending = False
        return response

    def close(self) -> None:
        if self.socket is not None:
            self.poller.unregister(self.socket)
            self.socket.close(0)
            self.socket = None


@dataclass
class CameraView:
    name: str
    target_width: int
    target_height: int
    frames: deque[int] = field(default_factory=lambda: deque(maxlen=240))
    image: np.ndarray | None = None
    last_sequence: int = 0
    last_payload_size: int = 0
    bytes_since_report: int = 0

    def record(
        self, image: np.ndarray, header: dict[str, Any], payload_size: int
    ) -> None:
        self.image = image
        self.last_sequence = int(header.get("sequence", 0))
        self.last_payload_size = payload_size
        self.bytes_since_report += payload_size
        self.frames.append(time.monotonic_ns())

    def fps(self, now_ns: int) -> int:
        cutoff = now_ns - 1_000_000_000
        while self.frames and self.frames[0] < cutoff:
            self.frames.popleft()
        return len(self.frames)


def decode_frame(header: dict[str, Any], payload: bytes) -> np.ndarray:
    codec = header.get("codec")
    width = int(header["width"])
    height = int(header["height"])
    if codec == "jpeg":
        image = cv2.imdecode(np.frombuffer(payload, dtype=np.uint8), cv2.IMREAD_COLOR)
        if image is None:
            raise ValueError("OpenCV could not decode JPEG payload")
        return image
    if codec == "raw_bgr8":
        expected_size = width * height * 3
        if len(payload) != expected_size:
            raise ValueError(
                f"raw payload is {len(payload)} bytes, expected {expected_size}"
            )
        return np.frombuffer(payload, dtype=np.uint8).reshape((height, width, 3))
    raise ValueError(f"unsupported codec: {codec!r}")


def status_by_camera(snapshot: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {
        str(camera["name"]): camera
        for camera in snapshot.get("cameras", [])
        if "name" in camera
    }


def display_tile(view: CameraView, status: dict[str, Any]) -> np.ndarray:
    size = (view.target_width, view.target_height)
    if view.image is None:
        tile = np.full((view.target_height, view.target_width, 3), 36, dtype=np.uint8)
    else:
        tile = cv2.resize(view.image, size, interpolation=cv2.INTER_AREA)
    state = status.get("state", "WAITING")
    state_color = {
        "ONLINE": (70, 210, 70),
        "STARTING": (0, 200, 240),
        "RECOVERING": (0, 200, 240),
        "OFFLINE": (60, 60, 235),
        "CONFIG_ERROR": (60, 60, 235),
    }.get(state, (180, 180, 180))
    cv2.rectangle(tile, (0, 0), (view.target_width, 48), (0, 0, 0), thickness=-1)
    cv2.putText(
        tile,
        view.name,
        (12, 21),
        cv2.FONT_HERSHEY_SIMPLEX,
        0.55,
        (255, 255, 255),
        1,
        cv2.LINE_AA,
    )
    cv2.putText(
        tile,
        f"{state}  RX {view.fps(time.monotonic_ns())} fps  seq {view.last_sequence or '-'}",
        (12, 41),
        cv2.FONT_HERSHEY_SIMPLEX,
        0.45,
        state_color,
        1,
        cv2.LINE_AA,
    )
    return tile


def mosaic(views: list[CameraView], statuses: dict[str, dict[str, Any]]) -> np.ndarray:
    columns = 2 if len(views) > 1 else 1
    rows = math.ceil(len(views) / columns)
    tile_width = max(view.target_width for view in views)
    tile_height = max(view.target_height for view in views)
    tiles = [display_tile(view, statuses.get(view.name, {})) for view in views]
    while len(tiles) < rows * columns:
        tiles.append(np.zeros((tile_height, tile_width, 3), dtype=np.uint8))
    return np.vstack(
        [
            np.hstack(tiles[index : index + columns])
            for index in range(0, len(tiles), columns)
        ]
    )


def selected_cameras(config: ServiceConfig, requested: list[str] | None) -> list[str]:
    known = {camera.name for camera in config.cameras}
    names = requested or [camera.name for camera in config.cameras]
    unknown = set(names) - known
    if unknown:
        raise ValueError(f"unknown cameras: {', '.join(sorted(unknown))}")
    return names


def main() -> int:
    args = parse_args()
    if args.status_interval <= 0:
        print("--status-interval must be positive", file=sys.stderr)
        return 2
    try:
        config = load_config(args.config)
        names = selected_cameras(config, args.camera)
    except Exception as exc:  # noqa: BLE001 - demo must report config validation errors
        print(f"configuration error: {exc}", file=sys.stderr)
        return 2

    camera_config = {camera.name: camera for camera in config.cameras}
    views = [
        CameraView(
            name, camera_config[name].profile.width, camera_config[name].profile.height
        )
        for name in names
    ]
    views_by_name = {view.name: view for view in views}
    stream_endpoint = client_endpoint(args.stream or config.endpoints.stream_pub)
    status_endpoint = client_endpoint(args.status or config.endpoints.status_rep)
    context = zmq.Context()
    stream = context.socket(zmq.SUB)
    stream.setsockopt(zmq.RCVHWM, 1)
    stream.setsockopt(zmq.LINGER, 0)
    stream.connect(stream_endpoint)
    for name in names:
        stream.setsockopt_string(zmq.SUBSCRIBE, f"{name}/color")
    poller = zmq.Poller()
    poller.register(stream, zmq.POLLIN)
    status_client = StatusClient(context, status_endpoint, poller)
    last_report = time.monotonic()
    latest_status: dict[str, Any] = {}
    print(
        f"subscribed to {', '.join(f'{name}/color' for name in names)} via {stream_endpoint}"
    )
    try:
        while True:
            now = time.monotonic()
            status_client.request_if_due(now, args.status_interval)
            events = dict(poller.poll(50))
            status = status_client.receive_if_ready(events)
            if status is not None:
                latest_status = status
                if "error" in status:
                    print(f"status error: {status['error']}", file=sys.stderr)

            if stream in events:
                while True:
                    try:
                        parts = stream.recv_multipart(flags=zmq.DONTWAIT)
                    except zmq.Again:
                        break
                    if len(parts) != 3:
                        continue
                    try:
                        header = json.loads(parts[1].decode("utf-8"))
                        name = str(header["camera"])
                        view = views_by_name[name]
                        view.record(
                            decode_frame(header, parts[2]), header, len(parts[2])
                        )
                    except (
                        KeyError,
                        TypeError,
                        UnicodeDecodeError,
                        json.JSONDecodeError,
                        ValueError,
                    ) as exc:
                        print(f"invalid frame: {exc}", file=sys.stderr)

            statuses = status_by_camera(latest_status)
            if not args.no_display:
                cv2.imshow("camera-stream client", mosaic(views, statuses))
                key = cv2.waitKey(1) & 0xFF
                if key in (27, ord("q")):
                    break

            now = time.monotonic()
            if now - last_report >= 1.0:
                now_ns = time.monotonic_ns()
                reports = []
                for view in views:
                    status = statuses.get(view.name, {})
                    rate = view.bytes_since_report / max(now - last_report, 1e-6) / 1024
                    reports.append(
                        f"{view.name}: {status.get('state', 'WAITING')} "
                        f"rx={view.fps(now_ns)}fps {rate:.0f}KiB/s seq={view.last_sequence or '-'}"
                    )
                    view.bytes_since_report = 0
                print(" | ".join(reports))
                last_report = now
    except KeyboardInterrupt:
        pass
    finally:
        if not args.no_display:
            cv2.destroyAllWindows()
        status_client.close()
        poller.unregister(stream)
        stream.close(0)
        context.term()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
