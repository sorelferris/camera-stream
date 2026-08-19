from pathlib import Path

import pytest
from pydantic import ValidationError

from camera_stream.config import ServiceConfig, load_config


def valid_camera(driver: str = "opencv") -> dict:
    device = {"path": "/dev/video0"} if driver == "opencv" else {"serial": "abc"}
    return {
        "name": "cam",
        "driver": driver,
        "device": device,
        "profile": {"width": 640, "height": 480, "fps": 30},
        "encoding": {"codec": "jpeg", "jpeg_quality": 85},
    }


def valid_service(camera: dict | None = None) -> dict:
    return {
        "endpoints": {
            "stream_pub": "tcp://127.0.0.1:5555",
            "status_rep": "tcp://127.0.0.1:5556",
        },
        "cameras": [camera or valid_camera()],
    }


def test_yaml_config_loads() -> None:
    config = load_config(Path(__file__).parents[1] / "config.yaml")
    assert len(config.cameras) == 3
    assert config.cameras[0].encoding.jpeg_quality == 85


def test_duplicate_names_rejected() -> None:
    document = valid_service()
    document["cameras"].append(valid_camera())
    with pytest.raises(ValidationError):
        ServiceConfig.model_validate(document)


def test_raw_codec_rejects_quality() -> None:
    camera = valid_camera()
    camera["encoding"] = {"codec": "raw_bgr8", "jpeg_quality": 80}
    with pytest.raises(ValidationError):
        ServiceConfig.model_validate(valid_service(camera))


def test_driver_device_shape_is_strict() -> None:
    camera = valid_camera("realsense")
    camera["device"] = {"path": "/dev/video0"}
    with pytest.raises(ValidationError):
        ServiceConfig.model_validate(valid_service(camera))


def test_idle_policy_is_optional_and_validated() -> None:
    default = ServiceConfig.model_validate(valid_service())
    assert not default.idle_policy.enabled

    document = valid_service()
    document["idle_policy"] = {"enabled": True, "sleep_after_s": 15}
    assert ServiceConfig.model_validate(document).idle_policy.sleep_after_s == 15

    document["idle_policy"]["sleep_after_s"] = 0
    with pytest.raises(ValidationError):
        ServiceConfig.model_validate(document)
