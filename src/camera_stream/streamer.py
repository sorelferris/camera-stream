from __future__ import annotations

import argparse
import logging
import sys
from importlib import resources
from pathlib import Path

from camera_stream.config import load_config
from camera_stream.supervisor import Supervisor, install_signal_handlers

TEMPLATE_FILENAME = "config.yaml"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Low-latency multi-camera ZeroMQ streamer"
    )
    parser.add_argument("--config", type=Path, help="YAML service configuration")
    parser.add_argument(
        "--tui", action="store_true", help="show the in-process Rich server dashboard"
    )
    parser.add_argument(
        "--download-template",
        action="store_true",
        help="write a starter config.yaml into the current directory",
    )
    return parser


def download_template(destination: Path) -> int:
    """Write the packaged deployment template without overwriting user files."""
    template = (
        resources.files("camera_stream")
        .joinpath("templates", TEMPLATE_FILENAME)
        .read_text(encoding="utf-8")
    )
    try:
        with destination.open("x", encoding="utf-8") as output:
            output.write(template)
    except FileExistsError:
        print(
            f"refusing to overwrite existing template: {destination}", file=sys.stderr
        )
        return 2
    except OSError as exc:
        print(f"could not write template {destination}: {exc}", file=sys.stderr)
        return 2
    print(f"wrote starter configuration: {destination}")
    return 0


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
    )
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.download_template:
        if args.config is not None or args.tui:
            parser.error(
                "--download-template cannot be combined with --config or --tui"
            )
        return download_template(Path.cwd() / TEMPLATE_FILENAME)
    if args.config is None:
        parser.error("--config is required unless --download-template is used")
    try:
        config = load_config(args.config)
    except Exception as exc:  # noqa: BLE001 - CLI must report every config/parser error
        print(f"configuration error: {exc}", file=sys.stderr)
        return 2
    supervisor = Supervisor(config)
    install_signal_handlers(supervisor)
    supervisor.run(tui=args.tui)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
