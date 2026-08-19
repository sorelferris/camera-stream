#!/usr/bin/env bash
# Install camera-stream as a systemd system service for this checkout.
set -euo pipefail

readonly SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
readonly REPOSITORY_ROOT="$(cd -- "$SCRIPT_DIR/.." && pwd)"
readonly SERVICE_TEMPLATE="$REPOSITORY_ROOT/systemd/camera-stream.service"
readonly ORIGINAL_ARGS=("$@")

project_dir="$REPOSITORY_ROOT"
config_path=""
service_user="${SUDO_USER:-$(id -un)}"
unit_name="camera-stream"
start_service=true
enable_service=true
dry_run=false

usage() {
  cat <<'EOF'
Usage: scripts/install_camera_stream_service.sh [OPTIONS]

Install this checkout as a systemd service. The service runs headlessly and
writes logs to journald. It uses the selected user's uv executable and project
environment; run uv sync with the required camera extras before installation.

Options:
  --project-dir DIR  Repository containing pyproject.toml. Defaults to this checkout.
  --config PATH      Service config YAML. Defaults to PROJECT_DIR/config.yaml.
  --user USER        Unprivileged account that owns the project and camera access.
                     Defaults to the sudo invoker, or the current user.
  --unit-name NAME   systemd unit name without .service. Default: camera-stream.
  --no-start         Install and enable the unit without starting it.
  --no-enable        Install the unit without enabling or starting it.
  --dry-run          Validate inputs and print the resolved installation plan.
  -h, --help         Show this help text.

Examples:
  sudo scripts/install_camera_stream_service.sh
  sudo scripts/install_camera_stream_service.sh --config /srv/camera-stream/config.yaml
  sudo scripts/install_camera_stream_service.sh --user robot --unit-name robot-camera
EOF
}

while (($#)); do
  case "$1" in
    --project-dir)
      project_dir="${2:?--project-dir requires a directory}"
      shift 2
      ;;
    --config)
      config_path="${2:?--config requires a path}"
      shift 2
      ;;
    --user)
      service_user="${2:?--user requires a user name}"
      shift 2
      ;;
    --unit-name)
      unit_name="${2:?--unit-name requires a name}"
      shift 2
      ;;
    --no-start)
      start_service=false
      shift
      ;;
    --no-enable)
      enable_service=false
      start_service=false
      shift
      ;;
    --dry-run)
      dry_run=true
      shift
      ;;
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
done

if ! $dry_run && [[ ${EUID} -ne 0 ]]; then
  uv_hint="$(command -v uv || true)"
  exec sudo env CAMERA_STREAM_INSTALL_UV="$uv_hint" "$0" "${ORIGINAL_ARGS[@]}"
fi

if [[ ! "$unit_name" =~ ^[A-Za-z0-9_.@-]+$ ]]; then
  printf 'Invalid unit name: %s\n' "$unit_name" >&2
  exit 2
fi
if ! id "$service_user" >/dev/null 2>&1; then
  printf 'Unknown service user: %s\n' "$service_user" >&2
  exit 2
fi

project_dir="$(cd -- "$project_dir" && pwd -P)"
if [[ -z "$config_path" ]]; then
  config_path="$project_dir/config.yaml"
fi
config_path="$(cd -- "$(dirname -- "$config_path")" && pwd -P)/$(basename -- "$config_path")"

if [[ ! -f "$project_dir/pyproject.toml" ]]; then
  printf 'No pyproject.toml in project directory: %s\n' "$project_dir" >&2
  exit 1
fi
if [[ ! -f "$config_path" ]]; then
  printf 'Configuration file does not exist: %s\n' "$config_path" >&2
  exit 1
fi
if [[ ! -f "$SERVICE_TEMPLATE" ]]; then
  printf 'Service template does not exist: %s\n' "$SERVICE_TEMPLATE" >&2
  exit 1
fi

service_home="$(getent passwd "$service_user" | cut -d: -f6)"
uv_bin="${CAMERA_STREAM_INSTALL_UV:-}"
if [[ ! -x "$uv_bin" ]]; then
  uv_bin="$(command -v uv || true)"
fi
if [[ ! -x "$uv_bin" && -x "$service_home/.local/bin/uv" ]]; then
  uv_bin="$service_home/.local/bin/uv"
fi
if [[ ! -x "$uv_bin" ]]; then
  printf 'Could not find an executable uv for %s. Install uv first.\n' "$service_user" >&2
  exit 1
fi

if [[ ${EUID} -eq 0 ]]; then
  if ! runuser -u "$service_user" -- test -r "$project_dir/pyproject.toml"; then
    printf '%s cannot read project metadata: %s\n' "$service_user" "$project_dir" >&2
    exit 1
  fi
  if ! runuser -u "$service_user" -- test -r "$config_path"; then
    printf '%s cannot read configuration: %s\n' "$service_user" "$config_path" >&2
    exit 1
  fi
  if ! runuser -u "$service_user" -- test -x "$uv_bin"; then
    printf '%s cannot execute uv: %s\n' "$service_user" "$uv_bin" >&2
    exit 1
  fi
fi

readonly wrapper_dir="/usr/local/lib/$unit_name"
readonly wrapper_path="$wrapper_dir/camera-stream-run"
readonly unit_path="/etc/systemd/system/$unit_name.service"
readonly dropin_dir="/etc/systemd/system/$unit_name.service.d"
readonly dropin_path="$dropin_dir/runtime-user.conf"

printf 'Unit: %s.service\n' "$unit_name"
printf 'Run as: %s\n' "$service_user"
printf 'Project: %s\n' "$project_dir"
printf 'Config: %s\n' "$config_path"
printf 'uv: %s\n' "$uv_bin"
printf 'Wrapper: %s\n' "$wrapper_path"

if $dry_run; then
  exit 0
fi

install -d -m 0755 "$wrapper_dir" "$dropin_dir"
{
  printf '%s\n' '#!/usr/bin/env bash'
  printf '%s\n' 'set -euo pipefail'
  printf 'exec '
  printf '%q ' "$uv_bin" run --project "$project_dir" camera-stream --config "$config_path"
  printf '\n'
} >"$wrapper_path"
chmod 0755 "$wrapper_path"

install -m 0644 "$SERVICE_TEMPLATE" "$unit_path"
printf '[Service]\nUser=%s\nExecStart=\nExecStart=%s\n' \
  "$service_user" "$wrapper_path" >"$dropin_path"

systemctl daemon-reload
if $enable_service; then
  systemctl enable "$unit_name.service"
fi
if $start_service; then
  systemctl restart "$unit_name.service"
fi

printf 'Installed %s.service.\n' "$unit_name"
printf 'Inspect with: systemctl status %s.service\n' "$unit_name"
printf 'Follow logs with: journalctl -u %s.service -f\n' "$unit_name"
