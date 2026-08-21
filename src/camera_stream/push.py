"""CLI-facing local camera capture adapters for remote ingest publishing."""

from __future__ import annotations

import logging
import threading
from collections.abc import Iterable
from dataclasses import dataclass

from camera_stream.config import CameraConfig, ServiceConfig
from camera_stream.drivers import DriverConfigurationError, create_driver
from camera_stream.publisher import PublishedStream, StreamPublisher

logger = logging.getLogger(__name__)


@dataclass
class PushCameraWorker:
    config: CameraConfig
    stream: PublishedStream
    publisher: StreamPublisher
    stop: threading.Event

    def __post_init__(self) -> None:
        self.thread = threading.Thread(
            target=self._run, name=f"push-capture-{self.config.name}", daemon=True
        )

    def start(self) -> None:
        self.thread.start()

    def join(self, timeout: float | None = None) -> None:
        self.thread.join(timeout)

    def _run(self) -> None:
        retry_s = 1.0
        while not self.stop.is_set() and self.stream.state != "REJECTED":
            if not self.publisher.connected:
                self.stop.wait(0.1)
                continue
            driver = None
            try:
                driver = create_driver(self.config)
                driver.open()
                retry_s = 1.0
                logger.info("push camera opened: camera=%s", self.config.name)
                while (
                    not self.stop.is_set()
                    and self.publisher.connected
                    and self.stream.state != "REJECTED"
                ):
                    self.stream.publish(driver.read())
            except DriverConfigurationError as exc:
                self.stream._local_error("CONFIG_ERROR", str(exc))
                logger.error(
                    "push camera configuration error: camera=%s error=%s",
                    self.config.name,
                    exc,
                )
                return
            # Camera SDK exceptions are vendor-specific and not consistently typed.
            except Exception as exc:  # noqa: BLE001
                if not self.stop.is_set():
                    self.stream._local_error("RECOVERING", str(exc))
                    logger.warning(
                        "push camera read failed: camera=%s error=%s",
                        self.config.name,
                        exc,
                    )
                    self.stop.wait(retry_s)
                    retry_s = min(30.0, retry_s * 2)
            finally:
                if driver is not None:
                    driver.close()


class PushService:
    """Run selected local cameras through one remote :class:`StreamPublisher`."""

    def __init__(
        self, config: ServiceConfig, *, token: str | None, cameras: Iterable[str]
    ) -> None:
        config.require_push_role()
        selected = set(cameras)
        unknown = selected - {camera.name for camera in config.cameras}
        if unknown:
            raise ValueError(f"unknown cameras: {', '.join(sorted(unknown))}")
        self.config = config
        self.stop = threading.Event()
        self.publisher = StreamPublisher(config.endpoints.ingest_api or "", token=token)
        camera_configs = [
            camera
            for camera in config.cameras
            if not selected or camera.name in selected
        ]
        self.workers: list[PushCameraWorker] = []
        for camera in camera_configs:
            stream = self.publisher.open_stream(
                camera=camera.name,
                codec=camera.encoding.codec,
                jpeg_quality=camera.encoding.jpeg_quality,
            )
            self.workers.append(
                PushCameraWorker(camera, stream, self.publisher, self.stop)
            )

    def run(self) -> int:
        self.publisher.start()
        for worker in self.workers:
            worker.start()
        try:
            while not self.stop.wait(0.2):
                states = [worker.stream.state for worker in self.workers]
                if states and all(
                    state in {"REJECTED", "CONFIG_ERROR"} for state in states
                ):
                    return 1
            return 0
        finally:
            self.stop.set()
            for worker in self.workers:
                worker.join(timeout=1.0)
            self.publisher.close()

    def request_stop(self) -> None:
        self.stop.set()
