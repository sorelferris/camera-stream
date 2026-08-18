from camera_stream.streamer import build_parser


def test_tui_flag_enables_the_in_process_dashboard() -> None:
    args = build_parser().parse_args(["--config", "config.yaml", "--tui"])
    assert args.tui is True
