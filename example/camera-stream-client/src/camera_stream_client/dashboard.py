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
        self._draw_global_bar(canvas, views, status, receiver_error)
        display_views = views
        if self.focused:
            display_views = [view for view in views if view["name"] == self.focused]
            if not display_views:
                self.focused = None
                display_views = views
        self._hits = self._draw_grid(canvas, display_views)
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
        server_state = view["server"].get("state") or view["stream_state"] or "-"
        self._text(
            canvas,
            f"{view['name']}  {view['local_state']}  srv:{server_state}",
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
            lines = [
                f"rx {self._fps(metrics['instant_fps'])}  avg {self._fps(metrics['average_fps'])}  1% low {self._fps(metrics['one_percent_low_fps'])}",
                f"interval {self._ms(metrics['frame_interval_ms'])}  p95 {self._ms(metrics['frame_interval_p95_ms'])}  p99 {self._ms(metrics['frame_interval_p99_ms'])}",
                f"age {self._ms(view['frame_age_ms'])} (NTP/PTP)  rate {metrics['bitrate_mbps']:.2f} Mbps  payload {metrics['last_payload_size'] // 1024} KiB",
                f"gap {metrics['source_gaps']}  overwrite {metrics['client_overwrites']}  display {self._fps(metrics['display_fps'])}",
                f"decode p50/p95 {self._ms(metrics['decode_p50_ms'])}/{self._ms(metrics['decode_p95_ms'])}  draw p95 {self._ms(metrics['draw_p95_ms'])}",
                f"rx->display p95 {self._ms(metrics['receive_to_display_p95_ms'])}  {header.get('codec', '-')} {header.get('width', '-')}x{header.get('height', '-')}",
                f"server capture {self._fps(server.get('capture_fps'))}  pub {self._fps(server.get('publish_fps'))}  capture cost {self._ms(server.get('capture_cost_ms'))}  IPC {self._ms(server.get('ipc_cost_ms'))}",
                self._error_line(view, metrics),
            ]
            for index, value in enumerate(lines[:available_lines]):
                self._text(
                    canvas,
                    self._ellipsize(value, width - 16, scale),
                    (x + 8, hud_y + line * (index + 1)),
                    scale,
                    TEXT if index < 7 else RED,
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
        self._text(
            canvas,
            self._ellipsize(
                f"RX {self._fps(metrics['instant_fps'])}  AVG {self._fps(metrics['average_fps'])}  1% {self._fps(metrics['one_percent_low_fps'])}  {metrics['bitrate_mbps']:.1f} Mbps",
                summary_width,
                scale,
            ),
            (x + 8, hud_y + line),
            scale,
            TEXT,
        )
        if compact_lines >= 2:
            self._text(
                canvas,
                self._ellipsize(
                    f"age {self._ms(view['frame_age_ms'])}*  gap {metrics['source_gaps']}  local drop {metrics['client_overwrites']}",
                    summary_width,
                    scale,
                ),
                (x + 8, hud_y + line * 2),
                scale,
                MUTED,
            )
            self._draw_chart(
                canvas,
                metrics["fps_chart"],
                x + width - graph_width - 8,
                hud_y + 7,
                graph_width,
                max(18, compact_height - 14),
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
        self._overlay(canvas, x, y, width, height, color=(18, 29, 33), alpha=0.8)
        if len(points) < 2:
            return
        low, high = self._chart_range(points)
        coords = []
        for index, value in enumerate(points):
            px = x + round(index * (width - 2) / max(len(points) - 1, 1)) + 1
            clipped = min(high, max(low, value))
            py = (
                y
                + height
                - 2
                - round((clipped - low) / max(high - low, 0.01) * (height - 4))
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

    @staticmethod
    def _fps(value: float | None) -> str:
        return "-" if value is None else f"{value:.1f} fps"

    @staticmethod
    def _ms(value: float | None) -> str:
        return "-" if value is None else f"{value:.1f} ms"

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
