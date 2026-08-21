from __future__ import annotations

import argparse
import logging
import os
import signal
import sys
from importlib import resources
from pathlib import Path

from camera_stream.client.cli import add_arguments as add_client_arguments
from camera_stream.client.cli import run as run_client
from camera_stream.topic import add_topic_subcommands, run_topic_command

SERVER_TEMPLATE_FILENAME = "config.yaml"
PUSH_TEMPLATE_FILENAME = "push-config.yaml"
OUTPUT_TEMPLATE_FILENAME = "config.yaml"


def configure_logging(*, tui: bool = False) -> None:
    """Keep lifecycle logs useful headlessly without disrupting Rich Live."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    logging.getLogger("camera_stream").setLevel(
        logging.CRITICAL + 1 if tui else logging.NOTSET
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Low-latency multi-camera ZeroMQ streaming tools"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    server = subparsers.add_parser("server", help="run the camera streaming server")
    server.add_argument("--config", type=Path, help="YAML service configuration")
    server.add_argument(
        "--tui", action="store_true", help="show the in-process Rich server dashboard"
    )
    server.add_argument(
        "--download-template",
        action="store_true",
        help="write a starter config.yaml into the current directory",
    )

    push = subparsers.add_parser(
        "push", help="capture local cameras and push to a server"
    )
    push.add_argument("--config", type=Path, help="YAML push configuration")
    push.add_argument(
        "--download-template",
        action="store_true",
        help="write a starter push config.yaml into the current directory",
    )
    push.add_argument(
        "--camera", action="append", default=[], help="push only this configured camera"
    )
    push.add_argument(
        "--token",
        help="ingest token; defaults to CAMERA_STREAM_INGEST_TOKEN when unset",
    )

    client = subparsers.add_parser("client", help="view camera streams graphically")
    add_client_arguments(client)

    add_topic_subcommands(subparsers)
    return parser


def download_template(destination: Path, template_filename: str) -> int:
    """Write the packaged deployment template without overwriting user files."""
    template = (
        resources.files("camera_stream")
        .joinpath("templates", template_filename)
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
    parser = build_parser()
    args = parser.parse_args(argv)
    configure_logging(tui=args.command == "server" and args.tui)
    if args.command == "client":
        return run_client(args)
    if args.command == "topic":
        try:
            return run_topic_command(args)
        except (TypeError, ValueError) as exc:
            print(f"topic error: {exc}", file=sys.stderr)
            return 2
    if args.command == "push":
        if args.download_template:
            if args.config is not None or args.camera or args.token:
                parser.error(
                    "push --download-template cannot be combined with --config, --camera, or --token"
                )
            return download_template(
                Path.cwd() / OUTPUT_TEMPLATE_FILENAME, PUSH_TEMPLATE_FILENAME
            )
        if args.config is None:
            parser.error("push --config is required unless --download-template is used")
        from camera_stream.config import load_config
        from camera_stream.push import PushService

        try:
            config = load_config(args.config)
            token = args.token or os.environ.get("CAMERA_STREAM_INGEST_TOKEN")
            service = PushService(config, token=token, cameras=args.camera)
        # CLI must report every configuration/setup error without a traceback.
        except Exception as exc:  # noqa: BLE001
            print(f"push configuration error: {exc}", file=sys.stderr)
            return 2

        def stop_push(_signum: int, _frame: object) -> None:
            service.request_stop()

        signal.signal(signal.SIGINT, stop_push)
        signal.signal(signal.SIGTERM, stop_push)
        return service.run()
    if args.download_template:
        if args.config is not None or args.tui:
            parser.error(
                "--download-template cannot be combined with --config or --tui"
            )
        return download_template(
            Path.cwd() / OUTPUT_TEMPLATE_FILENAME, SERVER_TEMPLATE_FILENAME
        )
    if args.config is None:
        parser.error("server --config is required unless --download-template is used")
    # Keep the graphical client importable on Windows without server-only modules.
    from camera_stream.config import load_config
    from camera_stream.supervisor import Supervisor, install_signal_handlers

    try:
        config = load_config(args.config)
        config.require_server_role()
    except Exception as exc:  # noqa: BLE001 - CLI must report every config/parser error
        print(f"configuration error: {exc}", file=sys.stderr)
        return 2
    supervisor = Supervisor(config)
    install_signal_handlers(supervisor)
    supervisor.run(tui=args.tui)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
