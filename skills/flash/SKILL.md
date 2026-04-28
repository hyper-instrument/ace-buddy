---
description: Build and flash ace-buddy firmware. Stops and restarts the daemon around the flash.
---

Flash the ace-buddy firmware by running these commands in sequence:

```bash
bash "$CLAUDE_PLUGIN_ROOT/scripts/stop.sh" || true
bash "$CLAUDE_PLUGIN_ROOT/scripts/flash.sh"
bash "$CLAUDE_PLUGIN_ROOT/scripts/start.sh"
```

Requires PlatformIO and a buddy device plugged in via USB.
