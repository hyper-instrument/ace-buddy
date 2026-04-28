#!/usr/bin/env bash
# Start the bridge daemon in the background. Idempotent.
#
# Config via env vars:
#   BUDDY_TRANSPORT   auto|serial|ble (default: auto)
#   BUDDY_BUDGET      context window budget for the device (default: 200000)
#   BUDDY_OWNER       override $USER as the displayed owner name
#   BUDDY_HTTP_PORT   HTTP listener port (default: 9876)

set -euo pipefail
SELF_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
# shellcheck source=common.sh
source "$SELF_DIR/common.sh"

if is_running; then
  echo "daemon already running (pid $(cat "$PID_FILE"))"
  exit 0
fi

need_python

TRANSPORT="${BUDDY_TRANSPORT:-auto}"
BUDGET="${BUDDY_BUDGET:-200000}"
OWNER="${BUDDY_OWNER:-$USER}"
HTTP_PORT="${BUDDY_HTTP_PORT:-9876}"

nohup "$PY" "$DAEMON" \
  --transport "$TRANSPORT" \
  --budget "$BUDGET" \
  --owner "$OWNER" \
  --http-port "$HTTP_PORT" \
  >> "$LOG_FILE" 2>&1 &
echo $! > "$PID_FILE"

sleep 0.5
if is_running; then
  echo "daemon started (pid $(cat "$PID_FILE"))  transport=$TRANSPORT"
  echo "log: $LOG_FILE"
else
  echo "daemon failed to start -- check $LOG_FILE"
  tail -n 20 "$LOG_FILE" || true
  exit 1
fi
