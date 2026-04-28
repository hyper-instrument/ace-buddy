---
description: Build + flash ace-buddy firmware. Stops + restarts the daemon around the flash.
---

Runs `pio run -t upload` to flash the firmware. Requires PlatformIO and
a buddy device plugged in via USB.

!`bash -c 'bash "$CLAUDE_PLUGIN_ROOT/plugin/scripts/stop.sh" || true; bash "$CLAUDE_PLUGIN_ROOT/plugin/scripts/flash.sh"; bash "$CLAUDE_PLUGIN_ROOT/plugin/scripts/start.sh"'`
