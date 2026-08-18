from io import StringIO

from rich.console import Console

from camera_stream.config import ServiceConfig
from camera_stream.dashboard import Dashboard
from camera_stream.supervisor import Supervisor


def test_dashboard_renders_in_process_status() -> None:
    config = ServiceConfig.model_validate(
        {
            "endpoints": {
                "stream_pub": "tcp://127.0.0.1:*",
                "status_rep": "tcp://localhost:*",
            },
            "cameras": [
                {
                    "name": "cam",
                    "driver": "opencv",
                    "device": {"path": "/dev/video0"},
                    "profile": {"width": 640, "height": 480, "fps": 30},
                    "encoding": {"codec": "jpeg", "jpeg_quality": 85},
                }
            ],
        }
    )
    supervisor = Supervisor(config)
    try:
        output = StringIO()
        Console(file=output, force_terminal=False, width=200).print(
            Dashboard(supervisor).render()
        )
        assert "cam" in output.getvalue()
        assert "SUPERVISOR / AGGREGATE" in output.getvalue()
        assert "EXTERNAL SERVICE" in output.getvalue()
        assert "Capture -> PUB" in output.getvalue()
    finally:
        supervisor.shutdown()
