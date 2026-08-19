import time
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
        supervisor.records["cam"].status.update(
            {"capture_cost_ms": 4.2, "ipc_cost_ms": 1.1}
        )
        supervisor.last_supervisor_cost_ms = 2.4
        supervisor.last_service_cost_ms = 0.6
        supervisor.clients[17] = {
            "ip": "192.168.5.21",
            "port": 54321,
            "fd": 17,
            "endpoint": "tcp://127.0.0.1:5555",
            "connected_monotonic_ns": time.monotonic_ns(),
        }
        supervisor.clients[18] = {
            "ip": "192.168.5.22",
            "port": 54322,
            "fd": 18,
            "endpoint": "tcp://127.0.0.1:5555",
            "connected_monotonic_ns": time.monotonic_ns(),
        }
        output = StringIO()
        Console(file=output, force_terminal=False, width=300).print(
            Dashboard(supervisor).render()
        )
        assert "cam" in output.getvalue()
        assert "192.168.5.21" in output.getvalue()
        assert "192.168.5.22" in output.getvalue()
        assert "SUPERVISOR" in output.getvalue()
        assert "SERVICE" in output.getvalue()
        assert "codec   JPEG" in output.getvalue()
        assert "est rx  0 Mbps" in output.getvalue()
        assert "peer    54321/TCP" in output.getvalue()
        assert "cost 4 ms" in output.getvalue()
        assert "cost 2 ms" in output.getvalue()
        assert "cost 0.60 ms" in output.getvalue()
        assert "PUB  tcp://127.0.0.1:*" in output.getvalue()
        assert "REP  tcp://localhost:*" in output.getvalue()
        assert "uptime 00:00:00" in output.getvalue()
        assert "up 00:00:00" in output.getvalue()
        assert "egress  0 Mbps" in output.getvalue()
        assert "aggregate_publish_fps" not in output.getvalue()
        client_lines = [
            line
            for line in output.getvalue().splitlines()
            if "192.168.5.21" in line or "192.168.5.22" in line
        ]
        assert len(client_lines) == 2
    finally:
        supervisor.shutdown()


def test_dashboard_arrow_places_marker_between_labels() -> None:
    output = StringIO()
    Console(file=output, force_terminal=False, width=80).print(
        Dashboard._arrow("IPC", "PUSH -> PULL")
    )
    rendered = output.getvalue()
    assert (
        rendered.index("IPC") < rendered.index(">>>") < rendered.index("PUSH -> PULL")
    )


def test_dashboard_formats_sub_millisecond_costs_with_decimals() -> None:
    assert Dashboard._cost_milliseconds(0.62) == "0.62 ms"
    assert Dashboard._cost_milliseconds(1.0) == "1 ms"
