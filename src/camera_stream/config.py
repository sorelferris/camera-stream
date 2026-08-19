from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class EndpointConfig(StrictModel):
    stream_pub: str
    status_rep: str

    @model_validator(mode="after")
    def validate_endpoints(self) -> EndpointConfig:
        for name, endpoint in (
            ("stream_pub", self.stream_pub),
            ("status_rep", self.status_rep),
        ):
            if not endpoint.startswith("tcp://"):
                raise ValueError(f"{name} must use a tcp:// endpoint")
        if self.stream_pub == self.status_rep:
            raise ValueError("stream_pub and status_rep must be different")
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
    cameras: list[CameraConfig] = Field(min_length=1, max_length=64)
    idle_policy: IdlePolicyConfig = Field(default_factory=IdlePolicyConfig)

    @model_validator(mode="after")
    def validate_names(self) -> ServiceConfig:
        names = [camera.name for camera in self.cameras]
        if len(names) != len(set(names)):
            raise ValueError("camera names must be unique")
        return self


def load_config(path: str | Path) -> ServiceConfig:
    config_path = Path(path)
    with config_path.open("r", encoding="utf-8") as stream:
        document: Any = yaml.safe_load(stream)
    if not isinstance(document, dict):
        raise TypeError("configuration root must be a YAML mapping")
    return ServiceConfig.model_validate(document)
