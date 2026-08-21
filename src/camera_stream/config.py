from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class EndpointConfig(StrictModel):
    stream_pub: str | None = None
    ingest_api: str | None = None

    @model_validator(mode="after")
    def validate_endpoints(self) -> EndpointConfig:
        for name in ("stream_pub", "ingest_api"):
            value = getattr(self, name)
            if value is not None and not value.startswith("tcp://"):
                raise ValueError(f"{name} must use a tcp:// endpoint")
        return self


class DeviceConfig(StrictModel):
    path: str | None = None
    serial: str | None = None


class StreamProfile(StrictModel):
    width: int = Field(gt=0, le=7680)
    height: int = Field(gt=0, le=4320)
    fps: int = Field(gt=0, le=240)


class EncodingConfig(StrictModel):
    codec: Literal["jpeg", "raw_bgr8"]
    jpeg_quality: int | None = Field(default=None, ge=1, le=100)

    @model_validator(mode="after")
    def validate_quality(self) -> EncodingConfig:
        if self.codec == "jpeg" and self.jpeg_quality is None:
            raise ValueError("jpeg_quality is required for jpeg")
        if self.codec == "raw_bgr8" and self.jpeg_quality is not None:
            raise ValueError("jpeg_quality is not valid for raw_bgr8")
        return self


class IdlePolicyConfig(StrictModel):
    """Stop idle camera workers and reopen them when a stream is subscribed."""

    enabled: bool = False
    sleep_after_s: float = Field(default=60.0, gt=0, le=86_400)


class IngestPolicyConfig(StrictModel):
    """Server-side limits and ownership lifetime for remote camera push."""

    token: str | None = Field(default=None, min_length=1, max_length=4096)
    topic_lease_s: float = Field(default=60.0, gt=0, le=86_400)
    max_width: int = Field(default=3840, gt=0, le=7680)
    max_height: int = Field(default=2160, gt=0, le=4320)
    max_payload_bytes: int = Field(default=16_777_216, gt=0, le=128 * 1024 * 1024)
    max_remote_topics: int = Field(default=64, gt=0, le=1024)
    max_connections: int = Field(default=32, gt=0, le=1024)
    max_fps: int = Field(default=120, gt=0, le=1000)


class CameraConfig(StrictModel):
    name: str = Field(min_length=1, max_length=80, pattern=r"^[A-Za-z0-9_.-]+$")
    driver: Literal["opencv", "realsense", "orbbec"]
    device: DeviceConfig
    profile: StreamProfile
    encoding: EncodingConfig

    @model_validator(mode="after")
    def validate_device(self) -> CameraConfig:
        if self.driver == "opencv":
            if not self.device.path or self.device.serial:
                raise ValueError(
                    "opencv requires device.path and forbids device.serial"
                )
        elif not self.device.serial or self.device.path:
            raise ValueError(
                f"{self.driver} requires device.serial and forbids device.path"
            )
        return self


class ServiceConfig(StrictModel):
    endpoints: EndpointConfig
    cameras: list[CameraConfig] = Field(default_factory=list, max_length=64)
    idle_policy: IdlePolicyConfig = Field(default_factory=IdlePolicyConfig)
    ingest_policy: IngestPolicyConfig = Field(default_factory=IngestPolicyConfig)

    @model_validator(mode="after")
    def validate_names(self) -> ServiceConfig:
        names = [camera.name for camera in self.cameras]
        if len(names) != len(set(names)):
            raise ValueError("camera names must be unique")
        return self

    def require_server_role(self) -> None:
        if self.endpoints.stream_pub is None:
            raise ValueError("server requires endpoints.stream_pub")

    def require_push_role(self) -> None:
        if self.endpoints.ingest_api is None:
            raise ValueError("push requires endpoints.ingest_api")
        if not self.cameras:
            raise ValueError("push requires at least one camera")


def load_config(path: str | Path) -> ServiceConfig:
    config_path = Path(path)
    with config_path.open("r", encoding="utf-8") as stream:
        document: Any = yaml.safe_load(stream)
    if not isinstance(document, dict):
        raise TypeError("configuration root must be a YAML mapping")
    return ServiceConfig.model_validate(document)
