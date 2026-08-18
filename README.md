# camera-stream

Linux service for publishing multiple local cameras over ZeroMQ with a
latest-frame-wins policy. It supports OpenCV/V4L2, Intel RealSense, and Orbbec
color cameras.

## Run

Install the drivers used by `config.yaml`, then run the service:

```bash
uv sync --extra realsense --extra orbbec
uv run camera-stream --config config.yaml
```

The minimal Python client demo subscribes to one color stream, decodes the
multipart payload and displays it with OpenCV:

```bash
uv run python example/client.py --camera head_camera
# Headless statistics-only mode
uv run python example/client.py --camera head_camera --no-display
```

For the three V4L2 devices available on this machine, use the ready-to-run
demo configuration. The client subscribes to all configured camera topics and
shows them in a single 2-column mosaic window:

```bash
uv run camera-stream --config config.demo.yaml
uv run python example/client.py --config config.demo.yaml
```

In another terminal, run the Rich telemetry TUI:

```bash
uv run camera-stream-tui --config config.yaml
```

Use `--once` for a single non-interactive snapshot. The TUI subscribes to the
same stream endpoint, so its `TUI FPS`, frame age, payload size and sequence
gaps describe the client-side flow; service-side capture/publish rates and
drop counters come from `status_rep`. Frame age uses UTC timestamps and is most
accurate when the TUI host clock is synchronized with the streaming host.
Before a status snapshot arrives, the TUI shows `WAITING` and marks the status
endpoint as disconnected rather than reporting a misleading camera state.

`stream_pub` publishes camera frames as three-part ZeroMQ messages:

```text
[topic UTF-8] [header JSON UTF-8] [JPEG or BGR bytes]
```

Topics are `<camera-name>/color`. The header declares `schema_version`,
`sequence`, capture timestamps, dimensions, pixel format and codec.

`status_rep` accepts `{"op":"get_status"}` and returns the supervisor's
current status snapshot. State changes are also published on the stream socket
under `status/<camera-name>`. A camera remains `STARTING` until its worker has
actually captured a first frame, then changes to `ONLINE`; this transition and
the first capture timestamp do not depend on any stream subscriber.

The service is intentionally live-only: no recording, replay, frame grouping,
or image transformation is performed. Every internal data stage has capacity
one, so a slow encoder or subscriber loses old frames instead of building a
queue.
