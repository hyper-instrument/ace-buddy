# ace-buddy Claude Code Plugin

This plugin bridges ace-buddy hardware devices with Claude Code terminal sessions.

## How it works

```
Claude Code hook  --POST-->  bridge daemon  --serial/BLE-->  ace-buddy device
                                  ^                              |
                                  +------ permission ack --------+
```

Claude Code fires hooks on tool use, session start/stop, and user prompts.
The hooks POST payloads to a local Python daemon (`claude_code_bridge.py`),
which translates them into the heartbeat JSON protocol the ace-buddy
firmware already speaks. Permission decisions (approve/deny) flow back
from the device through the daemon to Claude Code.

## Install

### As a Claude Code plugin (recommended)

```bash
# From the ace repo root:
make install-buddy

# Or install directly:
claude plugin install ace-buddy@/path/to/ace-buddy
```

Then run `/ace-buddy-install` in Claude Code.

### Manual

```bash
# Install deps
pip install pyserial   # required
pip install bleak      # optional, for BLE transport

# Merge hooks into ~/.claude/settings.json
bash plugin/scripts/install-hooks.sh

# Start the daemon
bash plugin/scripts/start.sh
```

## Slash Commands

| Command | Description |
|---------|-------------|
| `/ace-buddy-install` | First-time setup (deps, hooks, flash, daemon) |
| `/ace-buddy-start` | Start the bridge daemon |
| `/ace-buddy-stop` | Stop the bridge daemon |
| `/ace-buddy-status` | Show daemon and device status |
| `/ace-buddy-flash` | Re-flash firmware (stop -> flash -> start) |

## Configuration

Environment variables for `/ace-buddy-start`:

| Variable | Default | Description |
|----------|---------|-------------|
| `BUDDY_TRANSPORT` | `auto` | Transport: `auto`, `serial`, or `ble` |
| `BUDDY_BUDGET` | `200000` | Context window limit for budget bar |
| `BUDDY_OWNER` | `$USER` | Owner name displayed on device |
| `BUDDY_HTTP_PORT` | `9876` | HTTP listener port for hooks |
| `BUDDY_PYTHON` | (auto) | Python interpreter to use |

## State Directory

`~/.ace-buddy/` contains:
- `daemon.pid` -- PID of the running daemon
- `daemon.log` -- daemon log output
