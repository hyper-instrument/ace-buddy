---
description: Build + flash ace-buddy firmware. Stops + restarts the daemon around the flash.
---

Runs `pio run -t upload` to flash the firmware. Requires PlatformIO and
a buddy device plugged in via USB.

!`bash -c '~/.ace-buddy/bin/stop.sh || true; ~/.ace-buddy/bin/flash.sh; ~/.ace-buddy/bin/start.sh'`
