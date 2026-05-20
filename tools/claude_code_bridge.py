#!/usr/bin/env python3
"""Bridge Claude Code <-> ace-buddy hardware device.

Replaces Claude Desktop's built-in BLE bridge so the same ESP32 buddy
firmware works with Claude Code's terminal-based sessions.

Hook flow:

  Claude Code hook  --POST-->  this daemon  --serial/BLE-->  ace-buddy
                                     ^                           |
                                     +------ permission ack -----+

Two transports:
  - USB serial: zero-setup, autodetects /dev/cu.usbserial-* or /dev/ttyUSB*.
  - BLE (Nordic UART Service via bleak): wireless, first connect triggers
    macOS system pairing dialog.

Heartbeat extensions beyond the stock desktop protocol (firmware ignores
unknown fields, so backward-compatible):
  project / branch / dirty    -- session's git context
  budget                      -- context window budget bar
  model                       -- current Claude model
  assistant_msg               -- last prose reply pulled from transcript
  prompt.body                 -- full approval content (diff / command)
  prompt.kind                 -- "permission" or "question"
  prompt.options              -- AskUserQuestion options

Usage:
    python3 tools/claude_code_bridge.py                    # auto: serial first, else BLE
    python3 tools/claude_code_bridge.py --transport ble    # force BLE
    python3 tools/claude_code_bridge.py --transport serial # force serial
    python3 tools/claude_code_bridge.py --budget 200000
"""

import argparse
import asyncio
import glob
import json
import os
import ssl
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from collections import deque
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# Load environment variables from .env in the repo root.
_script_dir = os.path.dirname(os.path.abspath(__file__))
_repo_root = os.path.dirname(_script_dir)
for _dotenv_path in (os.path.join(_repo_root, ".env"), os.path.join(os.getcwd(), ".env")):
    if os.path.exists(_dotenv_path):
        try:
            from dotenv import load_dotenv
            load_dotenv(_dotenv_path, override=True)
        except ImportError:
            pass
        break

# Nordic UART Service UUIDs -- match the firmware's ble_bridge.cpp.
NUS_SERVICE_UUID = "6e400001-b5a3-f393-e0a9-e50e24dcca9e"
NUS_RX_UUID      = "6e400002-b5a3-f393-e0a9-e50e24dcca9e"   # central -> device (write)
NUS_TX_UUID      = "6e400003-b5a3-f393-e0a9-e50e24dcca9e"   # device -> central (notify)

# ---------------------------------------------------------------------------
# Shared state
# ---------------------------------------------------------------------------

STATE_LOCK = threading.Lock()

SESSIONS_RUNNING = set()
SESSIONS_TOTAL   = set()
SESSIONS_WAITING = set()
SESSION_META     = {}          # sid -> {cwd, project, branch, dirty, checked_at}
TRANSCRIPT       = deque(maxlen=8)
TOKENS_TOTAL     = 0
TOKENS_TODAY     = 0
ACTIVE_PROMPT    = None        # currently-focused prompt shown on device
PENDING_PROMPTS  = {}          # prompt_id -> prompt dict (all unresolved)
PENDING          = {}          # prompt_id -> {"event", "decision"}

BUDGET_LIMIT        = 0
MODEL_NAME          = ""
ASSISTANT_MSG       = ""
SESSION_ASSISTANT   = {}       # sid -> latest assistant text (per-session)
FOCUSED_SID         = None     # user-picked focused session (for dashboard)
TRANSPORT           = None
BUMP_EVENT          = threading.Event()

# ---------------------------------------------------------------------------
# Feishu (Lark) mode -- send permission prompts via Feishu interactive cards
# ---------------------------------------------------------------------------

FEISHU_MODE         = False
FEISHU_USER_ID      = ""
FEISHU_APP_ID       = ""
FEISHU_APP_SECRET   = ""
FEISHU_CLIENT       = None     # _FeishuClient instance when mode is active

# Tools that Claude Code does not prompt for in default/acceptEdits mode.
# The buddy device should not intercept them either.
_READONLY_TOOLS = {
    "Read", "Glob", "Grep",
    "TaskList", "TaskGet", "TaskCreate", "TaskUpdate",
    "WebSearch", "WebFetch",
    "SendMessage",
    "CronList", "CronCreate", "CronDelete",
    "Skill",
}

# File-mutation tools that are auto-allowed in acceptEdits mode.
_EDIT_TOOLS = {"Edit", "Write", "MultiEdit"}


def log(*a, **kw):
    print(*a, file=sys.stderr, flush=True, **kw)


def now_hm():
    return datetime.now().strftime("%H:%M")


def add_transcript(line: str):
    with STATE_LOCK:
        TRANSCRIPT.appendleft(f"{now_hm()} {line[:80]}")


# ---------------------------------------------------------------------------
# Transport abstraction
# ---------------------------------------------------------------------------

class Transport:
    def start(self, on_byte, on_connect=None): raise NotImplementedError
    def write(self, data: bytes): raise NotImplementedError
    def connected(self) -> bool: raise NotImplementedError


class SerialTransport(Transport):
    def __init__(self, port):
        import serial
        self._port_name = port
        self.ser = serial.Serial(port, 115200, timeout=0.2)
        self._write_lock = threading.Lock()
        time.sleep(0.2)
        log(f"[serial] opened {port}")

    def start(self, on_byte, on_connect=None):
        if on_connect:
            on_connect()
        threading.Thread(target=self._reader, args=(on_byte,), daemon=True).start()

    def _reader(self, on_byte):
        while True:
            try:
                chunk = self.ser.read(256)
            except Exception as e:
                log(f"[serial] read fail: {e}")
                time.sleep(1)
                continue
            for b in chunk:
                on_byte(b)

    def write(self, data: bytes):
        with self._write_lock:
            try:
                self.ser.write(data)
            except Exception as e:
                log(f"[serial] write fail: {e}")

    def connected(self): return True


class BLETransport(Transport):
    """BLE Central via bleak. Scans for 'Claude-*', connects, subscribes."""

    def __init__(self, name_prefix="Claude-"):
        self._name_prefix = name_prefix
        self._loop  = None
        self._client = None
        self._thread = None
        self._on_byte = None
        self._on_connect = None
        self._connected_evt = threading.Event()

    def start(self, on_byte, on_connect=None):
        self._on_byte = on_byte
        self._on_connect = on_connect
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self):
        try:
            self._loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self._loop)
            self._loop.run_until_complete(self._main())
        except Exception as e:
            log(f"[ble] thread crashed: {e!r}")

    async def _main(self):
        try:
            from bleak import BleakScanner, BleakClient
        except ImportError:
            log("[ble] bleak not installed. run: pip install bleak")
            return

        while True:
            log(f"[ble] scanning for '{self._name_prefix}*'...")
            device = None
            try:
                device = await BleakScanner.find_device_by_filter(
                    lambda d, ad: bool(d.name) and d.name.startswith(self._name_prefix),
                    timeout=10.0,
                )
            except Exception as e:
                log(f"[ble] scan error: {e}")

            if not device:
                log("[ble] no device found, retrying in 5s")
                await asyncio.sleep(5)
                continue

            log(f"[ble] connecting to {device.name} ({device.address})")
            try:
                async with BleakClient(device) as client:
                    self._client = client

                    def _on_notify(_sender, data: bytearray):
                        for b in data:
                            self._on_byte(b)
                    await client.start_notify(NUS_TX_UUID, _on_notify)

                    self._connected_evt.set()
                    log("[ble] connected")
                    if self._on_connect:
                        threading.Thread(
                            target=self._on_connect, daemon=True,
                            name="ble-handshake",
                        ).start()

                    while client.is_connected:
                        await asyncio.sleep(1.0)
                    log("[ble] link lost")
            except Exception as e:
                log(f"[ble] client error: {e!r}")
            finally:
                self._client = None
                self._connected_evt.clear()

            await asyncio.sleep(2)

    def write(self, data: bytes):
        client = self._client
        if client is None or not client.is_connected:
            return
        try:
            fut = asyncio.run_coroutine_threadsafe(
                client.write_gatt_char(NUS_RX_UUID, data, response=False),
                self._loop,
            )
            fut.result(timeout=3)
        except Exception as e:
            log(f"[ble] write fail: {e!r}")

    def connected(self): return self._connected_evt.is_set()


# ---------------------------------------------------------------------------
# Feishu (Lark) lightweight client (std-lib only, no lark_oapi dep)
# ---------------------------------------------------------------------------

class _FeishuClient:
    """Sync Feishu API client for sending interactive cards."""

    _TOKEN_URL = "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal"
    _MSG_URL = "https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type=open_id"

    def __init__(self, app_id: str, app_secret: str):
        self.app_id = app_id
        self.app_secret = app_secret
        self._token = None
        self._token_expires = 0.0
        self._ctx = ssl.create_default_context()

    def _ensure_token(self) -> str:
        if self._token and time.time() < self._token_expires - 60:
            return self._token
        body = json.dumps({"app_id": self.app_id, "app_secret": self.app_secret}).encode()
        req = urllib.request.Request(
            self._TOKEN_URL, data=body,
            headers={"Content-Type": "application/json"}, method="POST")
        try:
            with urllib.request.urlopen(req, context=self._ctx, timeout=10) as r:
                data = json.loads(r.read())
                self._token = data["tenant_access_token"]
                self._token_expires = time.time() + data.get("expire", 7200)
                return self._token
        except Exception as e:
            log(f"[feishu] token error: {e}")
            raise

    def _api(self, req: urllib.request.Request) -> dict:
        req.add_header("Authorization", f"Bearer {self._ensure_token()}")
        try:
            with urllib.request.urlopen(req, context=self._ctx, timeout=10) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", "replace")
            log(f"[feishu] HTTP {e.code}: {body}")
            raise

    def send_card(self, receive_id: str, elements: list) -> str:
        """Send an interactive card. Returns message_id or empty string."""
        card = json.dumps({"schema": "2.0", "body": {"elements": elements}}, ensure_ascii=False)
        body = json.dumps({
            "receive_id": receive_id,
            "msg_type": "interactive",
            "content": card,
        }, ensure_ascii=False).encode()
        req = urllib.request.Request(self._MSG_URL, data=body,
                                     headers={"Content-Type": "application/json"}, method="POST")
        resp = self._api(req)
        return (resp.get("data") or {}).get("message_id", "")


def _build_permission_card(prompt_obj: dict) -> list:
    """Build Feishu card elements for a permission / question prompt."""
    tool = prompt_obj["tool"]
    body_text = prompt_obj.get("body", "")
    kind = prompt_obj.get("kind", "permission")
    pid = prompt_obj["id"]
    opts = prompt_obj.get("option_labels") or []

    elements = []
    header = f"**🤖 Claude 请求确认**\n\n**{tool}**"
    if body_text:
        header += f"\n\n```\n{body_text[:400]}\n```"
    elements.append({"tag": "markdown", "content": header})

    if kind == "question" and opts:
        # AskUserQuestion option buttons
        buttons = []
        for i, label in enumerate(opts[:4]):
            buttons.append({
                "tag": "button",
                "text": {"tag": "plain_text", "content": label[:20]},
                "type": "primary" if i == 0 else "default",
                "size": "small",
                "value": {"action": "buddy_option", "pid": pid, "idx": i},
                "behaviors": [{"type": "callback", "value": {"action": "buddy_option", "pid": pid, "idx": i}}],
            })
        # Flow layout for short labels, vertical otherwise
        short = all(len(lb) <= 10 for lb in opts[:4])
        if short and buttons:
            columns = [{"tag": "column", "width": "auto", "elements": [b]} for b in buttons]
            elements.append({"tag": "column_set", "flex_mode": "flow", "columns": columns})
        else:
            elements.extend(buttons)
    else:
        # Allow / Deny buttons
        elements.append({
            "tag": "column_set",
            "flex_mode": "flow",
            "columns": [
                {"tag": "column", "width": "auto", "elements": [{
                    "tag": "button",
                    "text": {"tag": "plain_text", "content": "✅ 确认"},
                    "type": "primary",
                    "size": "small",
                    "value": {"action": "buddy_allow", "pid": pid},
                    "behaviors": [{"type": "callback", "value": {"action": "buddy_allow", "pid": pid}}],
                }]},
                {"tag": "column", "width": "auto", "elements": [{
                    "tag": "button",
                    "text": {"tag": "plain_text", "content": "❌ 取消"},
                    "type": "default",
                    "size": "small",
                    "value": {"action": "buddy_deny", "pid": pid},
                    "behaviors": [{"type": "callback", "value": {"action": "buddy_deny", "pid": pid}}],
                }]},
            ]
        })
    return elements


def _handle_feishu_callback(payload: dict) -> dict:
    """Process a Feishu card action callback. Returns HTTP response body."""
    event = payload.get("event", {})
    action = event.get("action", {})
    value = action.get("value", {})
    pid = value.get("pid", "")
    action_type = value.get("action", "")

    if not pid:
        return {}

    h = PENDING.get(pid)
    if not h:
        log(f"[feishu] callback for unknown prompt {pid}")
        return {}

    decision = None
    toast = "已处理"
    if action_type == "buddy_allow":
        decision = "once"
        toast = "✅ 已确认"
    elif action_type == "buddy_deny":
        decision = "deny"
        toast = "❌ 已取消"
    elif action_type == "buddy_option":
        idx = value.get("idx", -1)
        decision = f"option:{idx}"
        toast = f"已选择选项 {idx + 1}"

    if decision is not None:
        h["decision"] = decision
        h["event"].set()
        log(f"[feishu] prompt {pid} -> {decision}")
    return {"toast": {"type": "info", "content": toast}}


# ---------------------------------------------------------------------------
# Line-based RX parsing
# ---------------------------------------------------------------------------

_rx_buf = bytearray()


def on_rx_byte(b: int):
    global _rx_buf
    if b in (0x0A, 0x0D):
        if _rx_buf:
            raw = bytes(_rx_buf)
            _rx_buf = bytearray()
            try:
                line = raw.decode("utf-8", errors="replace")
            except Exception:
                return
            log(f"[dev<] {line}")
            if not line.startswith("{"):
                return
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                return
            cmd = obj.get("cmd")
            if cmd == "permission":
                pid = obj.get("id")
                h = PENDING.get(pid)
                if h:
                    h["decision"] = obj.get("decision")
                    h["event"].set()
            elif cmd == "focus_session":
                global FOCUSED_SID
                FOCUSED_SID = obj.get("sid") or None
                BUMP_EVENT.set()
    else:
        if len(_rx_buf) < 4096:
            _rx_buf.append(b)


def send_line(obj: dict):
    if TRANSPORT is None:
        return
    data = (json.dumps(obj, separators=(",", ":"), ensure_ascii=False) + "\n").encode()
    TRANSPORT.write(data)


# ---------------------------------------------------------------------------
# Git / project introspection
# ---------------------------------------------------------------------------

GIT_TTL_SEC = 10


def _git(cwd, *args, timeout=2.0):
    try:
        out = subprocess.run(("git", *args), cwd=cwd, capture_output=True,
                             text=True, timeout=timeout, check=False)
        return out.stdout.strip() if out.returncode == 0 else ""
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return ""


def refresh_git(sid: str, cwd: str):
    if not cwd or not os.path.isdir(cwd):
        return
    now = time.time()
    meta = SESSION_META.get(sid) or {}
    if meta.get("cwd") == cwd and (now - meta.get("checked_at", 0)) < GIT_TTL_SEC:
        return
    root = _git(cwd, "rev-parse", "--show-toplevel") or cwd
    SESSION_META[sid] = {
        "cwd": cwd,
        "project":    os.path.basename(root.rstrip("/"))[:39] or "",
        "branch":     _git(cwd, "rev-parse", "--abbrev-ref", "HEAD")[:39],
        "dirty":      sum(1 for ln in _git(cwd, "status", "--porcelain").splitlines() if ln.strip()),
        "checked_at": now,
    }


# ---------------------------------------------------------------------------
# Tool -> display hint + body
# ---------------------------------------------------------------------------

HINT_FIELDS = {
    "Bash": "command", "Edit": "file_path", "MultiEdit": "file_path",
    "Write": "file_path", "Read": "file_path", "NotebookEdit": "notebook_path",
    "WebFetch": "url", "WebSearch": "query",
    "Glob": "pattern", "Grep": "pattern",
}


def hint_from_tool(tool: str, tin: dict) -> str:
    field = HINT_FIELDS.get(tool)
    if field and isinstance((tin or {}).get(field), str):
        return tin[field]
    for v in (tin or {}).values():
        if isinstance(v, str):
            return v
    return json.dumps(tin or {})[:60]


def body_from_tool(tool: str, tin: dict) -> str:
    tin = tin or {}

    if tool == "AskUserQuestion":
        qs = tin.get("questions")
        if isinstance(qs, list) and qs and isinstance(qs[0], dict):
            q = qs[0].get("question") or qs[0].get("header") or ""
        else:
            q = tin.get("question", "")
        return (q or "").strip()[:500]

    if tool == "Bash":
        cmd  = tin.get("command", "")
        desc = tin.get("description", "")
        return (f"{desc}\n\n$ {cmd}" if desc else f"$ {cmd}")[:500]

    if tool in ("Edit", "MultiEdit"):
        path = tin.get("file_path", "")
        oldv = str(tin.get("old_string", ""))[:180]
        newv = str(tin.get("new_string", ""))[:180]
        return f"{path}\n\n--- old\n{oldv}\n\n+++ new\n{newv}"

    if tool == "Write":
        path    = tin.get("file_path", "")
        content = str(tin.get("content", ""))
        head    = content[:320]
        return f"{path}\n\n{head}{('...' if len(content) > 320 else '')}"

    if tool == "Read":
        return tin.get("file_path", "")

    if tool == "WebFetch":
        url = tin.get("url", "")
        prompt = str(tin.get("prompt", ""))[:200]
        return f"{url}\n\n{prompt}" if prompt else url

    if tool == "WebSearch":
        return str(tin.get("query", ""))[:300]

    if tool in ("Glob", "Grep"):
        parts = [f"pattern: {tin.get('pattern', '')}"]
        if tin.get("path"): parts.append(f"path: {tin['path']}")
        if tin.get("type"): parts.append(f"type: {tin['type']}")
        return "\n".join(parts)[:300]

    try:
        return json.dumps(tin, indent=2)[:500]
    except Exception:
        return str(tin)[:500]


# ---------------------------------------------------------------------------
# Task scanning (~/.claude/tasks/)
# ---------------------------------------------------------------------------

TASKS_DIR = os.path.join(os.path.expanduser("~"), ".claude", "tasks")
_tasks_cache: list = []
_tasks_cache_at: float = 0.0
TASKS_TTL_SEC = 5


def scan_tasks() -> list:
    """Scan ~/.claude/tasks/<team>/*.json for task entries."""
    global _tasks_cache, _tasks_cache_at
    now = time.time()
    if now - _tasks_cache_at < TASKS_TTL_SEC:
        return _tasks_cache

    results = []
    try:
        if not os.path.isdir(TASKS_DIR):
            _tasks_cache = []
            _tasks_cache_at = now
            return results
        for team in os.listdir(TASKS_DIR):
            team_dir = os.path.join(TASKS_DIR, team)
            if not os.path.isdir(team_dir):
                continue
            for fname in os.listdir(team_dir):
                if not fname.endswith(".json"):
                    continue
                fpath = os.path.join(team_dir, fname)
                try:
                    with open(fpath) as f:
                        task = json.load(f)
                    if not isinstance(task, dict):
                        continue
                    results.append({
                        "id": str(task.get("id", "")),
                        "subject": (task.get("subject", "") or "")[:40],
                        "status": task.get("status", "pending"),
                    })
                except (json.JSONDecodeError, OSError, ValueError):
                    continue
    except OSError:
        pass

    _tasks_cache = results
    _tasks_cache_at = now
    return results


# ---------------------------------------------------------------------------
# Heartbeat construction
# ---------------------------------------------------------------------------

# Firmware line buffer is 1024 bytes.  Keep heartbeat JSON well under that
# limit so that deserializeJson never fails silently.
_HB_MAX_BYTES = 900


def build_heartbeat() -> dict:
    with STATE_LOCK:
        msg = (f"approve: {ACTIVE_PROMPT['tool']}" if ACTIVE_PROMPT
               else (TRANSCRIPT[0][6:] if TRANSCRIPT else "idle"))
        hb = {
            "total":        len(SESSIONS_TOTAL),
            "running":      len(SESSIONS_RUNNING),
            "waiting":      len(SESSIONS_WAITING),
            "msg":          msg[:23],
            "entries":      list(TRANSCRIPT)[:4],
            "tokens":       0,
            "tokens_today": 0,
        }
        if ACTIVE_PROMPT:
            p = {
                "id":   ACTIVE_PROMPT["id"],
                "tool": ACTIVE_PROMPT["tool"][:19],
                "hint": ACTIVE_PROMPT["hint"][:43],
                "body": ACTIVE_PROMPT["body"][:80],
                "kind": ACTIVE_PROMPT.get("kind", "permission"),
            }
            opts = ACTIVE_PROMPT.get("option_labels") or []
            if opts: p["options"] = [o[:16] for o in opts[:4]]
            sid = ACTIVE_PROMPT.get("session_id", "")
            if sid:
                p["sid"] = sid[:8]
                meta = SESSION_META.get(sid) or {}
                p["project"] = meta.get("project", "")[:23]
            hb["prompt"] = p

        sessions_list = []
        for sid in list(SESSIONS_TOTAL)[:2]:
            meta = SESSION_META.get(sid) or {}
            sessions_list.append({
                "sid":     sid[:8],
                "proj":    (meta.get("project", "") or "")[:16],
                "branch":  (meta.get("branch", "") or "")[:12],
                "dirty":   meta.get("dirty", 0),
                "running": sid in SESSIONS_RUNNING,
                "waiting": sid in SESSIONS_WAITING,
            })
        if sessions_list:
            hb["sessions"] = sessions_list
        if BUDGET_LIMIT > 0:   hb["budget"] = BUDGET_LIMIT

        sid = None
        if FOCUSED_SID and FOCUSED_SID in SESSION_META:
            sid = FOCUSED_SID
        elif ACTIVE_PROMPT and ACTIVE_PROMPT.get("session_id"):
            sid = ACTIVE_PROMPT["session_id"]
        elif SESSIONS_RUNNING:
            sid = next(iter(SESSIONS_RUNNING))
        elif SESSION_META:
            sid = max(SESSION_META, key=lambda s: SESSION_META[s].get("checked_at", 0))

        if sid and sid in SESSION_META:
            m = SESSION_META[sid]
            hb["project"] = m.get("project", "")
            hb["branch"]  = m.get("branch", "")
            hb["dirty"]   = m.get("dirty", 0)

        if sid:
            ctx = SESSION_CONTEXT.get(sid, 0)
            hb["tokens"] = ctx
            hb["tokens_today"] = ctx

        s_model = SESSION_MODEL.get(sid) if sid else None
        if s_model:       hb["model"] = s_model
        elif MODEL_NAME:   hb["model"] = MODEL_NAME

    tasks = [{**t, "subject": t["subject"][:20]} for t in scan_tasks()[:8]]
    hb["tasks"] = tasks

    # Safety: if heartbeat exceeds firmware buffer, progressively drop
    # optional fields until it fits.
    raw = json.dumps(hb, separators=(",", ":"), ensure_ascii=False)
    if len(raw) > _HB_MAX_BYTES:
        hb.pop("sessions", None)
        raw = json.dumps(hb, separators=(",", ":"), ensure_ascii=False)
    if len(raw) > _HB_MAX_BYTES:
        hb["entries"] = hb.get("entries", [])[:2]
        raw = json.dumps(hb, separators=(",", ":"), ensure_ascii=False)
    if len(raw) > _HB_MAX_BYTES:
        hb.pop("entries", None)
        hb["tasks"] = hb.get("tasks", [])[:4]
        raw = json.dumps(hb, separators=(",", ":"), ensure_ascii=False)
    if len(raw) > _HB_MAX_BYTES:
        if "prompt" in hb:
            hb["prompt"]["body"] = hb["prompt"]["body"][:30]

    return hb


def heartbeat_loop():
    """Send heartbeat on state change or every 10s idle."""
    MIN_INTERVAL = 1.0
    last_sent = 0.0
    while True:
        BUMP_EVENT.wait(timeout=10)
        BUMP_EVENT.clear()
        now = time.time()
        since = now - last_sent
        if since < MIN_INTERVAL:
            time.sleep(MIN_INTERVAL - since)
        send_line(build_heartbeat())
        last_sent = time.time()


# ---------------------------------------------------------------------------
# Model + transcript helpers
# ---------------------------------------------------------------------------

def short_model(full: str) -> str:
    if not full: return ""
    import re
    s = full.lower()
    family = "Claude"
    for tag, label in (("opus", "Opus"), ("sonnet", "Sonnet"), ("haiku", "Haiku")):
        if tag in s:
            family = label; break
    m = re.search(r"(\d+)[\.\-](\d+)", s)
    if m: return f"{family} {m.group(1)}.{m.group(2)}"
    return family if family != "Claude" else full[:28]


def extract_session_context(path: str) -> int:
    """Return session's current context-window usage (input + output tokens)."""
    if not path or not os.path.exists(path):
        return 0
    try:
        sz = os.path.getsize(path)
        with open(path, "rb") as f:
            f.seek(max(0, sz - 131072))
            data = f.read()
        lines = data.decode("utf-8", errors="replace").splitlines()
        for line in reversed(lines):
            line = line.strip()
            if not line or not line.startswith("{"): continue
            try: obj = json.loads(line)
            except json.JSONDecodeError: continue
            msg = obj.get("message", obj)
            if not isinstance(msg, dict) or msg.get("role") != "assistant":
                continue
            usage = msg.get("usage")
            if isinstance(usage, dict):
                inp = int(usage.get("input_tokens", 0) or 0)
                out = int(usage.get("output_tokens", 0) or 0)
                return inp + out
    except Exception:
        pass
    return 0


SESSION_CONTEXT: dict = {}


def extract_session_model(path: str) -> str:
    """Find model from the most recent assistant message in transcript."""
    if not path or not os.path.exists(path):
        return ""
    try:
        sz = os.path.getsize(path)
        with open(path, "rb") as f:
            f.seek(max(0, sz - 131072))
            data = f.read()
        lines = data.decode("utf-8", errors="replace").splitlines()
        for line in reversed(lines):
            line = line.strip()
            if not line or not line.startswith("{"): continue
            try: obj = json.loads(line)
            except json.JSONDecodeError: continue
            msg = obj.get("message", obj)
            if not isinstance(msg, dict) or msg.get("role") != "assistant":
                continue
            m = msg.get("model")
            if isinstance(m, str) and m:
                return m
    except Exception:
        pass
    return ""


SESSION_MODEL: dict = {}


def extract_last_assistant(path: str) -> str:
    if not path or not os.path.exists(path):
        return ""
    try:
        sz = os.path.getsize(path)
        with open(path, "rb") as f:
            f.seek(max(0, sz - 131072))
            data = f.read()
        lines = data.decode("utf-8", errors="replace").splitlines()
        for line in reversed(lines):
            line = line.strip()
            if not line or not line.startswith("{"):
                continue
            try: obj = json.loads(line)
            except json.JSONDecodeError: continue
            msg = obj.get("message", obj)
            if not isinstance(msg, dict): continue
            if msg.get("role") != "assistant": continue
            content = msg.get("content")
            text = ""
            if isinstance(content, str):
                text = content
            elif isinstance(content, list):
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "text":
                        text = block.get("text", "")
                        if text: break
            text = (text or "").strip()
            if text:
                return " ".join(text.split())[:220]
    except Exception as e:
        log(f"[transcript] error: {e}")
    return ""


# ---------------------------------------------------------------------------
# Hook deduplication -- settings.json hooks + plugin auto-loaded hooks
# can both fire for the same event, causing duplicate processing.
# ---------------------------------------------------------------------------

_DEDUP_LOCK = threading.Lock()
_DEDUP_CACHE: dict = {}   # key -> timestamp
_DEDUP_TTL = 2.0          # seconds


def _is_duplicate_hook(payload: dict) -> bool:
    """Return True if this hook payload was already seen recently."""
    event = payload.get("hook_event_name", "")
    sid = payload.get("session_id", "")
    tool = payload.get("tool_name", "")
    key = f"{event}:{sid}:{tool}"
    now = time.time()
    with _DEDUP_LOCK:
        # Prune stale entries
        stale = [k for k, t in _DEDUP_CACHE.items() if now - t > _DEDUP_TTL * 5]
        for k in stale:
            del _DEDUP_CACHE[k]
        prev = _DEDUP_CACHE.get(key)
        if prev is not None and (now - prev) < _DEDUP_TTL:
            return True
        _DEDUP_CACHE[key] = now
    return False


# ---------------------------------------------------------------------------
# HTTP handler -- receives Claude Code hook payloads
# ---------------------------------------------------------------------------

class HookHandler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args): pass

    def do_POST(self):
        try:
            n = int(self.headers.get("Content-Length") or "0")
            body = self.rfile.read(n) if n > 0 else b""
            payload = json.loads(body.decode("utf-8")) if body else {}
        except Exception as e:
            return self._reply(400, {"error": str(e)})

        # ── Feishu callback (card action or URL verification) ──
        if payload.get("type") == "url_verification":
            return self._reply(200, {"challenge": payload.get("challenge", "")})

        # Feishu card action callbacks have event.action.value
        if payload.get("event", {}).get("action"):
            try:
                resp_body = _handle_feishu_callback(payload)
            except Exception as e:
                log(f"[feishu] callback error: {e!r}")
                resp_body = {}
            return self._reply(200, resp_body)

        # ── Claude Code hooks ──
        event = payload.get("hook_event_name", "")
        log(f"[hook] {event} session={payload.get('session_id', '')[:8]}")

        if _is_duplicate_hook(payload):
            log(f"[hook] duplicate {event}, skipping")
            return self._reply(200, {})

        sid = payload.get("session_id", "")
        cwd = payload.get("cwd", "")
        if sid and cwd:
            refresh_git(sid, cwd)

        global MODEL_NAME, ASSISTANT_MSG
        for k in ("model", "model_id", "assistant_model"):
            v = payload.get(k)
            if isinstance(v, str) and v:
                MODEL_NAME = short_model(v); break

        tp = payload.get("transcript_path")
        if isinstance(tp, str) and tp:
            if sid:
                m = extract_session_model(tp)
                if m:
                    SESSION_MODEL[sid] = short_model(m)
            latest = extract_last_assistant(tp)
            if latest:
                if sid and SESSION_ASSISTANT.get(sid) != latest:
                    SESSION_ASSISTANT[sid] = latest
                    BUMP_EVENT.set()
                if latest != ASSISTANT_MSG:
                    ASSISTANT_MSG = latest
                    BUMP_EVENT.set()
            if sid:
                ctx = extract_session_context(tp)
                if SESSION_CONTEXT.get(sid) != ctx:
                    SESSION_CONTEXT[sid] = ctx
                    BUMP_EVENT.set()

        try:
            if   event == "SessionStart":      resp = self._session_start(payload)
            elif event == "Stop":              resp = self._session_stop(payload)
            elif event == "UserPromptSubmit":  resp = self._user_prompt(payload)
            elif event == "PreToolUse":        resp = self._pretool(payload)
            elif event == "PostToolUse":       resp = self._posttool(payload)
            else:                              resp = {}
        except Exception as e:
            log(f"[hook] handler error: {e!r}"); resp = {}

        self._reply(200, resp)

    def _reply(self, code: int, obj: dict):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try: self.wfile.write(body)
        except BrokenPipeError: pass

    def _session_start(self, p):
        sid = p.get("session_id", "")
        with STATE_LOCK:
            SESSIONS_TOTAL.add(sid); SESSIONS_RUNNING.add(sid)
        proj = (SESSION_META.get(sid) or {}).get("project", "")
        add_transcript(f"session: {proj}" if proj else "session started")
        BUMP_EVENT.set()
        return {}

    def _session_stop(self, p):
        sid = p.get("session_id", "")
        with STATE_LOCK:
            SESSIONS_RUNNING.discard(sid)
            SESSIONS_TOTAL.discard(sid)
        add_transcript("session done"); BUMP_EVENT.set()
        return {}

    def _user_prompt(self, p):
        prompt = (p.get("prompt") or "").strip().replace("\n", " ")
        if prompt:
            add_transcript(f"> {prompt[:60]}"); BUMP_EVENT.set()
        return {}

    def _posttool(self, p):
        tool = p.get("tool_name", "?")
        add_transcript(f"{tool} done"); BUMP_EVENT.set()
        return {}

    def _pretool(self, p):
        global ACTIVE_PROMPT
        sid  = p.get("session_id", "")
        tool = p.get("tool_name", "?")
        tin  = p.get("tool_input") or {}
        mode = p.get("permission_mode", "default")

        if mode == "bypassPermissions" and tool != "AskUserQuestion":
            add_transcript(f"{tool} (bypass)")
            BUMP_EVENT.set()
            return {"hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "allow",
                "permissionDecisionReason": "bypass-permissions mode",
            }}

        # Read-only / lookup tools: Claude Code does not prompt for these,
        # so the buddy device should not either.
        if tool in _READONLY_TOOLS:
            return {}

        # acceptEdits mode auto-allows file mutations.
        if mode == "acceptEdits" and tool in _EDIT_TOOLS:
            add_transcript(f"{tool} (accept-edits)")
            BUMP_EVENT.set()
            return {"hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "allow",
                "permissionDecisionReason": "accept-edits mode",
            }}

        hint = hint_from_tool(tool, tin)
        body = body_from_tool(tool, tin)

        kind = "question" if tool == "AskUserQuestion" else "permission"
        option_labels = []
        if kind == "question":
            qs = tin.get("questions")
            if isinstance(qs, list) and qs and isinstance(qs[0], dict):
                for o in (qs[0].get("options") or [])[:4]:
                    option_labels.append(str(o.get("label")) if isinstance(o, dict) else str(o))
            else:
                for o in (tin.get("options") or [])[:4]:
                    option_labels.append(str(o.get("label")) if isinstance(o, dict) else str(o))

        prompt_id = f"req_{int(time.time() * 1000)}_{os.getpid()}_{threading.get_ident()}"
        event = threading.Event()
        holder = {"event": event, "decision": None}
        PENDING[prompt_id] = holder

        prompt_obj = {
            "id": prompt_id, "tool": tool, "hint": hint, "body": body,
            "kind": kind, "option_labels": option_labels, "session_id": sid,
        }

        with STATE_LOCK:
            SESSIONS_WAITING.add(sid)
            PENDING_PROMPTS[prompt_id] = prompt_obj
            if ACTIVE_PROMPT is None:
                ACTIVE_PROMPT = prompt_obj
        BUMP_EVENT.set()

        # ── Feishu mode: send interactive card to user ──
        if FEISHU_MODE and FEISHU_CLIENT and FEISHU_USER_ID:
            try:
                elements = _build_permission_card(prompt_obj)
                msg_id = FEISHU_CLIENT.send_card(FEISHU_USER_ID, elements)
                if msg_id:
                    log(f"[feishu] card sent for prompt {prompt_id}")
                else:
                    log(f"[feishu] card send failed for prompt {prompt_id}")
            except Exception as e:
                log(f"[feishu] send error: {e}")

        try:
            got = event.wait(timeout=30)
            decision = holder["decision"] if got else None
            if isinstance(decision, str) and decision.startswith("option:"):
                time.sleep(0.6)
        finally:
            PENDING.pop(prompt_id, None)
            with STATE_LOCK:
                SESSIONS_WAITING.discard(sid)
                PENDING_PROMPTS.pop(prompt_id, None)
                if ACTIVE_PROMPT and ACTIVE_PROMPT["id"] == prompt_id:
                    ACTIVE_PROMPT = next(iter(PENDING_PROMPTS.values()), None)
            BUMP_EVENT.set()

        if isinstance(decision, str) and decision.startswith("option:"):
            try: idx = int(decision.split(":", 1)[1])
            except ValueError: idx = -1
            label = option_labels[idx] if 0 <= idx < len(option_labels) else ""
            add_transcript(f"{tool} -> {label[:30]}"); BUMP_EVENT.set()
            source = "Feishu" if FEISHU_MODE else "ace-buddy device"
            return {"hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": (
                    f"The user answered on the {source}: "
                    f"'{label}' (option {idx + 1}). Proceed using this answer "
                    f"directly -- do NOT call AskUserQuestion again."
                ),
            }}

        if decision == "once":
            add_transcript(f"{tool} allow"); BUMP_EVENT.set()
            source = "Feishu" if FEISHU_MODE else "ace-buddy"
            return {"hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "allow",
                "permissionDecisionReason": f"Approved on {source}",
            }}
        if decision == "deny":
            add_transcript(f"{tool} deny"); BUMP_EVENT.set()
            source = "Feishu" if FEISHU_MODE else "ace-buddy device"
            if kind == "question":
                reason = (f"The user cancelled this question on the {source} "
                          "without answering. Ask them directly in the "
                          "terminal instead.")
            else:
                reason = f"Denied on {source}"
            return {"hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": reason,
            }}
        add_transcript(f"{tool} timeout"); BUMP_EVENT.set()
        return {}


# ---------------------------------------------------------------------------

def tz_offset_seconds() -> int:
    now = time.time()
    local = datetime.fromtimestamp(now)
    utc = datetime.fromtimestamp(now, tz=__import__('datetime').timezone.utc).replace(tzinfo=None)
    return int((local - utc).total_seconds())


# ---------------------------------------------------------------------------
# Feishu WebSocket handlers (lark_oapi)
# ---------------------------------------------------------------------------

def _start_feishu_ws(app_id: str, app_secret: str):
    """Start Feishu WebSocket client in a background thread."""
    try:
        import lark_oapi as lark
        from lark_oapi.api.im.v1.model import P2ImMessageReceiveV1
        from lark_oapi.event.callback.model.p2_card_action_trigger import (
            P2CardActionTrigger, P2CardActionTriggerResponse, CallBackToast,
        )
    except ImportError as e:
        log(f"[feishu] lark_oapi not installed, WebSocket unavailable: {e}")
        return

    def _on_message(event: P2ImMessageReceiveV1):
        global FEISHU_USER_ID, FEISHU_CLIENT
        sender = event.event.sender
        user_id = sender.sender_id.open_id
        if not user_id:
            return
        if not FEISHU_USER_ID:
            FEISHU_USER_ID = user_id
            log(f"[feishu] auto-paired user={user_id[:8]}...")
            if FEISHU_CLIENT:
                try:
                    elements = [
                        {"tag": "markdown", "content": "**🤖 ace-buddy 已配对**\n\n权限确认卡片将发送到这里。"}
                    ]
                    FEISHU_CLIENT.send_card(user_id, elements)
                except Exception as e:
                    log(f"[feishu] welcome card error: {e}")
        else:
            log(f"[feishu] message from user={user_id[:8]}...")

    def _on_card_action(data: P2CardActionTrigger) -> P2CardActionTriggerResponse:
        global _last_event
        event = data.event
        action = event.action
        value = action.value or {}
        pid = value.get("pid", "")
        action_type = value.get("action", "")

        resp = P2CardActionTriggerResponse()
        toast = CallBackToast()
        toast.type = "info"

        if not pid:
            toast.content = "无效请求"
            resp.toast = toast
            return resp

        h = PENDING.get(pid)
        if not h:
            log(f"[feishu] callback for unknown prompt {pid}")
            toast.content = "请求已过期"
            resp.toast = toast
            return resp

        decision = None
        if action_type == "buddy_allow":
            decision = "once"
            toast.content = "✅ 已确认"
        elif action_type == "buddy_deny":
            decision = "deny"
            toast.content = "❌ 已取消"
        elif action_type == "buddy_option":
            idx = value.get("idx", -1)
            decision = f"option:{idx}"
            toast.content = f"已选择选项 {idx + 1}"
        else:
            toast.content = "未知操作"
            resp.toast = toast
            return resp

        h["decision"] = decision
        h["event"].set()
        log(f"[feishu] prompt {pid} -> {decision}")
        resp.toast = toast
        return resp

    handler = lark.EventDispatcherHandler.builder("", "") \
        .register_p2_im_message_receive_v1(_on_message) \
        .register_p2_card_action_trigger(_on_card_action) \
        .build()

    ws_client = lark.ws.Client(
        app_id,
        app_secret,
        event_handler=handler,
        log_level=lark.LogLevel.WARNING,
    )

    def _run_ws():
        log("[feishu] WebSocket connecting...")
        try:
            ws_client.start()
        except Exception as e:
            log(f"[feishu] WebSocket error: {e}")

    t = threading.Thread(target=_run_ws, daemon=True, name="feishu-ws")
    t.start()


def pick_transport(kind: str, feishu_mode: bool = False) -> Transport | None:
    """Resolve --transport flag to a concrete Transport."""
    candidates = sorted(glob.glob("/dev/cu.usbserial-*") + glob.glob("/dev/ttyUSB*"))

    if kind == "none":
        log("[transport] disabled (feishu mode)")
        return None

    if kind == "serial":
        if not candidates:
            sys.exit("--transport serial requested but no serial device found")
        return SerialTransport(candidates[0])

    if kind == "ble":
        return BLETransport()

    # auto
    if candidates:
        log("[transport] serial device found, using USB")
        return SerialTransport(candidates[0])
    if feishu_mode:
        log("[transport] no serial device, skipping BLE (feishu mode)")
        return None
    log("[transport] no serial device, falling back to BLE")
    return BLETransport()


def main():
    global BUDGET_LIMIT, TRANSPORT, FEISHU_MODE, FEISHU_USER_ID, FEISHU_APP_ID, FEISHU_APP_SECRET, FEISHU_CLIENT

    ap = argparse.ArgumentParser(description="ace-buddy <-> Claude Code bridge daemon")
    ap.add_argument("--port", help="explicit serial port (implies --transport serial)")
    ap.add_argument("--transport", choices=("auto", "serial", "ble", "none"), default="auto")
    ap.add_argument("--http-port", type=int, default=9876)
    ap.add_argument("--owner", default=os.environ.get("USER", ""))
    ap.add_argument("--budget", type=int, default=200000,
                    help="context-window limit for the budget bar (default 200K)")
    args = ap.parse_args()

    BUDGET_LIMIT = max(0, args.budget)

    # ── Feishu mode setup ──
    # All config comes from .env (loaded above) or environment variables.
    # FEISHU_USER_ID can be auto-paired later from im.message.receive_v1 events.
    FEISHU_USER_ID = os.environ.get("BUDDY_FEISHU_USER_ID", "")
    FEISHU_APP_ID = os.environ.get("FEISHU_APP_ID", "")
    FEISHU_APP_SECRET = os.environ.get("FEISHU_APP_SECRET", "")
    FEISHU_MODE = bool(FEISHU_APP_ID and FEISHU_APP_SECRET)

    if FEISHU_MODE:
        FEISHU_CLIENT = _FeishuClient(FEISHU_APP_ID, FEISHU_APP_SECRET)
        if FEISHU_USER_ID:
            log(f"[feishu] mode enabled, pre-configured user={FEISHU_USER_ID[:8]}...")
        else:
            log("[feishu] mode enabled, waiting for user to send a message to auto-pair")
        _start_feishu_ws(FEISHU_APP_ID, FEISHU_APP_SECRET)
    else:
        log("[feishu] mode disabled (set FEISHU_APP_ID and FEISHU_APP_SECRET in .env to enable)")

    if args.port:
        TRANSPORT = SerialTransport(args.port)
    else:
        TRANSPORT = pick_transport(args.transport, feishu_mode=FEISHU_MODE)

    def _handshake():
        if args.owner:
            send_line({"cmd": "owner", "name": args.owner})
        send_line({"time": [int(time.time()), tz_offset_seconds()]})
        send_line(build_heartbeat())

    if TRANSPORT is not None:
        TRANSPORT.start(on_rx_byte, on_connect=_handshake)
        threading.Thread(target=heartbeat_loop, daemon=True).start()
    else:
        log("[transport] no device transport, heartbeat disabled")

    # Always listen on 127.0.0.1.  Feishu callbacks are proxied by
    # feishu-claude-code (WebSocket -> local POST) so we don't need
    # to expose this port publicly.
    srv = ThreadingHTTPServer(("127.0.0.1", args.http_port), HookHandler)
    srv.daemon_threads = True
    log(f"[http] listening on 127.0.0.1:{args.http_port}  budget={BUDGET_LIMIT}")
    log("[ready] start a Claude Code session with the hooks installed")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        log("\n[exit] bye")


if __name__ == "__main__":
    main()
