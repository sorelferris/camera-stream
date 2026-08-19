"""OpenCV video wall and in-frame diagnostics HUD."""

from __future__ import annotations

import math
import time
from dataclasses import dataclass
from typing import Any

import cv2
import numpy as np

WINDOW_TITLE = "CAMERA STREAM CLIENT"
HEADER_HEIGHT = 46
BACKGROUND = (22, 26, 30)
PANEL = (31, 37, 43)
TEXT = (230, 235, 238)
MUTED = (151, 164, 176)
GREEN = (88, 207, 120)
YELLOW = (63, 201, 232)
RED = (79, 91, 236)
CYAN = (226, 188, 71)


@dataclass(frozen=True)
class TileHit:
    name: str
    rect: tuple[int, int, int, int]


class VideoWall:
    """Single resizable OpenCV window with grid and focus modes."""

    def __init__(self, stream_endpoint: str, status_endpoint: str | None) -> None:
        self.stream_endpoint = stream_endpoint
        self.status_endpoint = status_endpoint
        self.diagnostics = False
        self.focused: str | None = None
        self._hits: list[TileHit] = []
        self._notice: tuple[str, int] | None = None
        self._last_size = (1440, 900)

    def open(self) -> None:
        cv2.namedWindow(WINDOW_TITLE, cv2.WINDOW_NORMAL)
        cv2.resizeWindow(WINDOW_TITLE, *self._last_size)
        cv2.setMouseCallback(WINDOW_TITLE, self._on_mouse)

    def close(self) -> None:
        cv2.destroyWindow(WINDOW_TITLE)

    def render(
        self,
        views: list[dict[str, Any]],
        status: dict[str, Any],
        receiver_error: str | None,
    ) -> np.ndarray:
        width, height = self._window_size()
        canvas = np.full((height, width, 3), BACKGROUND, dtype=np.uint8)
        if self.focused:
            focused_view = next(
                (view for view in views if view["name"] == self.focused), None
            )
            if focused_view is None:
                self.focused = None
            else:
                self._hits = self._draw_focus(canvas, focused_view)
                self._draw_notice(canvas)
                return canvas
        self._draw_global_bar(canvas, views, status, receiver_error)
        self._hits = self._draw_grid(canvas, views)
        self._draw_notice(canvas)
        return canvas

    def handle_key(self, key: int) -> str | None:
        if key in (27, ord("q"), ord("Q")):
            return "quit"
        if key == 9:
            self.diagnostics = not self.diagnostics
            self.notice("Diagnostics on" if self.diagnostics else "Compact HUD")
        elif key in (ord("s"), ord("S")):
            return "screenshot"
        elif key in (ord("e"), ord("E")):
            return "export"
        elif key in (13, 10) and self.focused:
            self.focused = None
        return None

    def notice(self, message: str, seconds: float = 2.5) -> None:
        self._notice = (message, time.monotonic_ns() + int(seconds * 1_000_000_000))

    def _window_size(self) -> tuple[int, int]:
        try:
            _, _, width, height = cv2.getWindowImageRect(WINDOW_TITLE)
        except cv2.error:
            return self._last_size
        if width >= 320 and height >= 240:
            self._last_size = (width, height)
        return self._last_size

    def _draw_global_bar(
        self,
        canvas: np.ndarray,
        views: list[dict[str, Any]],
        status: dict[str, Any],
        receiver_error: str | None,
    ) -> None:
        canvas[:HEADER_HEIGHT] = PANEL
        rate = sum(float(view["metrics"]["bitrate_mbps"]) for view in views)
        status_text = self._status_text(status)
        active = sum(1 for view in views if view["local_state"] == "LIVE")
        title = "CAMERA STREAM CLIENT"
        details = (
            f"stream {self.stream_endpoint}  |  {status_text}  |  "
            f"{active}/{len(views)} live  |  {rate:.1f} Mbps"
        )
        self._text(canvas, title, (14, 20), 0.55, TEXT, thickness=1)
        controls = "Tab HUD   S snapshot   E export   Q quit"
        control_scale = 0.42 if canvas.shape[1] >= 760 else 0.34
        controls = self._ellipsize(controls, canvas.shape[1] // 3, control_scale)
        control_width = self._text_width(controls, control_scale)
        self._text(
            canvas,
            self._ellipsize(details, canvas.shape[1] - control_width - 42, 0.42),
            (14, 38),
            0.42,
            MUTED,
        )
        self._right_text(
            canvas, controls, canvas.shape[1] - 14, 30, control_scale, MUTED
        )
        if receiver_error:
            self._right_text(canvas, "SUB ERROR", canvas.shape[1] - 14, 16, 0.42, RED)

    def _draw_grid(
        self, canvas: np.ndarray, views: list[dict[str, Any]]
    ) -> list[TileHit]:
        top = HEADER_HEIGHT + 8
        available_height = canvas.shape[0] - top - 8
        if not views:
            self._center_text(
                canvas,
                "Waiting for camera streams",
                canvas.shape[1] // 2,
                top + available_height // 2,
                0.75,
                MUTED,
            )
            return []
        count = len(views)
        if count == 1:
            rows, columns = 1, 1
        else:
            columns = math.ceil(
                math.sqrt(count * canvas.shape[1] / max(available_height, 1))
            )
            columns = max(1, min(count, columns))
            rows = math.ceil(count / columns)
        gap = 8
        cell_width = max(1, (canvas.shape[1] - gap * (columns + 1)) // columns)
        cell_height = max(1, (available_height - gap * (rows + 1)) // rows)
        hits: list[TileHit] = []
        for index, view in enumerate(views):
            row, column = divmod(index, columns)
            x = gap + column * (cell_width + gap)
            y = top + gap + row * (cell_height + gap)
            self._draw_tile(canvas, view, x, y, cell_width, cell_height)
            hits.append(TileHit(view["name"], (x, y, cell_width, cell_height)))
        return hits

    def _draw_focus(self, canvas: np.ndarray, view: dict[str, Any]) -> list[TileHit]:
        """Render one selected tile over the full available window."""
        height, width = canvas.shape[:2]
        self._draw_tile(canvas, view, 0, 0, width, height)
        return [TileHit(view["name"], (0, 0, width, height))]

    def _draw_tile(
        self,
        canvas: np.ndarray,
        view: dict[str, Any],
        x: int,
        y: int,
        width: int,
        height: int,
    ) -> None:
        canvas[y : y + height, x : x + width] = (16, 20, 24)
        image = view["image"]
        if image is not None:
            fitted, offset_x, offset_y = self._letterbox(image, width, height)
            target = canvas[
                y + offset_y : y + offset_y + fitted.shape[0],
                x + offset_x : x + offset_x + fitted.shape[1],
            ]
            target[:] = fitted
            content = (x + offset_x, y + offset_y, fitted.shape[1], fitted.shape[0])
        else:
            content = (x, y, width, height)
            state = view["local_state"]
            self._center_text(
                canvas,
                "Waiting for first frame" if state == "WAITING" else "Stream stale",
                x + width // 2,
                y + height // 2,
                self._font_scale(width, 0.6),
                MUTED if state == "WAITING" else RED,
            )
        if view["local_state"] == "STALE":
            self._overlay(canvas, *content, color=(10, 12, 16), alpha=0.52)
        draw_started_ns = time.monotonic_ns()
        self._draw_hud(canvas, view, *content)
        view["metrics_ref"].record_draw(
            (time.monotonic_ns() - draw_started_ns) / 1_000_000
        )
        border = self._state_color(view["local_state"])
        cv2.rectangle(canvas, (x, y), (x + width - 1, y + height - 1), border, 1)

    def _draw_hud(
        self,
        canvas: np.ndarray,
        view: dict[str, Any],
        x: int,
        y: int,
        width: int,
        height: int,
    ) -> None:
        metrics = view["metrics"]
        header = view["header"] or {}
        scale = self._font_scale(width, 0.45)
        line = max(14, round(20 * scale / 0.45))
        title_height = line + 14
        self._overlay(canvas, x, y, width, title_height, color=(9, 12, 15), alpha=0.72)
        server_state = self._server_state_label(view)
        self._text(
            canvas,
            f"{view['name']}  {view['local_state']}  {server_state}",
            (x + 8, y + line),
            scale,
            self._state_color(view["local_state"]),
            thickness=1,
        )
        if self.diagnostics:
            available_lines = max(1, (height - title_height - 10) // line)
            hud_height = min(height - title_height, line * available_lines + 10)
            hud_y = y + title_height
            self._overlay(
                canvas, x, hud_y, width, hud_height, color=(9, 12, 15), alpha=0.72
            )
            server = view["server"]
            metric_rows = [
                [
                    ("RX", self._fps(metrics["instant_fps"])),
                    ("AVG", self._fps(metrics["average_fps"])),
                    ("1% LOW", self._fps(metrics["one_percent_low_fps"])),
                ],
                [
                    ("INTERVAL", self._ms(metrics["frame_interval_ms"])),
                    ("P95", self._ms(metrics["frame_interval_p95_ms"])),
                    ("P99", self._ms(metrics["frame_interval_p99_ms"])),
                ],
                [
                    ("AGE", f"{self._ms(view['frame_age_ms'])}*"),
                    ("RATE", f"{metrics['bitrate_mbps']:.2f} Mbps"),
                    ("PAYLOAD", f"{metrics['last_payload_size'] // 1024} KiB"),
                ],
                [
                    ("GAP LOSS", self._percent(metrics["gap_loss_percent"])),
                    ("LOCAL LOSS", self._percent(metrics["local_loss_percent"])),
                    ("DISPLAY", self._fps(metrics["display_fps"])),
                ],
                [
                    ("DECODE P50", self._ms(metrics["decode_p50_ms"])),
                    ("DECODE P95", self._ms(metrics["decode_p95_ms"])),
                    ("DRAW P95", self._ms(metrics["draw_p95_ms"])),
                ],
                [
                    ("RX->DISPLAY", self._ms(metrics["receive_to_display_p95_ms"])),
                    ("CODEC", str(header.get("codec", "-")).upper()),
                    ("SIZE", f"{header.get('width', '-')}x{header.get('height', '-')}"),
                ],
                [
                    ("SERVER CAP", self._fps(server.get("capture_fps"))),
                    ("SERVER PUB", self._fps(server.get("publish_fps"))),
                    (
                        "CAP/IPC",
                        f"{self._ms(server.get('capture_cost_ms'))} / {self._ms(server.get('ipc_cost_ms'))}",
                    ),
                ],
            ]
            for index, fields in enumerate(metric_rows[:available_lines]):
                self._draw_metric_row(
                    canvas,
                    fields,
                    x + 8,
                    hud_y + line * (index + 1),
                    width - 16,
                    scale,
                )
            if available_lines > len(metric_rows):
                self._text(
                    canvas,
                    self._ellipsize(self._error_line(view, metrics), width - 16, scale),
                    (x + 8, hud_y + line * (len(metric_rows) + 1)),
                    scale,
                    RED,
                )
            return

        compact_lines = max(1, min(2, (height - title_height - 8) // line))
        compact_height = min(height - title_height, line * compact_lines + 8)
        hud_y = y + height - compact_height
        self._overlay(
            canvas, x, hud_y, width, compact_height, color=(9, 12, 15), alpha=0.72
        )
        graph_width = min(max(80, width // 4), 170) if compact_lines >= 2 else 0
        summary_width = width - graph_width - 20 if graph_width else width - 16
        compact_scale = self._font_scale(width, 0.4)
        self._draw_metric_row(
            canvas,
            [
                ("RX", self._fps(metrics["instant_fps"])),
                ("AVG", self._fps(metrics["average_fps"])),
                ("1%", self._fps(metrics["one_percent_low_fps"])),
                ("RATE", f"{metrics['bitrate_mbps']:.1f} Mbps"),
            ],
            x + 8,
            hud_y + line,
            summary_width,
            compact_scale,
            weights=[1, 1, 1, 1.3],
        )
        if compact_lines >= 2:
            self._draw_metric_row(
                canvas,
                [
                    ("AGE", f"{self._ms(view['frame_age_ms'])}*"),
                    ("GAP LOSS", self._percent(metrics["gap_loss_percent"])),
                    ("LOCAL LOSS", self._percent(metrics["local_loss_percent"])),
                ],
                x + 8,
                hud_y + line * 2,
                summary_width,
                compact_scale,
                color=MUTED,
            )
            self._draw_chart(
                canvas,
                metrics["fps_chart"],
                x + width - graph_width - 8,
                hud_y + 3,
                graph_width,
                max(24, compact_height - 6),
            )

    def _draw_chart(
        self,
        canvas: np.ndarray,
        points: list[float],
        x: int,
        y: int,
        width: int,
        height: int,
    ) -> None:
        label_scale = 0.28 if height >= 34 else 0.24
        label_height = max(9, round(11 * label_scale / 0.28))
        chart_y = y + label_height + 3
        chart_height = max(8, y + height - chart_y)
        if chart_y + chart_height > canvas.shape[0]:
            chart_height = max(1, canvas.shape[0] - chart_y)
        self._overlay(
            canvas, x, chart_y, width, chart_height, color=(18, 29, 33), alpha=0.8
        )
        cv2.rectangle(
            canvas,
            (x, chart_y),
            (x + width - 1, chart_y + chart_height - 1),
            MUTED,
            1,
            cv2.LINE_AA,
        )
        if not points:
            return
        low, high = self._chart_range(points)
        average = sum(points) / len(points)
        self._draw_metric_row(
            canvas,
            [
                ("MIN", self._chart_label(low)),
                ("AVG", self._chart_label(average)),
                ("MAX", self._chart_label(high)),
            ],
            x,
            y + label_height,
            width,
            label_scale,
            color=MUTED,
        )
        if len(points) < 2:
            return
        coords = []
        for index, value in enumerate(points):
            px = x + round(index * (width - 2) / max(len(points) - 1, 1)) + 1
            clipped = min(high, max(low, value))
            py = (
                chart_y
                + chart_height
                - 2
                - round((clipped - low) / max(high - low, 0.01) * (chart_height - 4))
            )
            coords.append((px, py))
        cv2.polylines(
            canvas, [np.asarray(coords, dtype=np.int32)], False, GREEN, 1, cv2.LINE_AA
        )

    @staticmethod
    def _chart_range(points: list[float]) -> tuple[float, float]:
        ordered = sorted(points)
        lower = ordered[max(0, round((len(ordered) - 1) * 0.05))]
        upper = ordered[min(len(ordered) - 1, round((len(ordered) - 1) * 0.95))]
        if upper - lower < 1:
            return max(0.0, lower - 1), upper + 1
        padding = (upper - lower) * 0.12
        return max(0.0, lower - padding), upper + padding

    @staticmethod
    def _chart_label(value: float) -> str:
        return f"{value:.0f}" if value >= 10 else f"{value:.1f}"

    def _on_mouse(
        self, event: int, x: int, y: int, _flags: int, _userdata: Any
    ) -> None:
        if event != cv2.EVENT_LBUTTONDBLCLK:
            return
        for hit in self._hits:
            left, top, width, height = hit.rect
            if left <= x < left + width and top <= y < top + height:
                self.focused = None if self.focused == hit.name else hit.name
                return

    def _draw_notice(self, canvas: np.ndarray) -> None:
        if self._notice is None:
            return
        message, expires_ns = self._notice
        if time.monotonic_ns() >= expires_ns:
            self._notice = None
            return
        width = int(min(canvas.shape[1] * 0.7, max(180, 9 * len(message))))
        x = (canvas.shape[1] - width) // 2
        self._overlay(
            canvas, x, HEADER_HEIGHT + 12, width, 34, color=(8, 12, 16), alpha=0.85
        )
        self._center_text(
            canvas, message, canvas.shape[1] // 2, HEADER_HEIGHT + 35, 0.5, TEXT
        )

    @staticmethod
    def _letterbox(
        image: np.ndarray, width: int, height: int
    ) -> tuple[np.ndarray, int, int]:
        scale = min(width / image.shape[1], height / image.shape[0])
        target_width = max(1, round(image.shape[1] * scale))
        target_height = max(1, round(image.shape[0] * scale))
        resized = cv2.resize(
            image, (target_width, target_height), interpolation=cv2.INTER_AREA
        )
        return resized, (width - target_width) // 2, (height - target_height) // 2

    def _draw_metric_row(
        self,
        canvas: np.ndarray,
        fields: list[tuple[str, str]],
        x: int,
        baseline_y: int,
        width: int,
        scale: float,
        *,
        weights: list[float] | None = None,
        color: tuple[int, int, int] = TEXT,
    ) -> None:
        """Draw stable metric columns with fixed anchors for changing values."""
        if not fields or width <= 0:
            return
        column_weights = weights or [1.0] * len(fields)
        if len(column_weights) != len(fields) or sum(column_weights) <= 0:
            raise ValueError("metric row weights must match fields and be positive")

        gap = max(4, round(7 * scale / 0.4))
        total_weight = sum(column_weights)
        column_left = x
        remaining_width = width
        remaining_weight = total_weight
        for index, ((label, value), weight) in enumerate(zip(fields, column_weights)):
            if index == len(fields) - 1:
                column_width = remaining_width
            else:
                column_width = max(
                    0, round(remaining_width * weight / remaining_weight)
                )
            column_right = column_left + column_width
            label_width = self._text_width(label, scale)
            available_value_width = column_width - label_width - gap

            if available_value_width >= self._text_width("...", scale):
                display_value = self._ellipsize(value, available_value_width, scale)
            else:
                display_value = ""
            self._text(canvas, label, (column_left, baseline_y), scale, MUTED)
            if display_value:
                self._right_text(
                    canvas, display_value, column_right, baseline_y, scale, color
                )

            column_left = column_right
            remaining_width -= column_width
            remaining_weight -= weight

    @staticmethod
    def _overlay(
        canvas: np.ndarray,
        x: int,
        y: int,
        width: int,
        height: int,
        *,
        color: tuple[int, int, int],
        alpha: float,
    ) -> None:
        if width <= 0 or height <= 0:
            return
        roi = canvas[y : y + height, x : x + width]
        fill = np.full_like(roi, color)
        cv2.addWeighted(fill, alpha, roi, 1 - alpha, 0, roi)

    @staticmethod
    def _font_scale(width: int, base: float) -> float:
        return max(0.32, min(base, base * width / 540))

    @staticmethod
    def _text(
        canvas: np.ndarray,
        value: str,
        point: tuple[int, int],
        scale: float,
        color: tuple[int, int, int],
        *,
        thickness: int = 1,
    ) -> None:
        cv2.putText(
            canvas,
            value,
            point,
            cv2.FONT_HERSHEY_DUPLEX,
            scale,
            color,
            thickness,
            cv2.LINE_AA,
        )

    @staticmethod
    def _right_text(
        canvas: np.ndarray,
        value: str,
        right: int,
        y: int,
        scale: float,
        color: tuple[int, int, int],
    ) -> None:
        size = cv2.getTextSize(value, cv2.FONT_HERSHEY_DUPLEX, scale, 1)[0]
        VideoWall._text(canvas, value, (right - size[0], y), scale, color)

    @staticmethod
    def _text_width(value: str, scale: float) -> int:
        return cv2.getTextSize(value, cv2.FONT_HERSHEY_DUPLEX, scale, 1)[0][0]

    @classmethod
    def _ellipsize(cls, value: str, max_width: int, scale: float) -> str:
        if cls._text_width(value, scale) <= max_width:
            return value
        suffix = "..."
        while value and cls._text_width(value + suffix, scale) > max_width:
            value = value[:-1]
        return value + suffix

    @staticmethod
    def _center_text(
        canvas: np.ndarray,
        value: str,
        x: int,
        y: int,
        scale: float,
        color: tuple[int, int, int],
    ) -> None:
        size = cv2.getTextSize(value, cv2.FONT_HERSHEY_DUPLEX, scale, 1)[0]
        VideoWall._text(canvas, value, (x - size[0] // 2, y), scale, color)

    @staticmethod
    def _state_color(state: str) -> tuple[int, int, int]:
        return {"LIVE": GREEN, "WAITING": YELLOW, "STALE": RED}.get(state, MUTED)

    def _server_state_label(self, view: dict[str, Any]) -> str:
        state = view["server"].get("state") or view["stream_state"]
        if state:
            return f"srv:{state}"
        return "srv:WAITING" if self.status_endpoint else "srv:DISABLED"

    @staticmethod
    def _fps(value: float | None) -> str:
        return "-" if value is None else f"{value:.1f} fps"

    @staticmethod
    def _ms(value: float | None) -> str:
        return "-" if value is None else f"{value:.1f} ms"

    @staticmethod
    def _percent(value: float) -> str:
        return f"{value:.1f}%"

    @staticmethod
    def _error_line(view: dict[str, Any], metrics: dict[str, Any]) -> str:
        error = view["server"].get("last_error") or view["stream_error"]
        if error:
            return f"server: {error}"[:110]
        invalid = sum(int(count) for count in metrics["invalid"].values())
        return f"invalid frames {invalid}" if invalid else "protocol OK"

    def _status_text(self, status: dict[str, Any]) -> str:
        if self.status_endpoint is None:
            return "status disabled"
        age = status.get("last_success_age_s")
        if age is not None:
            return f"status fresh {age:.0f}s"
        error = status.get("last_error") or "connecting"
        return f"status unavailable: {error}"[:80]
