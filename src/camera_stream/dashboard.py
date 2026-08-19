from __future__ import annotations

import time
from typing import Any

from rich.align import Align
from rich.console import Group, RenderableType
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text


class Dashboard:
    """Rich dashboard rendered from the in-process supervisor state."""

    _ARROW_COLUMN_WIDTH = 13

    def __init__(self, supervisor: Any, *, refresh_per_second: float = 4.0) -> None:
        self.supervisor = supervisor
        self.refresh_per_second = refresh_per_second
        self.live: Live | None = None
        self.next_update_at = 0.0

    def render(self) -> RenderableType:
        snapshot = self.supervisor.status_snapshot()
        statuses = {item["name"]: item for item in snapshot["cameras"]}
        service = snapshot.get("service", {})
        clients = snapshot.get("clients", [])

        topology = Table.grid(padding=(0, 0))
        topology.add_column()
        topology.add_column(width=self._ARROW_COLUMN_WIDTH, justify="center")
        topology.add_column()
        topology.add_column(width=self._ARROW_COLUMN_WIDTH, justify="center")
        topology.add_column()
        if clients:
            topology.add_column(width=self._ARROW_COLUMN_WIDTH, justify="center")
            topology.add_column()

        row: list[RenderableType] = [
            Align(self._camera_nodes(statuses), vertical="middle"),
            self._arrow("IPC", " PUSH / PULL "),
            Align(self._supervisor_node(service), vertical="middle"),
            self._arrow("ZeroMQ", "XPUB / SUB"),
            Align(self._service_node(service), vertical="middle"),
        ]
        if clients:
            row.extend(
                [
                    self._arrow("PUB", "SUB"),
                    Align(self._client_nodes(clients), vertical="middle"),
                ]
            )
        topology.add_row(*row)

        return Align.center(
            Panel(
                topology,
                # title=f"CAMERA STREAM  •  revision {snapshot['status_revision']}",
                title="CAMERA STREAM",
                subtitle=f"uptime {self._format_uptime(snapshot['service_uptime_s'])}",
                border_style="blue",
                expand=False,
                padding=(0, 0),
            )
        )

    @staticmethod
    def _arrow(top: str, bottom: str) -> Align:
        return Align(
            Text(f"{top}\n>>>>>>\n{bottom}", style="bold cyan", justify="center"),
            align="center",
            vertical="middle",
        )

    def _camera_nodes(self, statuses: dict[str, dict[str, Any]]) -> RenderableType:
        return Group(
            *[
                self._camera_node(camera, statuses[camera.name])
                for camera in self.supervisor.config.cameras
            ]
        )

    def _camera_node(self, camera: Any, status: dict[str, Any]) -> Panel:
        state = str(status["state"])
        state_style = self._state_style(state)
        details = Text()
        details.append(
            f"{camera.driver}  {camera.profile.width}x{camera.profile.height}"
            f" @{camera.profile.fps}\n",
            style="dim",
        )
        details.append(f"capture  {self._fps(status['capture_fps'])}\n", style="green")
        details.append(
            f"to pub   {self._milliseconds(status['last_capture_to_publish_ms'])}\n",
            style="cyan",
        )
        details.append(
            f"ipc      {self._cost_milliseconds(status.get('ipc_cost_ms'))}\n",
            style="dim",
        )
        details.append("drops    ", style="dim")
        details.append(
            f"slot {status['dropped_before_encode']}  ipc {status['dropped_ipc']}",
            style=self._drop_style(
                int(status["dropped_before_encode"]) + int(status["dropped_ipc"])
            ),
        )
        if status["last_error"]:
            details.append(f"\nerror: {status['last_error']}", style="red")
        return Panel(
            details,
            title=f"{camera.name}  [{state}]",
            subtitle=f"cost {self._cost_milliseconds(self._camera_cost_ms(status))}",
            border_style=state_style,
            padding=(0, 1),
        )

    def _supervisor_node(self, service: dict[str, Any]) -> Panel:
        details = Text()
        details.append("frame PULL  HWM: 1\n", style="bold magenta")
        details.append("control ROUTER\n", style="dim")
        details.append(
            f"workers      {service.get('active_worker_count', 0)}/"
            f"{service.get('worker_count', 0)}"
        )
        return Panel(
            details,
            title="SUPERVISOR",
            subtitle=f"cost {self._cost_milliseconds(service.get('last_supervisor_cost_ms'))}",
            border_style="magenta",
            padding=(0, 1),
        )

    def _service_node(self, service: dict[str, Any]) -> Panel:
        details = Text()
        details.append(
            f"PUB  {service.get('stream_pub', '-')}\n",
            style="green",
        )
        details.append(
            "status  PUB snapshot 1s\n",
            style="cyan",
        )
        details.append(
            f"rate    {self._megabits(service.get('stream_bitrate_mbps'))}\n",
            style="bold green",
        )
        details.append(
            f"egress  {self._megabits(service.get('estimated_egress_mbps'))}\n",
            style="cyan",
        )
        details.append(f"clients {service.get('client_count', 0)}", style="dim")
        return Panel(
            details,
            title="SERVICE",
            subtitle=f"cost {self._cost_milliseconds(service.get('last_service_cost_ms'))}",
            border_style="green",
            padding=(0, 1),
        )

    def _client_node(self, client: dict[str, Any]) -> Panel:
        details = Text()
        details.append(f"codec   {client.get('codecs', '-')}\n", style="dim")
        details.append(
            f"est rx  {self._megabits(client.get('estimated_bitrate_mbps'))}\n",
            style="cyan",
        )
        port = client.get("port") or "-"
        details.append(f"peer    {port}/TCP", style="dim")
        return Panel(
            details,
            title=client["ip"],
            subtitle=f"up {self._format_uptime(client['connected_s'])}",
            border_style="cyan",
            padding=(0, 1),
        )

    def _client_nodes(self, clients: list[dict[str, Any]]) -> RenderableType:
        return Group(*[self._client_node(client) for client in clients])

    @staticmethod
    def _state_style(state: str) -> str:
        return {
            "ONLINE": "green",
            "RECOVERING": "yellow",
            "STARTING": "yellow",
            "WAKING": "yellow",
            "IDLE_PENDING": "yellow",
            "SLEEPING": "dim",
            "OFFLINE": "red",
            "CONFIG_ERROR": "red",
        }.get(state, "dim")

    @staticmethod
    def _drop_style(value: int) -> str:
        return "red" if value else "green"

    @staticmethod
    def _camera_cost_ms(status: dict[str, Any]) -> float | None:
        value = status.get("capture_cost_ms")
        return None if value is None else float(value)

    @staticmethod
    def _milliseconds(value: float | None) -> str:
        return "-" if value is None else f"{round(value):.0f} ms"

    @staticmethod
    def _cost_milliseconds(value: float | None) -> str:
        if value is None:
            return "-"
        return f"{value:.2f} ms" if value < 1 else f"{round(value):.0f} ms"

    @staticmethod
    def _fps(value: float) -> str:
        return f"{round(value):.0f} fps"

    @staticmethod
    def _megabits(value: float | None) -> str:
        return "-" if value is None else f"{round(value):.0f} Mbps"

    @staticmethod
    def _format_uptime(value: float) -> str:
        total_seconds = max(0, round(value))
        hours, remainder = divmod(total_seconds, 3600)
        minutes, seconds = divmod(remainder, 60)
        return f"{hours:02d}:{minutes:02d}:{seconds:02d}"

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
