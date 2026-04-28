#!/usr/bin/env bash
# Build + upload the buddy firmware.
#
# Usage:
#   flash.sh                 # autodetect serial port
#   flash.sh /dev/cu.XYZ     # explicit port

set -euo pipefail
SELF_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
# shellcheck source=common.sh
source "$SELF_DIR/common.sh"

if ! have_pio; then
  echo "error: PlatformIO not found. Install: pip install platformio" >&2
  exit 1
fi

PORT="${1:-}"
if [ -z "$PORT" ]; then PORT="$(find_serial)"; fi
if [ -z "$PORT" ]; then
  echo "error: no serial device found. plug in the device and retry." >&2
  exit 1
fi

cd "$REPO_ROOT"

echo "==> Uploading firmware to $PORT..."
pio run -t upload --upload-port "$PORT"

echo ""
echo "==> Done."
