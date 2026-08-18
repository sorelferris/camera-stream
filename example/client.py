"""Multi-camera ZeroMQ client demo.

Run the server first, then display every camera in a single OpenCV mosaic:

    uv run python example/client.py --endpoint=tcp://192.168.5.24:5555

Press q or Escape in the OpenCV window to exit. Use --no-display on a headless
machine; it still subscribes to every discovered stream and prints live rates.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

import cv2
import numpy as np
import zmq


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


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="camera-stream multi-camera client demo"
    )
    parser.add_argument(
        "--endpoint",
        required=True,
        help="server stream PUB endpoint, for example tcp://192.168.5.24:5555",
    )
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
    return parser.parse_args(argv)


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


def display_tile(view: CameraView, width: int, height: int) -> np.ndarray:
    size = (width, height)
    if view.image is None:
        tile = np.full((height, width, 3), 36, dtype=np.uint8)
    else:
        tile = cv2.resize(view.image, size, interpolation=cv2.INTER_AREA)
    cv2.rectangle(tile, (0, 0), (width, 48), (0, 0, 0), thickness=-1)
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
        f"RX {view.fps(time.monotonic_ns())} fps  seq {view.last_sequence or '-'}",
        (12, 41),
        cv2.FONT_HERSHEY_SIMPLEX,
        0.45,
        (70, 210, 70),
        1,
        cv2.LINE_AA,
    )
    return tile


def mosaic(views: list[CameraView]) -> np.ndarray:
    columns = 2 if len(views) > 1 else 1
    rows = math.ceil(len(views) / columns)
    tile_width = max(view.target_width for view in views)
    tile_height = max(view.target_height for view in views)
    tiles = [display_tile(view, tile_width, tile_height) for view in views]
    while len(tiles) < rows * columns:
        tiles.append(np.zeros((tile_height, tile_width, 3), dtype=np.uint8))
    return np.vstack(
        [
            np.hstack(tiles[index : index + columns])
            for index in range(0, len(tiles), columns)
        ]
    )


def main() -> int:
    args = parse_args()
    requested = set(args.camera or [])
    views: list[CameraView] = []
    views_by_name: dict[str, CameraView] = {}
    stream_endpoint = client_endpoint(args.endpoint)
    context = zmq.Context()
    stream = context.socket(zmq.SUB)
    stream.setsockopt(zmq.RCVHWM, 1)
    stream.setsockopt(zmq.LINGER, 0)
    stream.connect(stream_endpoint)
    stream.setsockopt_string(zmq.SUBSCRIBE, "")
    poller = zmq.Poller()
    poller.register(stream, zmq.POLLIN)
    last_report = time.monotonic()
    print(f"subscribed to all camera streams via {stream_endpoint}")
    try:
        while True:
            events = dict(poller.poll(50))
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
                        if requested and name not in requested:
                            continue
                        view = views_by_name.get(name)
                        if view is None:
                            view = CameraView(
                                name=name,
                                target_width=int(header["width"]),
                                target_height=int(header["height"]),
                            )
                            views_by_name[name] = view
                            views.append(view)
                            print(f"discovered {name}/color")
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

            if not args.no_display and views:
                cv2.imshow(
                    "camera-stream client",
                    mosaic(sorted(views, key=lambda view: view.name)),
                )
                key = cv2.waitKey(1) & 0xFF
                if key in (27, ord("q")):
                    break

            now = time.monotonic()
            if now - last_report >= 1.0:
                now_ns = time.monotonic_ns()
                reports = []
                for view in sorted(views, key=lambda view: view.name):
                    rate = view.bytes_since_report / max(now - last_report, 1e-6) / 1024
                    reports.append(
                        f"{view.name}: rx={view.fps(now_ns)}fps "
                        f"{rate:.0f}KiB/s seq={view.last_sequence or '-'}"
                    )
                    view.bytes_since_report = 0
                print(" | ".join(reports))
                last_report = now
    except KeyboardInterrupt:
        pass
    finally:
        if not args.no_display:
            cv2.destroyAllWindows()
        poller.unregister(stream)
        stream.close(0)
        context.term()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
