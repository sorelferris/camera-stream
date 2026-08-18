from __future__ import annotations

import time
from typing import Any

from rich import box
from rich.console import Group, RenderableType
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text


class Dashboard:
    """Rich dashboard rendered from the in-process supervisor state."""

    def __init__(self, supervisor: Any, *, refresh_per_second: float = 4.0) -> None:
        self.supervisor = supervisor
        self.refresh_per_second = refresh_per_second
        self.live: Live | None = None
        self.next_update_at = 0.0

    def render(self) -> RenderableType:
        snapshot = self.supervisor.status_snapshot()
        cameras = {item["name"]: item for item in snapshot["cameras"]}
        header = Text("CAMERA STREAM  •  SERVER TELEMETRY", style="bold cyan")
        header.append(
            f"  revision {snapshot['status_revision']}"
            f"  uptime {snapshot['service_uptime_s']:.1f}s"
            f"  stream {self.supervisor.config.endpoints.stream_pub}"
            f"  status {self.supervisor.config.endpoints.status_rep}",
            style="dim",
        )

        flow = Table.grid(expand=True, padding=(0, 1))
        flow.add_column(style="bold")
        flow.add_column(ratio=1)
        for camera in self.supervisor.config.cameras:
            status = cameras[camera.name]
            state = status["state"]
            color = {
                "ONLINE": "green",
                "RECOVERING": "yellow",
                "STARTING": "yellow",
                "OFFLINE": "red",
                "CONFIG_ERROR": "red",
            }.get(state, "dim")
            flow.add_row(
                Text(f"● {camera.name} [{state}]", style=f"{color} bold"),
                Text(
                    f"capture {status['capture_fps']:>5.1f} fps  ━━━▶  "
                    f"PUB {status['publish_fps']:>5.1f} fps  "
                    f"(seq {status['last_sequence'] or '-'})",
                    style="green" if state == "ONLINE" else "yellow",
                ),
            )

        metrics = Table(box=box.SIMPLE_HEAD, expand=True, pad_edge=False)
        for name in (
            "Camera",
            "State",
            "Driver",
            "Profile",
            "Codec",
            "Cap FPS",
            "Pub FPS",
            "Seq",
            "Drops capture/IPC/PUB",
            "Last error",
        ):
            metrics.add_column(
                name,
                justify=(
                    "right"
                    if name not in {"Camera", "State", "Driver", "Codec", "Last error"}
                    else "left"
                ),
            )
        for camera in self.supervisor.config.cameras:
            status = cameras[camera.name]
            state = status["state"]
            state_style = {
                "ONLINE": "green",
                "RECOVERING": "yellow",
                "STARTING": "yellow",
                "OFFLINE": "red",
                "CONFIG_ERROR": "red",
            }.get(state, "dim")
            quality = camera.encoding.jpeg_quality
            codec = camera.encoding.codec
            if quality is not None:
                codec = f"{codec}/{quality}"
            metrics.add_row(
                camera.name,
                Text(state, style=state_style),
                camera.driver,
                f"{camera.profile.width}x{camera.profile.height}@{camera.profile.fps}",
                codec,
                f"{status['capture_fps']:.1f}",
                f"{status['publish_fps']:.1f}",
                str(status["last_sequence"] or "-"),
                f"{status['dropped_before_encode']}/"
                f"{status['dropped_ipc']}/{status['dropped_pub']}",
                status["last_error"] or "-",
            )

        return Group(
            Panel(header, border_style="cyan"),
            Panel(flow, title="Data flow", border_style="blue"),
            Panel(metrics, title="Performance and parameters", border_style="green"),
        )

    def start(self) -> None:
        self.live = Live(
            self.render(),
            refresh_per_second=self.refresh_per_second,
            screen=True,
        )
        self.live.start()
        self.next_update_at = time.monotonic()

    def update(self) -> None:
        if self.live is None or time.monotonic() < self.next_update_at:
            return
        self.live.update(self.render(), refresh=False)
        self.next_update_at = time.monotonic() + 1 / self.refresh_per_second

    def stop(self) -> None:
        if self.live is not None:
            self.live.stop()
            self.live = None
