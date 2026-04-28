#!/usr/bin/env bash
# Shared helpers for the ace-buddy plugin scripts.
#
# Path conventions:
#   REPO_ROOT  -- the checked-out ace-buddy repo
#   STATE_DIR  -- $HOME/.ace-buddy  (pid file, log)
#   DAEMON     -- tools/claude_code_bridge.py
#   PY         -- a Python 3 interpreter

set -euo pipefail

SCRIPTS_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
REPO_ROOT="$( cd "$SCRIPTS_DIR/.." && pwd )"
STATE_DIR="$HOME/.ace-buddy"
PID_FILE="$STATE_DIR/daemon.pid"
LOG_FILE="$STATE_DIR/daemon.log"
DAEMON="$REPO_ROOT/tools/claude_code_bridge.py"

mkdir -p "$STATE_DIR"

# Pick a Python 3 interpreter.
pick_python() {
  if [ -n "${BUDDY_PYTHON:-}" ] && [ -x "${BUDDY_PYTHON}" ]; then
    echo "$BUDDY_PYTHON"; return
  fi
  if command -v python3 >/dev/null 2>&1; then echo "$(command -v python3)"; return; fi
  if command -v python >/dev/null 2>&1; then echo "$(command -v python)"; return; fi
  echo ""; return 1
}

PY="$(pick_python || true)"

need_python() {
  if [ -z "$PY" ]; then
    echo "error: no suitable Python found." >&2
    echo "install Python 3 or set BUDDY_PYTHON." >&2
    exit 1
  fi
}

have_pio() { command -v pio >/dev/null 2>&1; }

find_serial() {
  ls /dev/cu.usbserial-* /dev/ttyUSB* 2>/dev/null | head -n1 || true
}

is_running() {
  [ -f "$PID_FILE" ] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null
}
