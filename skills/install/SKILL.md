---
description: First-time setup for ace-buddy — checks Python deps, merges hooks, offers to flash firmware, starts the daemon.
---

Run the full ace-buddy install by running:

```bash
bash "$CLAUDE_PLUGIN_ROOT/scripts/install.sh"
```

Safe to re-run; every step is idempotent. It will:
1. Check Python + deps (pyserial, optionally bleak for BLE)
2. Merge hook config into `~/.claude/settings.json`
3. Offer to flash firmware if a device is plugged in
4. Start the daemon
