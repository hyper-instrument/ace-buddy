# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Overview

This is an ESP32 Arduino firmware for the M5StickC Plus / M5StickS3 that acts as a "hardware buddy" for Claude desktop apps. It receives session telemetry and permission prompts over BLE (Nordic UART Service) and renders them on the stick's 135x240 LCD as ASCII pets or custom GIF characters.

## Common Commands

Build and flash (default environment):
```bash
pio run -t upload
```

Flash a specific board:
```bash
pio run -e m5stickc-plus -t upload
pio run -e m5stack-sticks3 -t upload
```

Erase before flashing (useful when switching from old firmware):
```bash
pio run -t erase && pio run -t upload
```

Upload LittleFS filesystem (for bundled GIF characters):
```bash
pio run -t uploadfs
```

Flash a character pack directly over USB (bypasses BLE folder push):
```bash
python tools/flash_character.py characters/bufo
```

Prepare/resize source GIFs to 96px wide:
```bash
python tools/prep_character.py <source_dir>
```

Monitor serial output:
```bash
pio device monitor
```

## Feishu (Lark) Mode

Instead of using the ESP32 device for permission prompts, you can receive confirmation requests as interactive cards in Feishu (飞书).

**Prerequisites:**
- A Feishu app with bot capability
- `FEISHU_APP_ID` and `FEISHU_APP_SECRET` in `.env`
- `lark_oapi` installed (`pip install lark-oapi`)

**Configure `.env`:**
```bash
# 在项目根目录 .env 里添加
FEISHU_APP_ID=cli_xxxxxxxxxxxxxxxx
FEISHU_APP_SECRET=xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
```

**Start the bridge:**
```bash
/buddy start
# 或手动
python3 tools/claude_code_bridge.py
```

Bridge 启动时会自动加载 `.env`。只要 `FEISHU_APP_ID` + `FEISHU_APP_SECRET` 填了，就会自动启用飞书模式，并启动 WebSocket 连接飞书。

**How it works:**
1. Start the bridge (`/buddy start` or `python3 tools/claude_code_bridge.py`)
2. The bridge connects to Feishu via WebSocket
3. Send any message to the bot in Feishu — the bridge auto-pairs your `open_id`
4. Permission prompts are sent as interactive cards to the paired user
5. Click "Allow / Deny / Option" on the card — the bridge receives the event over WebSocket and replies to Claude Code

**`.env` variables:**
| Variable | Description |
|----------|-------------|
| `FEISHU_APP_ID` | Feishu app ID |
| `FEISHU_APP_SECRET` | Feishu app secret |
| `BUDDY_FEISHU_USER_ID` | (Optional) Pre-bound user `open_id` |
| `BUDDY_TRANSPORT` | `auto`/`serial`/`ble`/`none` |
| `BUDDY_BUDGET` | Context-window budget bar limit |

## Architecture

### Rendering Pipeline

- `main.cpp` owns the 135x240 `M5Canvas` sprite. Each `loop()` tick updates the sprite and `pushSprite(0, 0)` blits it to the LCD. Landscape clock mode is the exception: it draws directly to `M5.Lcd` with rotation.
- Two mutually exclusive character backends:
  - **ASCII mode**: `buddy.cpp` dispatches to one of 18 species in `src/buddies/*.cpp`. Each species exposes 7 state functions (sleep, idle, busy, attention, celebrate, dizzy, heart) that render into the sprite at 5 fps with tick-gated redraws.
  - **GIF mode**: `character.cpp` mounts LittleFS, reads `/characters/<name>/manifest.json`, and decodes GIFs via `AnimatedGIF`. GIFs are rendered scanline-by-scanline into the sprite. Text-mode characters (frame arrays in manifest) are also supported.
- `buddySetPeek()` / `characterSetPeek()` switch between full-size home-screen rendering and half-scale "peek" rendering for the PET/INFO panels.

### Data Ingestion

- `data.h` is a header-only parser. It reads newline-delimited JSON from both `Serial` (USB) and the BLE ring buffer, populating a `TamaState` struct. It supports three modes: live (real data within last 10s), asleep (no connection), and demo (auto-cycling fake scenarios).
- `ble_bridge.cpp` implements the Nordic UART Service. RX bytes go into a 2048-byte ring buffer drained by `dataPoll()`. TX replies (acks, status) are chunked to the negotiated MTU.
- The BLE link uses LE Secure Connections with passkey entry. `blePasskey()` returns non-zero while pairing; main.cpp renders the 6-digit code.

### Folder Push Protocol

- `xfer.h` handles the desktop-to-device file transfer triggered by dropping a folder in the Hardware Buddy window. Commands: `char_begin` → `file` → repeated `chunk` → `file_end` → `char_end`.
- Each command is acked before the next is sent. Base64 chunks are decoded with `mbedtls_base64_decode` and appended to LittleFS files under `/characters/<name>/`.
- Only one character lives on the device at a time; installing a new one wipes `/characters/` first.

### Persistence (NVS)

- `stats.h` is header-only with file-static state. **Include it from exactly one translation unit** (currently `main.cpp`). It stores approvals, denials, nap time, velocity ring buffer, level, and tokens in the Preferences "buddy" namespace.
- Settings (sound, BT toggle, LED, HUD, clock rotation) and pet/owner names are also NVS-backed.
- Writes are sparse (only on significant events), because NVS flash sectors have limited erase cycles.

### State Machine

- `PersonaState` enum: sleep, idle, busy, attention, celebrate, dizzy, heart.
- `derive()` maps `TamaState` to a base state. One-shot states (celebrate, dizzy, heart) override it for a fixed duration via `triggerOneShot()`.
- Shake detection uses IMU magnitude delta; face-down nap uses Z-axis dominance. Nap dims the screen and pauses animation.
- Screen auto-powers off after 30s of inactivity (not while charging or during an approval prompt).

### Adding a New ASCII Species

1. Create `src/buddies/<name>.cpp`.
2. Define 7 state functions and a `Species` extern (see `cat.cpp` for the pattern).
3. Add the extern declaration and table entry in `buddy.cpp`.

### Important Invariants

- `buddy_common.h` defines shared geometry (`BUDDY_X_CENTER`, `BUDDY_Y_BASE`, etc.) and rendering helpers. Species hardcode these coordinates, so retargeting the canvas shifts the body but not particle overlays.
- `stats.h` must not be included from more than one `.cpp` file (file-static symbols).
- GIF art should be cropped tight and optimized with `gifsicle --lossy=80 -O3 --colors 64`. The whole character pack must fit under ~1.8MB.
- The wire protocol contract is `REFERENCE.md`; this repo is a reference implementation, not the spec.
