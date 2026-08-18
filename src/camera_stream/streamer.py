from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from camera_stream.config import load_config
from camera_stream.supervisor import Supervisor, install_signal_handlers


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Low-latency multi-camera ZeroMQ streamer"
    )
    parser.add_argument(
        "--config", type=Path, required=True, help="YAML service configuration"
    )
    parser.add_argument(
        "--tui", action="store_true", help="show the in-process Rich server dashboard"
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
    )
    args = build_parser().parse_args(argv)
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
