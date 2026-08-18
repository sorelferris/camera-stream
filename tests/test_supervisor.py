import time

import zmq

from camera_stream.config import ServiceConfig
from camera_stream.supervisor import Supervisor


def test_first_capture_updates_status_without_waiting_for_heartbeat() -> None:
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
    context = zmq.Context()
    worker = context.socket(zmq.DEALER)
    worker.connect(supervisor.control_endpoint)
    captured_monotonic_ns = time.monotonic_ns()
    captured_utc_ns = time.time_ns()
    try:
        worker.send_json(
            {
                "type": "capture",
                "camera": "cam",
                "captured_monotonic_ns": captured_monotonic_ns,
                "captured_utc_ns": captured_utc_ns,
            }
        )
        assert supervisor.control_router.poll(1000) == zmq.POLLIN
        supervisor._handle_control()
        status = supervisor.records["cam"].status
        assert status["last_capture_monotonic_ns"] == captured_monotonic_ns
        assert status["last_capture_utc_ns"] == captured_utc_ns
        assert status["state"] == "ONLINE"
    finally:
        worker.close(0)
        context.term()
        supervisor.shutdown()
