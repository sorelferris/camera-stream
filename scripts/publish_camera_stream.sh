#!/usr/bin/env bash
# Build and optionally publish the camera-stream-server distribution.
set -euo pipefail

readonly SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
readonly REPOSITORY_ROOT="$(cd -- "$SCRIPT_DIR/.." && pwd)"
readonly DIST_DIR="$REPOSITORY_ROOT/dist/camera-stream-server"

publish=false
test_pypi=false
allow_dirty=false

usage() {
  cat <<'EOF'
Usage: scripts/publish_camera_stream_server.sh [OPTIONS]

Build, validate, and publish camera-stream-server.

Options:
  --publish       Upload after validation. Without this flag, perform a dry run.
  --testpypi      Target TestPyPI instead of PyPI.
  --allow-dirty   Permit a dirty git worktree. Use only for deliberate local builds.
  -h, --help      Show this help text.

Environment:
  UV_PUBLISH_TOKEN  Required with --publish unless --trusted-publishing is added
                    to this script for a CI Trusted Publishing workflow.

Examples:
  scripts/publish_camera_stream_server.sh
  scripts/publish_camera_stream_server.sh --testpypi --publish
  UV_PUBLISH_TOKEN=pypi-... scripts/publish_camera_stream_server.sh --publish
EOF
}

while (($#)); do
  case "$1" in
    --publish) publish=true ;;
    --testpypi) test_pypi=true ;;
    --allow-dirty) allow_dirty=true ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      printf 'Unknown argument: %s\n' "$1" >&2
      usage >&2
      exit 2
      ;;
  esac
  shift
done

cd "$REPOSITORY_ROOT"

if ! $allow_dirty && [[ -n "$(git status --porcelain)" ]]; then
  printf '%s\n' 'Refusing to release from a dirty git worktree.' >&2
  printf '%s\n' 'Commit/stash the changes, or use --allow-dirty for an intentional local build.' >&2
  exit 1
fi

if $publish && [[ -z "${UV_PUBLISH_TOKEN:-}" ]]; then
  printf '%s\n' 'UV_PUBLISH_TOKEN must be set when --publish is used.' >&2
  exit 1
fi

if $test_pypi; then
  readonly PUBLISH_URL='https://test.pypi.org/legacy/'
  readonly CHECK_URL='https://test.pypi.org/simple/'
  readonly TARGET_NAME='TestPyPI'
else
  readonly PUBLISH_URL='https://upload.pypi.org/legacy/'
  readonly CHECK_URL='https://pypi.org/simple/'
  readonly TARGET_NAME='PyPI'
fi

printf 'Validating camera-stream-server for %s...\n' "$TARGET_NAME"
uv run --extra dev pytest -q \
  tests/test_capture_loop.py \
  tests/test_config.py \
  tests/test_dashboard.py \
  tests/test_demand.py \
  tests/test_protocol.py \
  tests/test_streamer.py \
  tests/test_supervisor.py
uv run --extra dev black --check src tests
uv run --extra dev ruff check src tests
git diff --check

printf 'Building distributions in %s...\n' "$DIST_DIR"
uv build --package camera-stream-server --out-dir "$DIST_DIR" --clear
uvx twine check "$DIST_DIR"/*

publish_args=(
  --publish-url "$PUBLISH_URL"
  --check-url "$CHECK_URL"
  --trusted-publishing never
)
if ! $publish; then
  publish_args=(--dry-run "${publish_args[@]}")
fi

if $publish; then
  printf 'Uploading camera-stream-server to %s...\n' "$TARGET_NAME"
else
  printf 'Dry-running upload to %s; no files will be uploaded.\n' "$TARGET_NAME"
fi
uv publish "${publish_args[@]}" "$DIST_DIR"/*

if $publish; then
  printf 'Published successfully. Verify with:\n'
  printf '  uvx --refresh camera-stream-server --help\n'
else
  printf '%s\n' 'Validation and dry run completed. Re-run with --publish to upload.'
fi
