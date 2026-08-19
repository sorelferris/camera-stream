"""Thread-safe rolling measurements for one camera stream."""

from __future__ import annotations

import math
import statistics
import threading
import time
from collections import Counter, deque
from typing import Any

ROLLING_WINDOW = 300
CHART_WINDOW = 100
BITRATE_WINDOW_NS = 1_000_000_000


class CameraMetrics:
    """Measurements made at the client, separated from server status data."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._intervals_ns: deque[int] = deque(maxlen=ROLLING_WINDOW)
        self._display_intervals_ns: deque[int] = deque(maxlen=ROLLING_WINDOW)
        self._decode_ms: deque[float] = deque(maxlen=ROLLING_WINDOW)
        self._draw_ms: deque[float] = deque(maxlen=ROLLING_WINDOW)
        self._receive_to_display_ms: deque[float] = deque(maxlen=ROLLING_WINDOW)
        self._bitrate_events: deque[tuple[int, int]] = deque()
        self._source_gap_window: deque[int] = deque(maxlen=ROLLING_WINDOW)
        self._overwrite_window: deque[bool] = deque(maxlen=ROLLING_WINDOW)
        self._last_received_ns = 0
        self._last_displayed_ns = 0
        self._last_sequence: int | None = None
        self._last_header: dict[str, Any] | None = None
        self._last_payload_size = 0
        self._received_frames = 0
        self._displayed_frames = 0
        self._invalid = Counter()

    def record_received(
        self,
        header: dict[str, Any],
        payload_size: int,
        received_ns: int,
        *,
        overwritten: bool,
    ) -> None:
        with self._lock:
            if self._last_received_ns:
                delta = received_ns - self._last_received_ns
                if delta > 0:
                    self._intervals_ns.append(delta)
            sequence = int(header["sequence"])
            source_gap = 0
            if self._last_sequence is not None and sequence > self._last_sequence + 1:
                source_gap = sequence - self._last_sequence - 1
            self._last_sequence = sequence
            self._last_received_ns = received_ns
            self._last_header = dict(header)
            self._last_payload_size = payload_size
            self._received_frames += 1
            self._source_gap_window.append(source_gap)
            self._overwrite_window.append(overwritten)
            self._bitrate_events.append((received_ns, payload_size))
            self._trim_bitrate(received_ns)

    def record_invalid(self, reason: str) -> None:
        with self._lock:
            self._invalid[reason] += 1

    def record_displayed(
        self, displayed_ns: int, decode_ms: float, receive_to_display_ms: float
    ) -> None:
        with self._lock:
            if self._last_displayed_ns:
                delta = displayed_ns - self._last_displayed_ns
                if delta > 0:
                    self._display_intervals_ns.append(delta)
            self._last_displayed_ns = displayed_ns
            self._displayed_frames += 1
            self._decode_ms.append(decode_ms)
            self._receive_to_display_ms.append(receive_to_display_ms)

    def record_draw(self, draw_ms: float) -> None:
        with self._lock:
            self._draw_ms.append(draw_ms)

    def snapshot(self, now_ns: int | None = None) -> dict[str, Any]:
        now_ns = now_ns or time.monotonic_ns()
        with self._lock:
            self._trim_bitrate(now_ns)
            interval_values = list(self._intervals_ns)
            display_values = list(self._display_intervals_ns)
            return {
                "received_frames": self._received_frames,
                "displayed_frames": self._displayed_frames,
                "last_received_ns": self._last_received_ns,
                "last_header": dict(self._last_header) if self._last_header else None,
                "last_payload_size": self._last_payload_size,
                "instant_fps": self._instant_fps(interval_values),
                "average_fps": self._average_fps(interval_values),
                "one_percent_low_fps": self._one_percent_low_fps(interval_values),
                "display_fps": self._average_fps(display_values),
                "frame_interval_ms": self._latest_ms(interval_values),
                "frame_interval_p50_ms": self._percentile_ms(interval_values, 50),
                "frame_interval_p95_ms": self._percentile_ms(interval_values, 95),
                "frame_interval_p99_ms": self._percentile_ms(interval_values, 99),
                "fps_chart": [
                    round(1_000_000_000 / value, 2)
                    for value in interval_values[-CHART_WINDOW:]
                ],
                "gap_loss_percent": self._gap_loss_percent(),
                "local_loss_percent": self._local_loss_percent(),
                "decode_p50_ms": self._percentile(self._decode_ms, 50),
                "decode_p95_ms": self._percentile(self._decode_ms, 95),
                "draw_p95_ms": self._percentile(self._draw_ms, 95),
                "receive_to_display_p95_ms": self._percentile(
                    self._receive_to_display_ms, 95
                ),
                "bitrate_mbps": round(
                    sum(size for _, size in self._bitrate_events) * 8 / 1_000_000, 3
                ),
                "invalid": dict(self._invalid),
            }

    def _trim_bitrate(self, now_ns: int) -> None:
        cutoff = now_ns - BITRATE_WINDOW_NS
        while self._bitrate_events and self._bitrate_events[0][0] < cutoff:
            self._bitrate_events.popleft()

    def _gap_loss_percent(self) -> float:
        received = len(self._source_gap_window)
        missing = sum(self._source_gap_window)
        return 0.0 if not missing else 100 * missing / (received + missing)

    def _local_loss_percent(self) -> float:
        return (
            0.0
            if not self._overwrite_window
            else 100 * sum(self._overwrite_window) / len(self._overwrite_window)
        )

    @staticmethod
    def _instant_fps(values: list[int]) -> float | None:
        return None if not values else 1_000_000_000 / values[-1]

    @staticmethod
    def _average_fps(values: list[int]) -> float | None:
        return None if not values else 1_000_000_000 / statistics.fmean(values)

    @staticmethod
    def _one_percent_low_fps(values: list[int]) -> float | None:
        if not values:
            return None
        count = max(1, math.ceil(len(values) * 0.01))
        slowest = sorted(values, reverse=True)[:count]
        return 1_000_000_000 / statistics.fmean(slowest)

    @staticmethod
    def _latest_ms(values: list[int]) -> float | None:
        return None if not values else values[-1] / 1_000_000

    @staticmethod
    def _percentile_ms(values: list[int], percentile: float) -> float | None:
        return CameraMetrics._percentile(
            [value / 1_000_000 for value in values], percentile
        )

    @staticmethod
    def _percentile(values: Any, percentile: float) -> float | None:
        values = sorted(values)
        if not values:
            return None
        index = (len(values) - 1) * percentile / 100
        lower = math.floor(index)
        upper = math.ceil(index)
        if lower == upper:
            return float(values[lower])
        return float(values[lower] + (values[upper] - values[lower]) * (index - lower))
