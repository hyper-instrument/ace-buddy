# ACE buddy

M5StickC Plus 硬件伴侣，连接 Claude Code 终端，实时展示会话状态、token 用量、权限审批和任务进度。

基于 [claude-desktop-buddy](https://github.com/anthropics/claude-desktop-buddy) 开发，新增 Claude Code 集成。

<p align="center">
  <img src="docs/device.jpg" alt="M5StickC Plus running the buddy firmware" width="500">
</p>

## 功能

- 实时显示 Claude Code 会话数、token 用量
- 设备上直接 approve/deny 权限请求
- Tasks 页面展示当前进行中的任务（`~/.claude/tasks/`）
- 18 种 ASCII 宠物 + 自定义 GIF 角色
- 待机时钟（自动旋转）
- BLE / USB Serial 双模连接

## 快速开始

### 1. 安装

**方式一：通过 ace 项目**
```bash
cd ace
make install-buddy
```

**方式二：直接在 ace-buddy 目录**
```bash
cd ace-buddy
make install
```

安装会自动完成：
- 安装 Python 依赖（pyserial）
- 合并 hooks 到 `~/.claude/settings.json`
- 注册 Claude Code marketplace 插件（`/ace-buddy:*` 命令）

### 2. 刷固件

需要 [PlatformIO Core](https://docs.platformio.org/en/latest/core/installation/) 和 USB 连接设备：

```bash
make flash
# 或
pio run -t upload
```

首次刷写建议先擦除：
```bash
pio run -t erase && pio run -t upload
```

### 3. 启动 daemon

**在 Claude Code 中：**
```
/ace-buddy:start
```

**或手动：**
```bash
make start
# 或
bash scripts/start.sh
```

daemon 在 `127.0.0.1:9876` 监听，接收 Claude Code hook 事件并转发到设备。

### 4. 使用

daemon 启动后自动连接设备（先检测 USB serial，无则切 BLE），Claude Code 的 hook 事件会自动推送到设备。

## Makefile 命令

| 命令 | 说明 |
|------|------|
| `make install` | 安装（pyserial + hooks + 插件注册） |
| `make start` | 启动 bridge daemon |
| `make stop` | 停止 bridge daemon |
| `make status` | 查看 daemon 状态 |
| `make flash` | 编译并刷写固件（需 PlatformIO + USB） |
| `make uninstall` | 卸载（停止 daemon + 清理插件 + 清理状态） |

## Claude Code slash 命令

插件安装后，在 Claude Code 中可用（命令带 `ace-buddy:` 命名空间前缀）：

| 命令 | 说明 |
|------|------|
| `/ace-buddy:start` | 启动 bridge daemon |
| `/ace-buddy:stop` | 停止 daemon |
| `/ace-buddy:status` | 查看 daemon + 设备状态 |
| `/ace-buddy:install` | 完整安装（依赖、hooks、固件、daemon） |
| `/ace-buddy:flash` | 重新刷写固件 |

## 设备按键

|                         | Normal               | Pet         | Info        | Tasks       | Approval    |
| ----------------------- | -------------------- | ----------- | ----------- | ----------- | ----------- |
| **A** (前面)            | 切换页面              | 切换页面     | 切换页面     | 切换页面     | **approve** |
| **B** (右侧)            | 滚动 transcript      | 翻页        | 翻页        | 滚动任务     | **deny**    |
| **长按 A**              | 菜单                 | 菜单        | 菜单        | 菜单        | 菜单        |
| **Power** (左侧短按)    | 关屏                 |             |             |             |             |
| **晃动**                | dizzy                |             |             |             | —           |
| **倒扣**                | 休眠（恢复能量）      |             |             |             |             |

页面循环：Normal → Pet → Info → **Tasks** → Normal → ...

## Tasks 页面

展示 `~/.claude/tasks/` 下的任务列表：

| 符号 | 颜色 | 状态 |
|------|------|------|
| `-` | 灰色 | pending |
| `>` | 橙色 | in_progress |
| `+` | 绿色 | completed |

Bridge daemon 每 5 秒轮询一次任务文件，最多显示 8 个。

## 架构

```
Claude Code hook ──POST──> bridge daemon (127.0.0.1:9876) ──serial/BLE──> ACE buddy 设备
                                  ^                                            |
                                  +──────────── permission ack ────────────────+

飞书模式（可选）：
Claude Code hook ──POST──> bridge daemon ──Feishu API──> 飞书交互卡片
                               ^   ^                          |
                               │   └──── WebSocket ───────────┘
                               └──────── permission reply
```

- **Bridge daemon** (`tools/claude_code_bridge.py`): Python HTTP 服务，接收 Claude Code hooks，转为 heartbeat JSON 发送到设备；也支持飞书模式，将权限确认推送到飞书交互卡片
- **Heartbeat 协议**: 每 ~10s 或状态变化时发送，包含 sessions、tokens、prompt、tasks 等数据
- **设备固件**: M5StickC Plus (ESP32)，基于 Arduino + M5Unified

## 七种状态

| 状态 | 触发 | 表现 |
|------|------|------|
| `sleep` | 未连接 | 闭眼，慢呼吸 |
| `idle` | 已连接，无事件 | 眨眼，四处看 |
| `busy` | 会话运行中 | 冒汗，忙碌 |
| `attention` | 权限待审批 | 警觉，LED 闪烁 |
| `celebrate` | 每 50K token 升级 | 庆祝，蹦跳 |
| `dizzy` | 摇晃设备 | 转圈眼 |
| `heart` | 5 秒内 approve | 飘爱心 |

## Hardware

固件基于 ESP32 Arduino 框架，使用 M5Unified 库。支持：
- M5StickC Plus
- M5StickC Plus2
- M5StickS3

## 项目结构

```
src/
  main.cpp       — 主循环、状态机、UI 页面
  buddy.cpp      — ASCII 宠物渲染
  buddies/       — 18 种宠物动画
  ble_bridge.cpp — Nordic UART BLE 服务
  character.cpp  — GIF 解码渲染
  data.h         — 通信协议、JSON 解析
  xfer.h         — 文件传输
  stats.h        — NVS 存储（统计、设置、宠物名）
tools/
  claude_code_bridge.py — Bridge daemon
scripts/         — daemon 生命周期脚本 (start/stop/status/flash/install)
hooks/           — Claude Code hooks 配置 (hooks.json)
commands/        — Claude Code slash 命令 (/ace-buddy:start, /ace-buddy:stop, ...)
characters/      — GIF 角色包
.claude-plugin/  — Claude Code marketplace 插件元数据
```

## ASCII 宠物

18 种宠物，每种 7 个动画状态。菜单 → "next pet" 循环切换，选择保存到 NVS。

## GIF 角色

支持自定义 GIF 角色包，通过 BLE 传输到设备。角色包格式见 `characters/bufo/`。

## 配置

所有配置统一放在项目根目录的 `.env` 文件，bridge 启动时自动加载。已有的 `.env` 是 `feishu-claude-code` 共用的一套凭证。

```bash
# 设备连接方式（auto / serial / ble / none）
BUDDY_TRANSPORT=auto
BUDDY_BUDGET=200000

# 飞书权限确认：填了 FEISHU_APP_ID + FEISHU_APP_SECRET 就会自动启用飞书模式（无需 ESP32 设备）
# BUDDY_FEISHU_USER_ID 可以留空，第一次给 bot 发消息时会自动配对
FEISHU_APP_ID=cli_xxxxxxxxxxxxxxxx
FEISHU_APP_SECRET=xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
```

| 变量 | 说明 |
|------|------|
| `BUDDY_TRANSPORT` | 连接方式：`auto`/`serial`/`ble`/`none` |
| `BUDDY_BUDGET` | context window 预算 |
| `FEISHU_APP_ID` | 飞书应用 ID |
| `FEISHU_APP_SECRET` | 飞书应用密钥 |
| `BUDDY_FEISHU_USER_ID` | （可选）预绑定用户 `open_id`，留空则自动配对 |

### 飞书模式

不想带 ESP32 设备？在 `.env` 里填好 `FEISHU_APP_ID` + `FEISHU_APP_SECRET`，然后直接启动：

```bash
/buddy start
# 或手动
python3 tools/claude_code_bridge.py
```

**自动配对：** 在飞书给 bot 发任意一条消息，bridge 就会自动记住你的 `open_id`，之后权限确认卡片就会推送到你这里。

**交互确认：** 飞书模式下权限确认会通过交互卡片推送到你的手机，点击「确认 / 取消 / 选项」即可。

**完全独立：** bridge 自己启动飞书 WebSocket 客户端，不需要 `feishu-claude-code`，不需要 ngrok，不需要配置飞书回调地址。

详见 [CLAUDE.md](CLAUDE.md) 中的「Feishu (Lark) Mode」章节。

## 协议参考

设备通信协议详见 [REFERENCE.md](REFERENCE.md)。
