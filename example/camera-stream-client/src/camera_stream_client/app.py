"""Application orchestration for the visual camera-stream debugger."""

from __future__ import annotations

import json
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import cv2

from .dashboard import VideoWall
from .state import CameraRegistry
from .transport import StatusPoller, StatusStore, StreamReceiver, client_endpoint


class ClientApp:
    """Runs independent networking threads and the main-thread OpenCV UI."""

    def __init__(
        self,
        *,
        endpoint: str,
        status_endpoint: str | None,
        cameras: list[str],
    ) -> None:
        self.stream_endpoint = client_endpoint(endpoint)
        self.status_endpoint = (
            client_endpoint(status_endpoint) if status_endpoint is not None else None
        )
        self.cameras = set(cameras)
        self.stop = threading.Event()
        self.registry = CameraRegistry(self.cameras)
        self.status_store = StatusStore()
        self.receiver = StreamReceiver(
            self.stream_endpoint, self.cameras, self.registry, self.stop
        )
        self.status_poller = (
            StatusPoller(
                self.status_endpoint, self.registry, self.status_store, self.stop
            )
            if self.status_endpoint
            else None
        )
        self.wall = VideoWall(self.stream_endpoint, self.status_endpoint)

    def run(self) -> int:
        self.wall.open()
        self.receiver.start()
        if self.status_poller is not None:
            self.status_poller.start()
        try:
            while not self.stop.is_set():
                self.registry.consume_latest()
                now_monotonic_ns = time.monotonic_ns()
                views = self.registry.views(now_monotonic_ns, time.time_ns())
                status = self.status_store.view(now_monotonic_ns)
                canvas = self.wall.render(views, status, self.receiver.error)
                cv2.imshow("CAMERA STREAM CLIENT", canvas)
                command = self.wall.handle_key(cv2.waitKeyEx(1))
                if command == "quit":
                    break
                if command == "screenshot":
                    self._save_screenshot(canvas)
                elif command == "export":
                    self._export_diagnostics(views, status)
        except cv2.error as exc:
            print(f"OpenCV GUI error: {exc}")
            return 2
        except KeyboardInterrupt:
            return 0
        finally:
            self.stop.set()
            self.receiver.join(timeout=1.0)
            if self.status_poller is not None:
                self.status_poller.join(timeout=1.0)
            self.wall.close()
        return 0

    def _save_screenshot(self, canvas: Any) -> None:
        path = self._output_path("png")
        if cv2.imwrite(str(path), canvas):
            self.wall.notice(f"Saved {path.name}")
        else:
            self.wall.notice("Could not save screenshot")

    def _export_diagnostics(
        self, views: list[dict[str, Any]], status: dict[str, Any]
    ) -> None:
        document = {
            "schema_version": 1,
            "created_utc": datetime.now(timezone.utc).isoformat(),
            "stream_endpoint": self.stream_endpoint,
            "status_endpoint": self.status_endpoint,
            "requested_cameras": sorted(self.cameras),
            "status": status,
            "cameras": [self._export_view(view) for view in views],
        }
        path = self._output_path("json")
        path.write_text(
            json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        self.wall.notice(f"Saved {path.name}")

    @staticmethod
    def _export_view(view: dict[str, Any]) -> dict[str, Any]:
        return {
            key: value
            for key, value in view.items()
            if key not in {"image", "metrics_ref"}
        }

    @staticmethod
    def _output_path(suffix: str) -> Path:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        return Path.cwd() / f"camera-stream-client-{stamp}.{suffix}"
