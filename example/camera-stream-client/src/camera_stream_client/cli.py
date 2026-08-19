"""Command-line entry point for ``camera-stream-client``."""

from __future__ import annotations

import argparse

from . import __version__
from .app import ClientApp


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Visual latest-frame-wins debugger for camera-stream PUB streams."
    )
    parser.add_argument(
        "--endpoint",
        required=True,
        help="server stream PUB endpoint, e.g. tcp://192.168.5.24:5555",
    )
    parser.add_argument(
        "--status-endpoint",
        help="optional server status REP endpoint, e.g. tcp://192.168.5.24:5556",
    )
    parser.add_argument(
        "--camera",
        action="append",
        default=[],
        metavar="NAME",
        help="subscribe to one camera only; repeat to select several",
    )
    parser.add_argument("--version", action="version", version=__version__)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    return ClientApp(
        endpoint=args.endpoint,
        status_endpoint=args.status_endpoint,
        cameras=args.camera,
    ).run()
