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

from . import core
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
                    "(screen space). Case-insensitive substring match, fuzzy fallback. Takes ~1-4 s. "
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
                    "`index` picks among several matches (top-to-bottom, left-to-right).",
     "inputSchema": _obj({"text": _S, "index": _I, "exact": _B,
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
     "description": "Launch an app via Hyprland exec (e.g. 'chromium https://example.com', 'nautilus', 'foot', "
                    "'omarchy-launch-browser'). Waits for its window and focuses it.",
     "inputSchema": _obj({"command": _S, "wait_seconds": {**_N, "description": "default 8; 0 = don't wait"},
                          "screenshot": _SHOT}, ["command"])},
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
    content = [_txt(msg)]
    if args.get("screenshot", AUTO_SHOT):
        time.sleep(settle)
        content.append(_img(core.screenshot()))
        content.append(_txt(core.desktop_summary()))
    return content


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
        w = core.launch(a["command"], float(a.get("wait_seconds", 8)))
        msg = (f"launched; new window {w['address']} [{w['class']}] {w['title']!r} box={w['box']}" if w
               else "launched (no new window detected" + (")" if a.get("wait_seconds", 8) else ", not waiting)"))
        return _after(a, msg, settle=0.6)

    if name == "keybindings":
        return [_txt("\n".join(core.keybindings(a.get("filter", ""))) or "no matching bindings")]

    if name == "clipboard":
        if "text" in a:
            core.set_clipboard(a["text"])
            return [_txt(f"clipboard set ({len(a['text'])} chars)")]
        return [_txt(core.get_clipboard())]

    if name == "wait":
        time.sleep(max(0.0, min(30.0, float(a.get("seconds", 1)))))
        return _after(a, f"waited {a.get('seconds', 1)}s", settle=0)

    if name == "hyprctl":
        return [_txt(core.hyprctl(a["args"]) or "(empty)")]

    raise CUError(f"unknown tool {name}")


# ----------------------------------------------------------------------- jsonrpc

_out_lock = threading.Lock()


def _send(msg: dict) -> None:
    data = json.dumps(msg, ensure_ascii=False)
    with _out_lock:
        sys.stdout.write(data + "\n")
        sys.stdout.flush()


def _handle(req: dict) -> dict | None:
    method, rid, params = req.get("method"), req.get("id"), req.get("params") or {}
    if rid is None:  # notification
        return None
    try:
        if method == "initialize":
            v = params.get("protocolVersion")
            return {"jsonrpc": "2.0", "id": rid, "result": {
                "protocolVersion": v if v in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[0],
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": "omarchy-computer-use", "version": "0.1.0"},
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
            name = params.get("name", "")
            args = params.get("arguments") or {}
            try:
                content = call(name, args)
                return {"jsonrpc": "2.0", "id": rid, "result": {"content": content, "isError": False}}
            except CUError as e:
                return {"jsonrpc": "2.0", "id": rid, "result": {"content": [_txt(f"error: {e}")], "isError": True}}
            except Exception as e:  # noqa: BLE001
                tb = traceback.format_exc(limit=3)
                return {"jsonrpc": "2.0", "id": rid, "result": {"content": [_txt(f"error: {e!r}\n{tb}")], "isError": True}}
        if method in ("resources/list", "prompts/list"):
            key = method.split("/")[0]
            return {"jsonrpc": "2.0", "id": rid, "result": {key: []}}
        return {"jsonrpc": "2.0", "id": rid, "error": {"code": -32601, "message": f"method not found: {method}"}}
    except Exception as e:  # noqa: BLE001
        return {"jsonrpc": "2.0", "id": rid, "error": {"code": -32603, "message": str(e)}}


def main() -> None:
    # tools are executed serially: desktop input must not interleave
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except json.JSONDecodeError:
            _send({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "parse error"}})
            continue
        reqs = req if isinstance(req, list) else [req]
        for r in reqs:
            resp = _handle(r)
            if resp is not None:
                _send(resp)


if __name__ == "__main__":
    main()
