from __future__ import annotations

import argparse
import json
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import zmq
from rich import box
from rich.console import Console, Group, RenderableType
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from camera_stream.config import ServiceConfig, load_config


def client_endpoint(endpoint: str) -> str:
    """Convert a local wildcard bind address into a usable client target."""
    parsed = urlsplit(endpoint)
    if parsed.scheme != "tcp" or parsed.hostname not in {"0.0.0.0", "::"}:
        return endpoint
    try:
        port = parsed.port
    except ValueError:
        return endpoint
    if port is None:
        return endpoint
    return f"tcp://127.0.0.1:{port}"


@dataclass
class FrameActivity:
    """Per-camera receive statistics; payloads are intentionally never retained."""

    last_sequence: int = 0
    last_received_monotonic_ns: int = 0
    last_captured_utc_ns: int = 0
    last_payload_size: int = 0
    last_codec: str = "-"
    last_dimensions: str = "-"
    received: deque[int] = field(default_factory=lambda: deque(maxlen=240))
    bytes_received: deque[tuple[int, int]] = field(
        default_factory=lambda: deque(maxlen=240)
    )
    sequence_gaps: int = 0

    def record(
        self, header: dict[str, Any], payload_size: int, monotonic_ns: int
    ) -> None:
        sequence = int(header.get("sequence", 0))
        if self.last_sequence and sequence > self.last_sequence + 1:
            self.sequence_gaps += sequence - self.last_sequence - 1
        self.last_sequence = sequence
        self.last_received_monotonic_ns = monotonic_ns
        self.last_captured_utc_ns = int(header.get("captured_utc_ns", 0))
        self.last_payload_size = payload_size
        self.last_codec = str(header.get("codec", "-"))
        self.last_dimensions = f"{header.get('width', '?')}x{header.get('height', '?')}"
        self.received.append(monotonic_ns)
        self.bytes_received.append((monotonic_ns, payload_size))

    def _trim(self, now_ns: int) -> None:
        cutoff = now_ns - 1_000_000_000
        while self.received and self.received[0] < cutoff:
            self.received.popleft()
        while self.bytes_received and self.bytes_received[0][0] < cutoff:
            self.bytes_received.popleft()

    def snapshot(self, now_ns: int | None = None) -> dict[str, Any]:
        now_ns = now_ns or time.monotonic_ns()
        self._trim(now_ns)
        return {
            "rx_fps": len(self.received),
            "rx_mbps": sum(size for _, size in self.bytes_received) * 8 / 1_000_000,
            "last_rx_age_ms": (
                (now_ns - self.last_received_monotonic_ns) / 1_000_000
                if self.last_received_monotonic_ns
                else None
            ),
            "capture_age_ms": (
                (time.time_ns() - self.last_captured_utc_ns) / 1_000_000
                if self.last_captured_utc_ns
                else None
            ),
            "last_sequence": self.last_sequence,
            "last_payload_size": self.last_payload_size,
            "last_codec": self.last_codec,
            "last_dimensions": self.last_dimensions,
            "sequence_gaps": self.sequence_gaps,
        }


class TuiApp:
    def __init__(self, config: ServiceConfig) -> None:
        self.config = config
        self.camera_configs = {camera.name: camera for camera in config.cameras}
        self.activities = {camera.name: FrameActivity() for camera in config.cameras}
        self.status: dict[str, Any] = {"cameras": []}
        self.status_events: deque[str] = deque(maxlen=6)
        self.stream_endpoint = client_endpoint(config.endpoints.stream_pub)
        self.status_endpoint = client_endpoint(config.endpoints.status_rep)
        self.context = zmq.Context()
        self.sub = self.context.socket(zmq.SUB)
        self.sub.setsockopt(zmq.RCVHWM, 1)
        self.sub.setsockopt(zmq.LINGER, 0)
        self.sub.connect(self.stream_endpoint)
        self.sub.setsockopt_string(zmq.SUBSCRIBE, "")
        self.status_req: zmq.Socket | None = None
        self.status_pending = False
        self.status_deadline = 0.0
        self.status_connected = False
        self.status_error: str | None = None
        self.poller = zmq.Poller()
        self.poller.register(self.sub, zmq.POLLIN)
        self.next_status_request = 0.0
        self._connect_status()

    def _connect_status(self) -> None:
        if self.status_req is not None:
            self.poller.unregister(self.status_req)
            self.status_req.close(0)
        self.status_req = self.context.socket(zmq.REQ)
        self.status_req.setsockopt(zmq.LINGER, 0)
        self.status_req.connect(self.status_endpoint)
        self.poller.register(self.status_req, zmq.POLLIN)
        self.status_pending = False

    def close(self) -> None:
        self.poller.unregister(self.sub)
        self.sub.close(0)
        if self.status_req is not None:
            self.poller.unregister(self.status_req)
            self.status_req.close(0)
        self.context.term()

    def _request_status(self, now: float) -> None:
        if self.status_req is None:
            return
        if self.status_pending:
            if now >= self.status_deadline:
                self.status_connected = False
                self.status_error = f"status endpoint timeout: {self.status_endpoint}"
                self._connect_status()
            return
        if now < self.next_status_request:
            return
        try:
            self.status_req.send_json({"op": "get_status"}, flags=zmq.DONTWAIT)
            self.status_pending = True
            self.status_deadline = now + 1.5
            self.next_status_request = now + 1.0
        except zmq.Again:
            self._connect_status()

    def _handle_status(self) -> None:
        if self.status_req is None:
            return
        try:
            response = self.status_req.recv_json(flags=zmq.DONTWAIT)
        except (zmq.Again, ValueError):
            return
        self.status_pending = False
        if "error" in response:
            self.status_connected = False
            self.status_error = str(response["error"])
            self.status_events.append(f"status error: {response['error']}")
            return
        self.status_connected = True
        self.status_error = None
        self.status = response

    def _handle_stream(self) -> None:
        while True:
            try:
                parts = self.sub.recv_multipart(flags=zmq.DONTWAIT)
            except zmq.Again:
                return
            if len(parts) == 2:
                try:
                    event = json.loads(parts[1].decode("utf-8"))
                    self.status_events.append(
                        f"{event.get('camera', '?')}: {event.get('state', '?')}"
                    )
                except (UnicodeDecodeError, json.JSONDecodeError):
                    continue
                continue
            if len(parts) != 3:
                continue
            try:
                header = json.loads(parts[1].decode("utf-8"))
                camera = str(header["camera"])
            except (KeyError, TypeError, UnicodeDecodeError, json.JSONDecodeError):
                continue
            activity = self.activities.get(camera)
            if activity is not None:
                activity.record(header, len(parts[2]), time.monotonic_ns())

    def poll(self, timeout_ms: int = 50) -> None:
        now = time.monotonic()
        self._request_status(now)
        events = dict(self.poller.poll(timeout_ms))
        if self.sub in events:
            self._handle_stream()
        if self.status_req is not None and self.status_req in events:
            self._handle_status()

    def render(self) -> RenderableType:
        now_ns = time.monotonic_ns()
        status_by_name = {
            item.get("name"): item for item in self.status.get("cameras", [])
        }
        header = Text("CAMERA STREAM  •  LIVE TELEMETRY", style="bold cyan")
        summary = (
            f"  revision {self.status.get('status_revision', '-')}"
            f"  uptime {self.status.get('service_uptime_s', '-')}s"
            f"  stream {self.stream_endpoint}"
        )
        header.append(summary, style="dim")
        status_label = "connected" if self.status_connected else "disconnected"
        status_style = "green" if self.status_connected else "yellow"
        header.append(f"  status {status_label}", style=status_style)
        if self.status_error:
            header.append(f" ({self.status_error})", style="red")

        flow = Table.grid(expand=True, padding=(0, 1))
        flow.add_column(style="bold")
        flow.add_column(ratio=1)
        for name, camera in self.camera_configs.items():
            status = status_by_name.get(name, {})
            activity = self.activities[name].snapshot(now_ns)
            state = status.get("state", "WAITING")
            color = {
                "ONLINE": "green",
                "RECOVERING": "yellow",
                "STARTING": "yellow",
                "OFFLINE": "red",
                "CONFIG_ERROR": "red",
            }.get(state, "dim")
            name_cell = Text("● ", style=color)
            name_cell.append(f"{name} [{state}]", style="bold")
            cap = status.get("capture_fps", 0)
            pub = status.get("publish_fps", 0)
            rx = activity["rx_fps"]
            age = activity["capture_age_ms"]
            age_text = "-" if age is None else f"{age:.0f} ms"
            flow.add_row(
                name_cell,
                Text(
                    f"capture {cap:>5} fps  ━━━▶  PUB {pub:>5} fps  ━━━▶  TUI {rx:>5} fps  ({age_text})",
                    style="green" if rx else "yellow",
                ),
            )

        metrics = Table(box=box.SIMPLE_HEAD, expand=True, pad_edge=False)
        metrics.add_column("Camera", style="bold")
        metrics.add_column("State", min_width=13, no_wrap=True)
        metrics.add_column("Profile", justify="right")
        metrics.add_column("Cap FPS", justify="right")
        metrics.add_column("Pub FPS", justify="right")
        metrics.add_column("TUI FPS", justify="right")
        metrics.add_column("Frame age", justify="right")
        metrics.add_column("Seq", justify="right")
        metrics.add_column("Codec", justify="center")
        metrics.add_column("Payload", justify="right")
        metrics.add_column("Drops pre/ipc", justify="right")
        metrics.add_column("Gaps", justify="right")
        for name, camera in self.camera_configs.items():
            status = status_by_name.get(name, {})
            activity = self.activities[name].snapshot(now_ns)
            state = status.get("state", "WAITING")
            state_style = {
                "ONLINE": "green",
                "RECOVERING": "yellow",
                "STARTING": "yellow",
                "OFFLINE": "red",
                "CONFIG_ERROR": "red",
            }.get(state, "dim")
            age = activity["capture_age_ms"]
            age_text = "-" if age is None else f"{age:.0f} ms"
            payload = activity["last_payload_size"]
            payload_text = "-" if not payload else f"{payload / 1024:.0f} KiB"
            drops = f"{status.get('dropped_before_encode', 0)}/{status.get('dropped_ipc', 0)}"
            metrics.add_row(
                name,
                Text(state, style=state_style),
                f"{camera.profile.width}x{camera.profile.height}@{camera.profile.fps}",
                f"{status.get('capture_fps', 0):.1f}",
                f"{status.get('publish_fps', 0):.1f}",
                f"{activity['rx_fps']:.1f}",
                age_text,
                str(activity["last_sequence"] or "-"),
                activity["last_codec"],
                payload_text,
                drops,
                str(activity["sequence_gaps"]),
            )

        event_lines = "\n".join(self.status_events) or "waiting for status events"
        footer = Panel(
            Text(event_lines, style="dim"), title="Recent events", border_style="dim"
        )
        return Group(
            Panel(header, border_style="cyan"),
            Panel(flow, title="Data flow", border_style="blue"),
            Panel(metrics, title="Performance and parameters", border_style="green"),
            footer,
        )

    def run(self, *, refresh: float = 4.0, once: bool = False) -> None:
        console = Console()
        try:
            if once:
                self.poll(200)
                console.print(self.render())
                return
            with Live(
                self.render(), console=console, refresh_per_second=refresh, screen=True
            ) as live:
                while True:
                    self.poll()
                    live.update(self.render(), refresh=True)
        except KeyboardInterrupt:
            return
        finally:
            self.close()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Rich TUI for camera-stream telemetry")
    parser.add_argument(
        "--config", type=Path, required=True, help="YAML service configuration"
    )
    parser.add_argument("--refresh", type=float, default=4.0, help="TUI refresh rate")
    parser.add_argument(
        "--once", action="store_true", help="render one snapshot and exit"
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        config = load_config(args.config)
    except Exception as exc:  # noqa: BLE001 - CLI must report every config/parser error
        Console(stderr=True).print(f"[red]configuration error:[/red] {exc}")
        return 2
    if not 0.5 <= args.refresh <= 20:
        Console(stderr=True).print("[red]--refresh must be between 0.5 and 20[/red]")
        return 2
    TuiApp(config).run(refresh=args.refresh, once=args.once)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
