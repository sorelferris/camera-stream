# camera-stream-client

`camera-stream-client` is a graphical ZeroMQ debugging client for
[`camera-stream`](../../README.md). It subscribes to the server PUB image
stream, renders a multi-camera OpenCV video wall, and overlays receive quality,
latency, decoding, and server-state metrics on every tile.

The client follows a latest-frame-wins policy. The receiver and renderer are
separated by a capacity-one latest-frame slot. If rendering cannot keep up, a
new frame replaces the old one instead of building a queue. This keeps the
display current, and `local loss` reports those replacements explicitly.

The client requires a desktop session. On Linux, X11 or Wayland must be
available; terminal-only and headless operation are not supported.

## Run

For local development from the repository root, use the workspace command. It
uses the current source tree, so code changes are reflected on every run:

```bash
uv run --package camera-stream-client camera-stream-client \
  --endpoint tcp://192.168.5.24:5555 \
  --status-endpoint tcp://192.168.5.24:5556
```

To verify the isolated `uvx` distribution from a checkout, force a fresh local
build. `uvx --from` caches packages with the same version and may otherwise
run a previously built `camera-stream-client==0.1.0`:

```bash
uvx --no-cache --from ./example/camera-stream-client camera-stream-client \
  --endpoint tcp://192.168.5.24:5555 \
  --status-endpoint tcp://192.168.5.24:5556
```

Set `--endpoint` to the externally reachable address of `stream_pub` in the
server `config.yaml`, and set `--status-endpoint` to the externally reachable
address of `status_rep`. `0.0.0.0` is a server bind address; remote clients
must use the server's actual IP address, such as `192.168.5.24`.

Repeat `--camera` to subscribe to selected cameras only. Filtering happens at
the ZeroMQ SUB subscription layer, so image data for other cameras is not
downloaded:

```bash
uv run --package camera-stream-client camera-stream-client \
  --endpoint tcp://192.168.5.24:5555 \
  --status-endpoint tcp://192.168.5.24:5556 \
  --camera base_camera \
  --camera side_camera
```

After publishing to PyPI, run it directly:

```bash
uvx camera-stream-client --endpoint tcp://192.168.5.24:5555
pipx run camera-stream-client --endpoint tcp://192.168.5.24:5555
```

The first run downloads packages and creates an isolated environment; the
OpenCV wheel is the largest download. Later runs reuse the `uv` or `pipx`
cache.

## Release

Build and validate a release, then perform a PyPI dry run:

```bash
scripts/publish_camera_stream_client.sh
```

Upload only after setting a PyPI API token in the environment:

```bash
export UV_PUBLISH_TOKEN='pypi-...'
scripts/publish_camera_stream_client.sh --publish
```

Use `--testpypi --publish` with a TestPyPI token to validate an upload before
the production release. The script refuses a dirty git worktree by default;
use `--allow-dirty` only for deliberate local builds.

## Arguments

| Argument | Required | Description |
|---|---:|---|
| `--endpoint ENDPOINT` | Yes | ZeroMQ PUB endpoint for image streams, for example `tcp://192.168.5.24:5555`. |
| `--status-endpoint ENDPOINT` | No | ZeroMQ REP endpoint for authoritative camera status, for example `tcp://192.168.5.24:5556`. It is queried once per second with a 500 ms timeout and never blocks video reception. |
| `--camera NAME` | No, repeatable | Subscribe only to the `<NAME>/color` topic. When omitted, subscribe to every camera and discover cameras dynamically. |
| `--version` | No | Print the client version. |

The status endpoint is never inferred from the image endpoint port; pass it
explicitly. Without it, the client can still display video and local metrics,
but it cannot report driver state, capture cost, or IPC metrics from the
server.

## Controls

| Input | Action |
|---|---|
| Double-click a tile | Focus that camera. Double-click it again or press `Enter` to return to the full video wall. |
| `Tab` | Toggle between the compact and detailed diagnostic HUD. |
| `S` | Save the current video wall, including HUDs, as `camera-stream-client-YYYYMMDD-HHMMSS.png` in the current directory. |
| `E` | Export endpoints, the status snapshot, and metrics as `camera-stream-client-YYYYMMDD-HHMMSS.json` in the current directory. Image payloads are not exported. |
| `Q` / `Esc` | Exit. |

The video wall adapts its grid to the window size and camera count. Each tile
preserves the source aspect ratio and uses letterboxing rather than stretching
the image.

## Status Bar And Tile States

The global status bar shows the stream endpoint, status-snapshot freshness,
the number of `LIVE` cameras over the number of visible tiles, the aggregate
one-second receive rate of visible cameras, and the keyboard shortcuts.

Every camera tile shows its name, its client-side receive state, and a `srv:`
server state:

| Display | Meaning |
|---|---|
| `LIVE` | The client is still receiving frames for this camera. |
| `WAITING` | The client has not received the first frame. A camera requested with `--camera`, or discovered through a status snapshot, appears in this state first. |
| `STALE` | The camera was seen before, but no new frame has arrived for `max(2 seconds, 3 x receive interval P50)`. The last image and historical metrics remain visible for troubleshooting. |
| `srv:ONLINE` / `OFFLINE` / `RECOVERING` / `CONFIG_ERROR` | Authoritative state reported by the server status endpoint. |
| `srv:WAITING` | A status endpoint was configured, but the first status snapshot has not arrived. |
| `srv:DISABLED` | No `--status-endpoint` was provided, so no server state source is available. |

The client state and `srv:` state intentionally remain separate. For example,
`LIVE + srv:OFFLINE` can show the final frame received before a disconnect,
while `STALE + srv:ONLINE` points to a problem between the server publisher and
the client.

## Compact HUD

The bottom of every tile shows the compact summary by default:

```text
RX 30.0 fps  AVG 29.8 fps  1% 24.1 fps  4.2 Mbps
age 38.0 ms*  gap loss 0.7%  local loss 2.3%
```

| Metric | Measurement | Diagnostic meaning |
|---|---|---|
| `RX` | Instantaneous FPS calculated from the interval between the two most recently received frames. | Shows current arrival speed. A single-frame fluctuation is normal. |
| `AVG` | Average FPS over at most 300 received-frame intervals. | Shows short-term stable throughput. |
| `1%` | FPS calculated from the average interval of the slowest 1% of at most 300 received-frame intervals. | The closer it is to `AVG`, the fewer low-FPS spikes and less jitter the stream has. |
| `Mbps` | Bit rate of received compressed image payloads over the previous second. | Excludes ZeroMQ protocol headers; useful for checking link and JPEG load. |
| `age` | Estimated frame age from header `captured_utc_ns` to the client's current UTC time. | The `*` means this depends on NTP/PTP clock synchronization. Without clock synchronization, clock offset appears as latency. |
| `gap loss` | Sequence gaps divided by estimated source frames over at most 300 received frames. | Frames were absent before reaching the client receive path. Causes may include capture, IPC, PUB high-water-mark behavior, or the network; it is not necessarily network packet loss. |
| `local loss` | Percentage of at most 300 received frames that were replaced by a newer frame before reaching the renderer. | Increases when local decoding or drawing cannot keep up. This is expected latest-frame-wins behavior and does not accumulate playback latency. |

The green chart at the right shows FPS derived from the most recent 100
received-frame intervals. Its Y range is automatically scaled from the P5-P95
sample range with a small margin. Labels at the upper and lower edge show the
current maximum and minimum Y-axis FPS. Extreme outliers are clipped at the
chart edge so they do not flatten the rest of the curve.

## Detailed Diagnostic HUD

Press `Tab` to display the detailed diagnostics for each camera. On a dense
grid or small window, rows are retained from the top and clipped to the
available space.

| Display | Meaning |
|---|---|
| `interval` / `p95` / `p99` | Latest, P95, and P99 received-frame intervals over at most 300 samples. Higher values indicate greater arrival jitter. |
| `payload` | Size of the most recently received image payload, in KiB. |
| `display` | FPS derived from intervals between at most 300 frames that completed client-side decoding. |
| `decode p50/p95` | P50 and P95 time to decode JPEG or `raw_bgr8` payloads. A high JPEG decode P95 usually indicates a client CPU bottleneck. |
| `draw p95` | P95 cost of compositing the HUD onto the image. |
| `rx->display p95` | P95 local time from the receiver thread accepting a frame until JPEG decoding completes and the frame is handed to rendering. It excludes server capture time and is different from `age`. |
| `jpeg` / `raw_bgr8`, `WIDTHxHEIGHT` | Codec and source dimensions read from the most recent frame header. |
| `server capture` | Camera capture FPS reported by the server worker. Requires `--status-endpoint`. |
| `pub` | Per-camera server publish FPS. Requires `--status-endpoint`. |
| `capture cost` | Most recent duration of the server camera driver's `read()` call. Requires `--status-endpoint`. |
| `IPC` | Most recent worker-side encoding and internal IPC PUSH send duration. Requires `--status-endpoint`. |
| `protocol OK` / `invalid frames N` | Image protocol validation result. Frames with invalid headers, unknown codecs, or decode failures are dropped and counted. A server-reported camera error takes precedence on this line. |

`rx->display` covers client receive, time waiting in the latest-frame slot, and
JPEG decoding. HUD compositing is reported separately as `draw p95`; final
window presentation time in `cv2.imshow` is not included.

## Troubleshooting

| Symptom | Check first |
|---|---|
| High `gap loss`, low `local loss` | Check server `server capture` and `pub`, capture-slot and IPC drops in the server TUI, then network throughput. |
| Low `gap loss`, high `local loss` | Check client `decode p95`, `draw p95`, video-wall camera count, and local CPU use. Test one stream with `--camera`. |
| Both loss rates are high | Total resolution, FPS, or JPEG bit rate exceeds available system capacity. Reduce unneeded camera resolution, FPS, or JPEG quality first. |
| `srv:WAITING` persists | Confirm `--status-endpoint` points to the server `status_rep` endpoint and check for `status unavailable` in the global status bar. |
| `STALE` with `srv:ONLINE` | The server considers the driver online but the client is not receiving new images. Inspect the PUB path and `gap loss`. |
