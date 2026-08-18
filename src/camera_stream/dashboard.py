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
        statuses = {item["name"]: item for item in snapshot["cameras"]}
        service = snapshot.get("service", {})
        header = Text("CAMERA STREAM  •  SERVER TOPOLOGY", style="bold cyan")
        header.append(
            f"  revision {snapshot['status_revision']}"
            f"  uptime {snapshot['service_uptime_s']:.1f}s"
            f"  aggregate {service.get('aggregate_publish_fps', 0)} fps",
            style="dim",
        )

        topology = Table.grid(expand=True, padding=(0, 1))
        topology.add_column(ratio=3)
        topology.add_column(width=12, justify="center")
        topology.add_column(ratio=2)
        topology.add_column(width=12, justify="center")
        topology.add_column(ratio=2)
        topology.add_row(
            self._camera_nodes(statuses),
            Text("IPC\nPUSH -> PULL\n>>>", style="bold cyan", justify="center"),
            self._supervisor_node(snapshot, service),
            Text("ZeroMQ\nPUB / REP\n>>>", style="bold cyan", justify="center"),
            self._service_node(snapshot, service),
        )

        metrics = self._metrics_table(statuses)
        return Group(
            Panel(header, border_style="cyan"),
            Panel(
                topology, title="Capture -> aggregate -> service", border_style="blue"
            ),
            Panel(metrics, title="Per-camera performance", border_style="green"),
        )

    def _camera_nodes(self, statuses: dict[str, dict[str, Any]]) -> RenderableType:
        nodes = [
            self._camera_node(camera, statuses[camera.name])
            for camera in self.supervisor.config.cameras
        ]
        return Group(*nodes)

    def _camera_node(self, camera: Any, status: dict[str, Any]) -> Panel:
        state = str(status["state"])
        state_style = self._state_style(state)
        details = Text()
        details.append(
            f"{camera.driver}  {camera.profile.width}x{camera.profile.height}"
            f" @{camera.profile.fps}\n",
            style="dim",
        )
        details.append(f"capture  {status['capture_fps']:.1f} fps\n", style="green")
        details.append(
            f"age      {self._milliseconds(self._capture_age_ms(status))}\n",
            style="dim",
        )
        details.append(
            f"to pub   {self._milliseconds(status['last_capture_to_publish_ms'])}\n",
            style="cyan",
        )
        details.append("drops    ", style="dim")
        details.append(
            f"slot {status['dropped_before_encode']}  " f"ipc {status['dropped_ipc']}",
            style=self._drop_style(
                int(status["dropped_before_encode"]) + int(status["dropped_ipc"])
            ),
        )
        if status["last_error"]:
            details.append(f"\nerror: {status['last_error']}", style="red")
        return Panel(
            details,
            title=f"{camera.name}  [{state}]",
            title_align="left",
            border_style=state_style,
            padding=(0, 1),
        )

    def _supervisor_node(
        self, snapshot: dict[str, Any], service: dict[str, Any]
    ) -> Panel:
        details = Text()
        details.append("frame PULL  HWM: 1\n", style="bold magenta")
        details.append("control ROUTER\n", style="dim")
        details.append(f"workers      {service.get('worker_count', 0)}\n")
        details.append(
            f"aggregate    {service.get('aggregate_publish_fps', 0)} fps\n",
            style="green",
        )
        details.append(
            "last to pub  "
            f"{self._milliseconds(service.get('last_publish_latency_ms'))}\n",
            style="cyan",
        )
        details.append(f"revision     {snapshot['status_revision']}", style="dim")
        return Panel(
            details,
            title="SUPERVISOR / AGGREGATE",
            title_align="left",
            border_style="magenta",
            padding=(0, 1),
        )

    def _service_node(self, snapshot: dict[str, Any], service: dict[str, Any]) -> Panel:
        details = Text()
        details.append("stream PUB\n", style="bold green")
        details.append(f"{service.get('stream_pub', '-')}\n", style="dim")
        details.append("status REP\n", style="bold cyan")
        details.append(f"{service.get('status_rep', '-')}\n", style="dim")
        details.append(f"uptime {snapshot['service_uptime_s']:.1f}s", style="magenta")
        return Panel(
            details,
            title="EXTERNAL SERVICE",
            title_align="left",
            border_style="green",
            padding=(0, 1),
        )

    def _metrics_table(self, statuses: dict[str, dict[str, Any]]) -> Table:
        metrics = Table(box=box.SIMPLE_HEAD, expand=True, pad_edge=False)
        for name in (
            "Camera",
            "State",
            "Capture",
            "Published",
            "Capture -> PUB",
            "Slot drops",
            "IPC drops",
            "PUB drops",
            "Last error",
        ):
            metrics.add_column(
                name,
                justify=(
                    "right" if name not in {"Camera", "State", "Last error"} else "left"
                ),
            )
        for camera in self.supervisor.config.cameras:
            status = statuses[camera.name]
            state = str(status["state"])
            metrics.add_row(
                camera.name,
                Text(state, style=self._state_style(state)),
                f"{status['capture_fps']:.1f} fps",
                f"{status['publish_fps']:.1f} fps",
                self._milliseconds(status["last_capture_to_publish_ms"]),
                self._drop_value(int(status["dropped_before_encode"])),
                self._drop_value(int(status["dropped_ipc"])),
                self._drop_value(int(status["dropped_pub"])),
                status["last_error"] or "-",
            )
        return metrics

    @staticmethod
    def _state_style(state: str) -> str:
        return {
            "ONLINE": "green",
            "RECOVERING": "yellow",
            "STARTING": "yellow",
            "OFFLINE": "red",
            "CONFIG_ERROR": "red",
        }.get(state, "dim")

    @staticmethod
    def _drop_style(value: int) -> str:
        return "red" if value else "green"

    def _drop_value(self, value: int) -> Text:
        return Text(str(value), style=self._drop_style(value))

    @staticmethod
    def _milliseconds(value: float | None) -> str:
        return "-" if value is None else f"{value:.1f} ms"

    @staticmethod
    def _capture_age_ms(status: dict[str, Any]) -> float | None:
        captured_utc_ns = int(status["last_capture_utc_ns"])
        if not captured_utc_ns:
            return None
        return max(0.0, (time.time_ns() - captured_utc_ns) / 1_000_000)

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
