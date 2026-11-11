"""Zero-dependency MCP stdio server exposing omarchy-cu as tools.

Implements the subset of MCP needed by Claude Code and other clients:
initialize, ping, tools/list, tools/call (newline-delimited JSON-RPC 2.0 over stdio).
"""
from __future__ import annotations

import json
import os
import sys
import threading
import time
import traceback

from . import a11y, core
from .core import CUError

PROTOCOL_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")
AUTO_SHOT = os.environ.get("OMARCHY_CU_AUTO_SCREENSHOT", "1") not in ("0", "false", "no")
SETTLE = float(os.environ.get("OMARCHY_CU_SETTLE", "0.35"))

COORD_NOTE = ("Coordinates are in screen space = pixels of the full screenshot returned by `screenshot` "
              "(not zoom crops).")

_I = {"type": "integer"}
_N = {"type": "number"}
_S = {"type": "string"}
_B = {"type": "boolean"}
_SHOT = {"type": "boolean", "description": "Return a screenshot after the action (default true)."}


def _obj(props: dict, required: list[str] | None = None) -> dict:
    return {"type": "object", "properties": props, "required": required or [], "additionalProperties": False}


TOOLS: list[dict] = [
    {"name": "screenshot",
     "description": "Capture the screen (focused monitor by default) with cursor. Returns the image plus a text "
                    "summary of monitors, visible windows (boxes in screen space) and cursor position. "
                    + COORD_NOTE + " Use `zoom` to read small text, `find_text` to get exact coordinates of text.",
     "inputSchema": _obj({"monitor": {**_S, "description": "'focused' (default), 'all', monitor name or index"},
                          "window": {**_S, "description": "Optional: crop to a window (address, class or title substring)"}})},
    {"name": "zoom",
     "description": "High-resolution crop of a screen-space rectangle (for reading small text / icons). "
                    "The returned image is magnified; keep using full-screen coordinates for actions.",
     "inputSchema": _obj({"x": _N, "y": _N, "width": _N, "height": _N}, ["x", "y", "width", "height"])},
    {"name": "find_text",
     "description": "OCR the screen and return on-screen text matches with their center coordinates and boxes "
                    "(screen space). Case-insensitive; best match first: the exact label, then whole-word, "
                    "then substring matches (fuzzy fallback if none). Takes ~1-4 s. "
                    "Pass an empty query to list all text lines.",
     "inputSchema": _obj({"query": _S, "monitor": _S,
                          "region": {"type": "array", "items": _N, "minItems": 4, "maxItems": 4,
                                     "description": "[x, y, width, height] in screen space"}}, ["query"])},
    {"name": "click",
     "description": "Move to (x, y) and click. " + COORD_NOTE + " Omit x/y to click at the current cursor position.",
     "inputSchema": _obj({"x": _N, "y": _N,
                          "button": {"type": "string", "enum": ["left", "right", "middle", "back", "forward"]},
                          "count": {**_I, "description": "2 = double click, 3 = triple click"},
                          "modifiers": {"type": "array", "items": _S, "description": "e.g. ['ctrl'] or ['shift']"},
                          "screenshot": _SHOT})},
    {"name": "click_text",
     "description": "OCR the screen, find the given visible text and click its center. Fails if not found. "
                    "Matches are ranked as in find_text (exact label first); `index` picks among them "
                    "(the reply lists them).",
     "inputSchema": _obj({"text": _S, "index": _I,
                          "exact": {**_B, "description": "only whole-word matches of the full text, no substring/fuzzy"},
                          "button": {"type": "string", "enum": ["left", "right", "middle"]},
                          "count": _I, "screenshot": _SHOT}, ["text"])},
    {"name": "move_mouse", "description": "Move the cursor to (x, y) (hover). " + COORD_NOTE,
     "inputSchema": _obj({"x": _N, "y": _N, "screenshot": _SHOT}, ["x", "y"])},
    {"name": "drag", "description": "Press at (x1, y1), move smoothly to (x2, y2), release. " + COORD_NOTE,
     "inputSchema": _obj({"x1": _N, "y1": _N, "x2": _N, "y2": _N,
                          "button": {"type": "string", "enum": ["left", "right", "middle"]}, "screenshot": _SHOT},
                         ["x1", "y1", "x2", "y2"])},
    {"name": "scroll",
     "description": "Scroll the mouse wheel at (x, y) (or current position). dy > 0 scrolls down, dx > 0 right; "
                    "units are wheel notches (~3 lines each in most apps).",
     "inputSchema": _obj({"x": _N, "y": _N, "dy": _I, "dx": _I, "screenshot": _SHOT})},
    {"name": "mouse_button",
     "description": "Low-level press or release of a mouse button at the current position (for custom drags).",
     "inputSchema": _obj({"action": {"type": "string", "enum": ["down", "up"]},
                          "button": {"type": "string", "enum": ["left", "right", "middle"]}}, ["action"])},
    {"name": "type_text",
     "description": "Type text into the focused window (any Unicode, newlines become Enter). "
                    "Set via_clipboard=true for very long text (pastes with ctrl+v; terminals may need ctrl+shift+v).",
     "inputSchema": _obj({"text": _S, "via_clipboard": _B, "screenshot": _SHOT}, ["text"])},
    {"name": "key",
     "description": "Press a key or combo: 'Return', 'Escape', 'ctrl+l', 'ctrl+shift+t', 'super+Return', 'alt+Tab', "
                    "'F5', 'Page_Down'. Space-separated combos run in sequence ('ctrl+a BackSpace'). "
                    "Names are xkb keysyms; aliases enter/esc/backspace/del/pgup/pgdn/up/down/left/right work.",
     "inputSchema": _obj({"combo": _S, "repeat": _I, "screenshot": _SHOT}, ["combo"])},
    {"name": "windows",
     "description": "List windows (address, class, title, workspace, visible, focused, box in screen space).",
     "inputSchema": _obj({"all_workspaces": {**_B, "description": "default true"}})},
    {"name": "focus_window",
     "description": "Focus a window (switches workspace if needed). Selector: address '0x…', class, or title substring.",
     "inputSchema": _obj({"window": _S, "screenshot": _SHOT}, ["window"])},
    {"name": "close_window", "description": "Close a window (selector as in focus_window) or the focused one.",
     "inputSchema": _obj({"window": _S})},
    {"name": "workspace", "description": "Switch to a workspace: number, name, 'e+1', 'e-1', 'previous', 'special:scratchpad'.",
     "inputSchema": _obj({"target": _S, "screenshot": _SHOT}, ["target"])},
    {"name": "launch",
     "description": "Start an app and wait for its window. Either `app` = installed app name / desktop id as in the "
                    "launcher ('Files', 'Obsidian', 'Telegram') or `command` = shell command run via Hyprland "
                    "('nautilus ~/Downloads', 'omarchy-launch-browser', 'foot -e htop').",
     "inputSchema": _obj({"app": _S, "command": _S, "wait_seconds": {**_N, "description": "default 8; 0 = don't wait"},
                          "screenshot": _SHOT})},
    {"name": "ui_tree",
     "description": "Accessibility tree (AT-SPI) of a window: visible interactive elements with id, role, name, "
                    "states, actions, value and center coords (screen space). Exact and semantic — prefer it over "
                    "OCR for GTK/GNOME apps (Files, Settings, …), Qt apps and Chromium/Electron started with "
                    "accessibility on. Returns empty for apps that expose nothing.",
     "inputSchema": _obj({"window": {**_S, "description": "selector; default = focused window"},
                          "query": {**_S, "description": "filter by name/role substring"},
                          "all_roles": {**_B, "description": "include non-interactive named elements (labels, headings)"},
                          "limit": _I})},
    {"name": "ui_action",
     "description": "Act on a ui_tree element by id without moving the mouse: action 'click' (default action), "
                    "'focus', 'set_text' (with text), or any action name the element lists. Re-run ui_tree if the id is stale.",
     "inputSchema": _obj({"id": _S, "action": _S, "text": _S, "window": _S, "screenshot": _SHOT}, ["id"])},
    {"name": "window",
     "description": "Hyprland window management: action = focus | close | float | tile | toggle_float | fullscreen | "
                    "maximize | move_to_workspace | send_to_workspace (stay here) | move (x,y) | resize (width,height) | "
                    "swap / move_direction / focus_direction (direction) | pin. Sizes/positions in screen space. "
                    "window = selector (default focused).",
     "inputSchema": _obj({"action": _S, "window": _S, "workspace": _S, "x": _N, "y": _N, "width": _N, "height": _N,
                          "direction": {"type": "string", "enum": ["left", "right", "up", "down"]},
                          "screenshot": _SHOT}, ["action"])},
    {"name": "apps",
     "description": "Search installed desktop applications (name, desktop id). Launch one with launch(app=...).",
     "inputSchema": _obj({"query": _S})},
    {"name": "omarchy",
     "description": "Run the Omarchy command center natively: `omarchy <args>` — e.g. 'theme list', 'theme set "
                    "tokyo-night', 'font list', 'audio output volume +10', 'brightness display 60%', "
                    "'toggle nightlight', 'screenshot'. Use omarchy_commands to discover commands. "
                    "Never run [sudo] commands or 'update' without asking the user.",
     "inputSchema": _obj({"args": _S}, ["args"])},
    {"name": "omarchy_commands",
     "description": "Search the Omarchy command catalog (~400 system commands: themes, fonts, audio, display, wifi, "
                    "bluetooth, power, capture, toggles, menus). All words of the query must match.",
     "inputSchema": _obj({"query": _S})},
    {"name": "notify",
     "description": "Show a desktop notification to the user (e.g. task finished, need attention).",
     "inputSchema": _obj({"title": _S, "body": _S,
                          "urgency": {"type": "string", "enum": ["low", "normal", "critical"]}}, ["title"])},
    {"name": "ask_user",
     "description": "Ask the user a question through a native Omarchy popup on their screen and wait for the answer: "
                    "a pick list when options are given, free text otherwise. Use when the user is at the desktop "
                    "rather than the terminal (e.g. confirm before an irreversible GUI action). Returns null if dismissed.",
     "inputSchema": _obj({"question": _S, "options": {"type": "array", "items": _S},
                          "timeout_seconds": _N}, ["question"])},
    {"name": "wait_for",
     "description": "Wait until text appears on screen (OCR) or a window (class/title) exists — or disappears with "
                    "gone=true. Better than fixed sleeps for loading screens.",
     "inputSchema": _obj({"text": _S, "window": _S, "gone": _B, "timeout_seconds": _N, "screenshot": _SHOT})},
    {"name": "keybindings",
     "description": "List the user's Hyprland keybindings (combo + description), optionally filtered "
                    "(e.g. 'browser', 'workspace', 'close'). Combos can be pressed with `key`.",
     "inputSchema": _obj({"filter": _S})},
    {"name": "clipboard", "description": "Read the clipboard, or set it when `text` is given.",
     "inputSchema": _obj({"text": _S})},
    {"name": "wait", "description": "Sleep for `seconds` (max 30), then return a screenshot.",
     "inputSchema": _obj({"seconds": _N, "screenshot": _SHOT})},
    {"name": "hyprctl",
     "description": "Raw hyprctl passthrough for anything else: 'clients -j', 'monitors -j', "
                    "'dispatch hl.dsp.window.float({ action = \"toggle\" })' (Hyprland lua dispatch syntax on Omarchy).",
     "inputSchema": _obj({"args": _S}, ["args"])},
]


# ----------------------------------------------------------------------- helpers

def _img(shot: core.Shot) -> dict:
    return {"type": "image", "data": shot.b64(), "mimeType": "image/png"}


def _txt(s: str) -> dict:
    return {"type": "text", "text": s}


def _after(args: dict, msg: str, settle: float = SETTLE) -> list[dict]:
    """Result of an action that already happened: a failing follow-up screenshot must not turn it into
    an error (the model would retry and e.g. type the text twice)."""
    content = [_txt(msg)]
    if args.get("screenshot", AUTO_SHOT):
        try:
            time.sleep(settle)
            shot = core.screenshot()
            content += [_img(shot), _txt(core.desktop_summary())]
        except Exception as e:  # noqa: BLE001
            content.append(_txt(f"(action done; follow-up screenshot failed: {e})"))
    return content


MAX_TEXT = 60_000


def _cap(s: str) -> str:
    if len(s) <= MAX_TEXT:
        return s
    return s[:MAX_TEXT] + f"\n... {len(s) - MAX_TEXT} more chars omitted; narrow the query"


def _need_xy(a: dict) -> tuple:
    x, y = a.get("x"), a.get("y")
    if (x is None) != (y is None):
        raise CUError("give both x and y, or neither")
    return x, y


# -------------------------------------------------------------------- handlers

def call(name: str, a: dict) -> list[dict]:
    if name == "screenshot":
        if a.get("window"):
            w = core.find_window(a["window"])
            if not w["visible"]:
                core.focus_window(w["address"])
                time.sleep(0.3)
                w = core.find_window(w["address"])
            shot = core.screenshot(region=tuple(w["box"]))
            note = f"window crop; image pixel (u,v) = screen ({shot.origin[0]}+u, {shot.origin[1]}+v)"
        else:
            shot = core.screenshot(monitor=a.get("monitor"))
            note = ("image pixel (u,v) = screen (u,v)" if shot.origin == (0, 0)
                    else f"image pixel (u,v) = screen ({shot.origin[0]}+u, {shot.origin[1]}+v)")
        return [_img(shot), _txt(f"{shot.width}x{shot.height}; {note}\n" + core.desktop_summary())]

    if name == "zoom":
        shot = core.zoom(a["x"], a["y"], a["width"], a["height"])
        k = shot.px_per_screen
        return [_img(shot), _txt(f"zoom x{k:.2f}: image pixel (u,v) = screen ({a['x']}+u/{k:.2f}, {a['y']}+v/{k:.2f})")]

    if name == "find_text":
        reg = tuple(a["region"]) if a.get("region") else None
        q = a.get("query", "")
        hits = core.ocr_lines(reg, a.get("monitor")) if not q.strip() else core.find_text(q, reg, a.get("monitor"))
        if not hits:
            return [_txt(f"no text matching {q!r} found on screen")]
        lines = [f"({h.x}, {h.y})  box={h.box}  {h.text!r}" for h in hits[:200]]
        if len(hits) > 200:
            lines.append(f"... {len(hits) - 200} more lines omitted")
        return [_txt("\n".join(lines))]

    if name == "click":
        x, y = _need_xy(a)
        core.click(x, y, a.get("button", "left"), a.get("count", 1), a.get("modifiers"))
        return _after(a, f"clicked {a.get('button', 'left')} x{a.get('count', 1)} at {(x, y) if x is not None else 'cursor'}")

    if name == "click_text":
        hits = core.find_text(a["text"], exact=a.get("exact", False))
        if not hits:
            raise CUError(f"text {a['text']!r} not found on screen (try find_text with an empty query to see what OCR reads)")
        i = int(a.get("index", 0))
        if i >= len(hits):
            raise CUError(f"only {len(hits)} matches")
        h = hits[i]
        core.click(h.x, h.y, a.get("button", "left"), a.get("count", 1))
        others = "" if len(hits) == 1 else f" ({len(hits)} matches: " + ", ".join(f"{x.text!r}@({x.x},{x.y})" for x in hits[:6]) + ")"
        return _after(a, f"clicked {h.text!r} at ({h.x}, {h.y}){others}")

    if name == "move_mouse":
        core.move(a["x"], a["y"])
        return _after(a, f"cursor at ({a['x']}, {a['y']})")

    if name == "drag":
        core.drag(a["x1"], a["y1"], a["x2"], a["y2"], a.get("button", "left"))
        return _after(a, f"dragged ({a['x1']}, {a['y1']}) -> ({a['x2']}, {a['y2']})")

    if name == "scroll":
        x, y = _need_xy(a)
        dy, dx = int(a.get("dy", 0)), int(a.get("dx", 0))
        if not dy and not dx:
            dy = 3
        core.scroll(x, y, dy, dx)
        return _after(a, f"scrolled dy={dy} dx={dx}")

    if name == "mouse_button":
        (core.mouse_down if a["action"] == "down" else core.mouse_up)(a.get("button", "left"))
        return [_txt(f"{a.get('button', 'left')} {a['action']}")]

    if name == "type_text":
        core.type_text(a["text"], via_clipboard=a.get("via_clipboard", False))
        return _after(a, f"typed {len(a['text'])} chars")

    if name == "key":
        core.key(a["combo"], a.get("repeat", 1))
        return _after(a, f"pressed {a['combo']}" + (f" x{a['repeat']}" if a.get("repeat", 1) > 1 else ""))

    if name == "windows":
        return [_txt(json.dumps(core.windows(a.get("all_workspaces", True)), ensure_ascii=False, indent=1))]

    if name == "focus_window":
        w = core.focus_window(a["window"])
        return _after(a, f"focused {w['address']} [{w['class']}] {w['title']!r}")

    if name == "close_window":
        core.close_window(a.get("window"))
        return [_txt("closed")]

    if name == "workspace":
        core.workspace(a["target"])
        return _after(a, f"workspace {a['target']}")

    if name == "launch":
        if a.get("app"):
            w = core.launch_app(a["app"], float(a.get("wait_seconds", 8)))
        elif a.get("command"):
            w = core.launch(a["command"], float(a.get("wait_seconds", 8)))
        else:
            raise CUError("give app or command")
        msg = (f"launched; new window {w['address']} [{w['class']}] {w['title']!r} box={w['box']}" if w
               else "launched (no new window detected" + (")" if a.get("wait_seconds", 8) else ", not waiting)"))
        return _after(a, msg, settle=0.6)

    if name == "ui_tree":
        res = a11y.tree(a.get("window"), a.get("query", ""), not a.get("all_roles", False), int(a.get("limit", 250)))
        lines = [f"window {res['window']['address']} [{res['window']['class']}] {res['window']['title']!r}"]
        for e in res["elements"]:
            extra = []
            if e.get("states"): extra.append(",".join(e["states"]))
            if e.get("value"): extra.append(f"value={e['value']!r}")
            if e.get("actions"): extra.append("actions=" + "/".join(e["actions"]))
            c = f"@({e['center'][0]},{e['center'][1]})" if e.get("center") else ""
            lines.append(f"[{e['id']}] {e['role']} {e['name']!r} {c} {' '.join(extra)}".rstrip())
        if res.get("note"):
            lines.append(f"note: {res['note']}")
        return [_txt("\n".join(lines))]

    if name == "ui_action":
        msg = a11y.act(a["id"], a.get("action", "click"), a.get("text"), a.get("window"))
        return _after(a, msg)

    if name == "window":
        msg = core.window_action(a["action"], a.get("window"), a.get("workspace"), a.get("x"), a.get("y"),
                                 a.get("width"), a.get("height"), a.get("direction"))
        return _after(a, msg)

    if name == "apps":
        items = core.apps(a.get("query", ""))
        return [_txt("\n".join(f"{i['name']}  ({i['id']})  {i['comment']}" for i in items[:150]) or "no apps match")]

    if name == "omarchy":
        return [_txt(_cap(core.omarchy(a["args"])))]

    if name == "omarchy_commands":
        items = core.omarchy_commands(a.get("query", ""))
        return [_txt("\n".join(items[:200]) or "no commands match")]

    if name == "notify":
        core.notify(a["title"], a.get("body", ""), a.get("urgency", "normal"))
        return [_txt("notified")]

    if name == "ask_user":
        ans = core.ask_user(a["question"], a.get("options"), float(a.get("timeout_seconds", 300)))
        return [_txt(json.dumps({"answer": ans}, ensure_ascii=False))]

    if name == "wait_for":
        r = core.wait_for(a.get("text"), a.get("window"), a.get("gone", False), float(a.get("timeout_seconds", 15)))
        return _after(a, json.dumps(r, ensure_ascii=False, default=str), settle=0)

    if name == "keybindings":
        return [_txt("\n".join(core.keybindings(a.get("filter", ""))) or "no matching bindings")]

    if name == "clipboard":
        if "text" in a:
            core.set_clipboard(a["text"])
            return [_txt(f"clipboard set ({len(a['text'])} chars)")]
        return [_txt(_cap(core.get_clipboard()) or "(clipboard is empty)")]

    if name == "wait":
        time.sleep(max(0.0, min(30.0, float(a.get("seconds", 1)))))
        return _after(a, f"waited {a.get('seconds', 1)}s", settle=0)

    if name == "hyprctl":
        return [_txt(_cap(core.hyprctl(a["args"]) or "(empty)"))]

    raise CUError(f"unknown tool {name!r}")


# ----------------------------------------------------------------------- jsonrpc

_out_lock = threading.Lock()


def _send(msg: dict) -> None:
    # ascii-only JSON: lone surrogates or odd bytes in tool output can never break the stream
    data = json.dumps(msg, ensure_ascii=True)
    with _out_lock:
        sys.stdout.write(data + "\n")
        sys.stdout.flush()


def _err(rid, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": rid, "error": {"code": code, "message": message}}


def _call_tool(rid, params: dict) -> dict:
    name = params.get("name", "")
    args = params.get("arguments") or {}
    if not isinstance(args, dict):
        return _err(rid, -32602, "arguments must be an object")
    try:
        content = call(name, args)
        return {"jsonrpc": "2.0", "id": rid, "result": {"content": content, "isError": False}}
    except CUError as e:
        return {"jsonrpc": "2.0", "id": rid, "result": {"content": [_txt(f"error: {e}")], "isError": True}}
    except Exception as e:  # noqa: BLE001
        tb = traceback.format_exc(limit=3)
        return {"jsonrpc": "2.0", "id": rid, "result": {"content": [_txt(f"error: {e!r}\n{tb}")], "isError": True}}


def _handle(req: dict) -> dict | None:
    method, rid, params = req.get("method"), req.get("id"), req.get("params") or {}
    if not isinstance(params, dict):
        return None if rid is None else _err(rid, -32602, "params must be an object")
    if rid is None:  # notification
        return None
    try:
        if method == "initialize":
            v = params.get("protocolVersion")
            return {"jsonrpc": "2.0", "id": rid, "result": {
                "protocolVersion": v if v in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[0],
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": "omarchy-computer-use", "version": "0.2.0"},
                "instructions": ("Desktop control for Omarchy/Hyprland. Start with `screenshot`. "
                                 "All coordinates are in the full-screenshot pixel space. Prefer `find_text`/"
                                 "`click_text` for text targets, `launch`/`focus_window`/`workspace` instead of "
                                 "hunting for icons, and `key` shortcuts over mouse when they exist."),
            }}
        if method == "ping":
            return {"jsonrpc": "2.0", "id": rid, "result": {}}
        if method == "tools/list":
            return {"jsonrpc": "2.0", "id": rid, "result": {"tools": TOOLS}}
        if method == "tools/call":
            return _call_tool(rid, params)
        if method in ("resources/list", "prompts/list"):
            key = method.split("/")[0]
            return {"jsonrpc": "2.0", "id": rid, "result": {key: []}}
        return _err(rid, -32601, f"method not found: {method}")
    except Exception as e:  # noqa: BLE001
        return _err(rid, -32603, str(e))


class _Worker:
    """Runs tools/call requests one at a time (desktop input must not interleave) on a background
    thread, so the reader keeps answering ping and handles notifications/cancelled meanwhile."""

    def __init__(self):
        import queue
        self.q: "queue.Queue" = queue.Queue()
        self.current = None
        self.cancelled: set = set()
        self.lock = threading.Lock()
        self.t = threading.Thread(target=self._loop, daemon=True)
        self.t.start()

    def submit(self, req: dict) -> None:
        self.q.put(req)

    def cancel(self, rid) -> None:
        with self.lock:
            self.cancelled.add(rid)
            if rid == self.current:
                core.CANCEL.set()  # ask_user / wait_for return early

    def _loop(self):
        while True:
            try:
                if self._one() is False:
                    return
            except Exception as e:  # noqa: BLE001  the worker must survive anything
                print(f"omarchy-cu mcp worker: {e!r}", file=sys.stderr)

    def _one(self):
        req = self.q.get()
        if req is None:
            return False
        rid = req.get("id")
        with self.lock:
            if rid in self.cancelled:
                self.cancelled.discard(rid)
                return None
            self.current = rid
            core.CANCEL.clear()
        try:
            resp = _handle(req)
        except Exception as e:  # noqa: BLE001
            resp = _err(rid, -32603, str(e))
        with self.lock:
            self.current = None
            dropped = rid in self.cancelled
            self.cancelled.discard(rid)
        if resp is not None and not dropped:  # no response to a cancelled request (MCP spec)
            try:
                _send(resp)
            except Exception as e:  # noqa: BLE001  client gone: stop long waits, drop the rest
                print(f"omarchy-cu mcp: send failed: {e!r}", file=sys.stderr)
                core.CANCEL.set()

    def close(self):
        """stdin closed: finish what was already requested (the old loop did too), then exit."""
        self.q.put(None)
        self.t.join()


def _dispatch(r, worker: _Worker) -> None:
    if not isinstance(r, dict):
        _send(_err(None, -32600, "invalid request"))
        return
    if r.get("method") == "notifications/cancelled":
        p = r.get("params") if isinstance(r.get("params"), dict) else {}
        if p.get("requestId") is not None:
            worker.cancel(p["requestId"])
        return
    rid = r.get("id")
    if rid is not None and (isinstance(rid, bool) or not isinstance(rid, (int, str))):
        _send(_err(None, -32600, "invalid request: id must be a string or number"))
        return
    if r.get("method") == "tools/call" and rid is not None:
        worker.submit(r)
        return
    resp = _handle(r)
    if resp is not None:
        _send(resp)


def main() -> None:
    worker = _Worker()
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except json.JSONDecodeError:
            _send(_err(None, -32700, "parse error"))
            continue
        for r in (req if isinstance(req, list) else [req]):
            try:
                _dispatch(r, worker)
            except Exception as e:  # noqa: BLE001  never let one bad message kill the server
                print(f"omarchy-cu mcp: {e!r}", file=sys.stderr)
    worker.close()


if __name__ == "__main__":
    main()
