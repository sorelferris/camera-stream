"""Arguments and runner for the visual ``camera-stream client`` command."""

from __future__ import annotations

import argparse

from camera_stream import __version__

from .app import ClientApp


def add_arguments(parser: argparse.ArgumentParser) -> None:
    """Add visual client options to a command parser."""
    parser.add_argument(
        "--endpoint",
        required=True,
        help="server stream PUB endpoint, e.g. tcp://192.168.5.24:5555",
    )
    parser.add_argument(
        "--camera",
        action="append",
        default=[],
        metavar="NAME",
        help="subscribe to one camera only; repeat to select several",
    )
    parser.add_argument("--version", action="version", version=__version__)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Visual latest-frame-wins debugger for camera-stream PUB streams."
    )
    add_arguments(parser)
    return parser.parse_args(argv)


def run(args: argparse.Namespace) -> int:
    """Run the visual client from parsed command-line arguments."""
    return ClientApp(
        endpoint=args.endpoint,
        cameras=args.camera,
    ).run()


def main(argv: list[str] | None = None) -> int:
    """Run the module directly; package users should invoke ``camera-stream client``."""
    return run(parse_args(argv))
