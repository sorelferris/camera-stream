from pathlib import Path

from camera_stream.config import load_config
from camera_stream.streamer import build_parser, main


def test_tui_flag_enables_the_in_process_dashboard() -> None:
    args = build_parser().parse_args(["--config", "config.yaml", "--tui"])
    assert args.tui is True


def test_server_distribution_exposes_pypi_and_compatible_cli_names() -> None:
    project_file = Path(__file__).parents[1] / "pyproject.toml"
    document = project_file.read_text(encoding="utf-8")

    assert 'name = "camera-stream-server"' in document
    assert 'camera-stream = "camera_stream.streamer:main"' in document
    assert 'camera-stream-server = "camera_stream.streamer:main"' in document


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
