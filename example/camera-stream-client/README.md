# camera-stream-client

Visual latest-frame-wins debugger for a `camera-stream` ZeroMQ PUB endpoint.

Run from a checkout without installing this repository:

```bash
uvx --from ./example/camera-stream-client camera-stream-client \
  --endpoint tcp://192.168.5.24:5555 \
  --status-endpoint tcp://192.168.5.24:5556
```

After publishing, the same command becomes:

```bash
uvx camera-stream-client --endpoint tcp://192.168.5.24:5555
```

The client requires a desktop session because it displays the camera video
wall. Press `Tab` for the detailed HUD, double-click a tile to focus it, `S`
to save a screenshot, `E` to export diagnostics, and `Q` or `Esc` to exit.
