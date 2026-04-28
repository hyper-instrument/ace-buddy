---
description: Start the ace-buddy bridge daemon (background).
---

Start the ace-buddy bridge daemon by running:

```bash
bash "$CLAUDE_PLUGIN_ROOT/scripts/start.sh"
```

The daemon listens on `127.0.0.1:9876`, receives Claude Code hook events, and forwards them to the hardware device.

PID is written to `~/.ace-buddy/daemon.pid`, logs to `~/.ace-buddy/daemon.log`.

Transport defaults to `auto` (tries USB serial first, falls back to BLE).
Override via env: `BUDDY_TRANSPORT=ble BUDDY_BUDGET=1000000`.

Idempotent — re-running while already up is a no-op.
