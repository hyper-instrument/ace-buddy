#!/usr/bin/env bash
# Bootstrap the ace-buddy environment:
#   1. Verify Python + deps (pyserial, optionally bleak for BLE).
#   2. Merge the hook config into ~/.claude/settings.json if not there.
#   3. Flash firmware if a device is plugged in (optional).
#   4. Start the daemon.
#
# Safe to re-run; every step is idempotent.

set -euo pipefail
SELF_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
# shellcheck source=common.sh
source "$SELF_DIR/common.sh"

need_python
echo "==> Python: $PY"

echo "==> Checking Python deps (pyserial)..."
if ! "$PY" -c "import serial" 2>/dev/null; then
  "$PY" -m pip install pyserial
fi
echo "    ok"

echo "==> Checking optional BLE dep (bleak)..."
if "$PY" -c "import bleak" 2>/dev/null; then
  echo "    bleak installed"
else
  echo "    bleak not installed (BLE transport unavailable, USB serial still works)"
  echo "    to enable BLE: $PY -m pip install bleak"
fi

echo "==> Merging hooks into ~/.claude/settings.json..."
"$SELF_DIR/install-hooks.sh"

if have_pio; then
  DEV="$(find_serial)"
  if [ -n "$DEV" ]; then
    echo ""
    read -r -p "Flash firmware to $DEV now? [y/N] " yn
    if [[ "$yn" =~ ^[Yy]$ ]]; then
      "$SELF_DIR/flash.sh"
    fi
  else
    echo ""
    echo "==> No buddy device detected over USB."
    echo "    Plug it in and run: $SELF_DIR/flash.sh"
  fi
else
  echo ""
  echo "==> PlatformIO not found (firmware flash skipped)."
  echo "    Install PlatformIO to flash firmware: pip install platformio"
fi

echo ""
echo "==> Starting daemon..."
"$SELF_DIR/start.sh"

echo ""
echo "Done. Useful commands:"
echo "  $SELF_DIR/start.sh    -- start daemon (idempotent)"
echo "  $SELF_DIR/stop.sh     -- stop daemon"
echo "  $SELF_DIR/status.sh   -- show daemon + device status"
echo "  $SELF_DIR/flash.sh    -- (re)flash firmware"
echo "  tail -f $LOG_FILE"
