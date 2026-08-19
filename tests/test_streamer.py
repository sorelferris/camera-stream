from __future__ import annotations

import json
import queue
import threading
from pathlib import Path

import zmq

from camera_stream.config import load_config
from camera_stream.streamer import build_parser, main


class Publisher:
    def __init__(self, messages: list[list[bytes]]) -> None:
        self.messages = messages
        self.ready: queue.Queue[str] = queue.Queue()
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.endpoint = ""

    def __enter__(self):
        self.thread.start()
        self.endpoint = self.ready.get(timeout=1.0)
        return self

    def __exit__(self, *_args: object) -> None:
        self.stop.set()
        self.thread.join(timeout=1.0)

    def _run(self) -> None:
        context = zmq.Context()
        socket = context.socket(zmq.PUB)
        socket.setsockopt(zmq.LINGER, 0)
        socket.bind("tcp://127.0.0.1:*")
        self.ready.put(socket.getsockopt_string(zmq.LAST_ENDPOINT))
        try:
            while not self.stop.is_set():
                for message in self.messages:
                    socket.send_multipart(message)
                    if self.stop.wait(0.01):
                        return
        finally:
            socket.close(0)
            context.term()


def topic_messages() -> list[list[bytes]]:
    snapshot = {
        "type": "snapshot",
        "cameras": [
            {"name": "front", "state": "ONLINE"},
            {"name": "side", "state": "SLEEPING"},
        ],
    }
    header = {
        "camera": "front",
        "codec": "jpeg",
        "height": 480,
        "payload_size": 3,
        "schema_version": 1,
        "sequence": 1,
        "width": 640,
    }
    return [
        [b"status/snapshot", json.dumps(snapshot).encode()],
        [b"front/color", json.dumps(header).encode(), b"jpg"],
    ]


def test_tui_flag_enables_the_in_process_dashboard() -> None:
    args = build_parser().parse_args(["--config", "config.yaml", "--tui"])
    assert args.tui is True


def test_topic_parser_accepts_ros_like_commands() -> None:
    args = build_parser().parse_args(
        ["topic", "hz", "front/color", "--endpoint", "tcp://127.0.0.1:5555"]
    )
    assert args.command == "topic"
    assert args.topic_command == "hz"
    assert args.topic == "front/color"


def test_topic_list_and_info_read_a_public_stream(capsys) -> None:
    with Publisher(topic_messages()) as publisher:
        assert main(["topic", "list", "--endpoint", publisher.endpoint]) == 0
        assert (
            main(
                [
                    "topic",
                    "info",
                    "front/color",
                    "--endpoint",
                    publisher.endpoint,
                ]
            )
            == 0
        )

    output = capsys.readouterr().out
    assert "front/color" in output
    assert "side/color" in output
    assert '"state": "ONLINE"' in output
    assert '"codec": "jpeg"' in output


def test_topic_echo_hz_and_bw_stop_after_requested_frames(capsys) -> None:
    with Publisher(topic_messages()) as publisher:
        endpoint = publisher.endpoint
        assert (
            main(
                ["topic", "echo", "front/color", "--endpoint", endpoint, "--count", "1"]
            )
            == 0
        )
        assert (
            main(["topic", "hz", "front/color", "--endpoint", endpoint, "--count", "2"])
            == 0
        )
        assert (
            main(["topic", "bw", "front/color", "--endpoint", endpoint, "--count", "2"])
            == 0
        )

    output = capsys.readouterr().out
    assert '"camera": "front"' in output
    assert "average rate:" in output
    assert "average bandwidth:" in output


def test_unified_distribution_exposes_server_and_client_cli_names() -> None:
    project_file = Path(__file__).parents[1] / "pyproject.toml"
    document = project_file.read_text(encoding="utf-8")

    assert 'name = "camera-stream"' in document
    assert 'camera-stream = "camera_stream.streamer:main"' in document
    assert 'camera-stream-client = "camera_stream.client.cli:main"' in document


def test_download_template_writes_once_without_overwriting(
    tmp_path, monkeypatch, capsys
) -> None:
    monkeypatch.chdir(tmp_path)

    assert main(["--download-template"]) == 0
    template = tmp_path / "config.yaml"
    assert 'name: "camera0"' in template.read_text(encoding="utf-8")
    assert load_config(template).cameras[0].name == "camera0"
    assert main(["--download-template"]) == 2
    assert "refusing to overwrite" in capsys.readouterr().err
