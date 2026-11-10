"""omarchy-cu command line — same capabilities as the MCP server, for any agent with a shell."""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time

from . import core
from .core import CUError

HELP = """\
coordinates: screen space = pixels of the image written by `omarchy-cu screenshot`.

examples:
  omarchy-cu screenshot                 # prints PNG path + monitors/windows summary
  omarchy-cu zoom 900 40 300 120        # magnified crop for small text
  omarchy-cu find "Sign in"             # OCR: center coords + boxes
  omarchy-cu click 640 360 [--right] [--double] [--mods ctrl]
  omarchy-cu click-text "Save"
  omarchy-cu type "hello world"; omarchy-cu key ctrl+s
  omarchy-cu scroll 640 360 --dy 5
  omarchy-cu launch "chromium https://example.com"
  omarchy-cu windows | focus firefox | workspace 3
  omarchy-cu pause | resume | toggle   # kill switch for agent input (bind it to a key)
"""


def _notify(msg: str) -> None:
    try:
        subprocess.Popen(["notify-send", "-a", "omarchy-cu", "-t", "2500", "Computer use", msg],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except FileNotFoundError:
        pass


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="omarchy-cu", description="computer use for Omarchy / Hyprland",
                                epilog=HELP, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("mcp", help="run the MCP stdio server")
    sub.add_parser("doctor", help="check dependencies and permissions")
    sub.add_parser("summary", help="monitors, visible windows, cursor (text)")

    s = sub.add_parser("screenshot", help="save a screenshot, print its path")
    s.add_argument("-o", "--out"); s.add_argument("--monitor"); s.add_argument("--window")
    s.add_argument("--no-cursor", action="store_true")

    s = sub.add_parser("zoom", help="magnified crop of x y w h")
    for k in ("x", "y", "w", "h"):
        s.add_argument(k, type=float)
    s.add_argument("-o", "--out")

    s = sub.add_parser("find", help="OCR search for text (empty string = all lines)")
    s.add_argument("query", nargs="?", default=""); s.add_argument("--exact", action="store_true")
    s.add_argument("--region", nargs=4, type=float, metavar=("X", "Y", "W", "H")); s.add_argument("--json", action="store_true")

    def btn(sp):
        sp.add_argument("--button", default="left", choices=sorted(core.BUTTONS))
        sp.add_argument("--right", action="store_const", const="right", dest="button")
        sp.add_argument("--middle", action="store_const", const="middle", dest="button")
        sp.add_argument("--count", type=int, default=1)
        sp.add_argument("--double", action="store_const", const=2, dest="count")
        sp.add_argument("--triple", action="store_const", const=3, dest="count")

    s = sub.add_parser("click", help="click at x y (or current position)")
    s.add_argument("x", type=float, nargs="?"); s.add_argument("y", type=float, nargs="?")
    btn(s); s.add_argument("--mods", default="", help="comma list: ctrl,shift,alt,super")

    s = sub.add_parser("click-text", help="OCR then click the text")
    s.add_argument("text"); s.add_argument("--index", type=int, default=0); s.add_argument("--exact", action="store_true"); btn(s)

    s = sub.add_parser("move", help="move cursor"); s.add_argument("x", type=float); s.add_argument("y", type=float)
    s = sub.add_parser("drag", help="drag x1 y1 x2 y2")
    for k in ("x1", "y1", "x2", "y2"):
        s.add_argument(k, type=float)
    s.add_argument("--button", default="left")
    s = sub.add_parser("scroll", help="scroll at x y")
    s.add_argument("x", type=float, nargs="?"); s.add_argument("y", type=float, nargs="?")
    s.add_argument("--dy", type=int, default=0); s.add_argument("--dx", type=int, default=0)
    s = sub.add_parser("down", help="mouse button down"); s.add_argument("button", nargs="?", default="left")
    s = sub.add_parser("up", help="mouse button up"); s.add_argument("button", nargs="?", default="left")
    sub.add_parser("cursor", help="print cursor position")

    s = sub.add_parser("type", help="type text ('-' reads stdin)")
    s.add_argument("text"); s.add_argument("--clipboard", action="store_true")
    s = sub.add_parser("key", help="key combo(s), e.g. ctrl+shift+t or 'ctrl+a BackSpace'")
    s.add_argument("combo", nargs="+"); s.add_argument("--repeat", type=int, default=1)

    s = sub.add_parser("windows", help="list windows as JSON"); s.add_argument("--visible", action="store_true")
    s = sub.add_parser("focus", help="focus window by address/class/title"); s.add_argument("window")
    s = sub.add_parser("close", help="close window (default: focused)"); s.add_argument("window", nargs="?")
    s = sub.add_parser("workspace", help="switch workspace"); s.add_argument("target")
    s = sub.add_parser("launch", help="launch app and wait for its window")
    s.add_argument("command"); s.add_argument("--wait", type=float, default=8)
    s = sub.add_parser("binds", help="list Hyprland keybindings"); s.add_argument("filter", nargs="?", default="")
    s = sub.add_parser("clipboard", help="print clipboard, or set it"); s.add_argument("text", nargs="?")
    s = sub.add_parser("hyprctl", help="raw hyprctl passthrough"); s.add_argument("args", nargs=argparse.REMAINDER)
    s = sub.add_parser("wait", help="sleep seconds"); s.add_argument("seconds", type=float)

    sub.add_parser("pause", help="block all agent input (kill switch)")
    sub.add_parser("resume", help="allow agent input again")
    sub.add_parser("toggle", help="toggle pause (bind to a key)")
    sub.add_parser("build-helper", help="(re)build the pointer helper")

    a = p.parse_args(argv)
    try:
        return _run(a)
    except CUError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1


def _run(a) -> int:
    c = a.cmd
    if c == "mcp":
        from .server import main as serve
        serve(); return 0
    if c == "doctor":
        ok = True
        for name, good, info in core.doctor():
            ok &= good or "optional" in info
            print(f"{'ok ' if good else 'NO '} {name:16} {info}")
        return 0 if ok else 1
    if c == "summary":
        print(core.desktop_summary()); return 0
    if c == "screenshot":
        if a.window:
            w = core.find_window(a.window)
            shot = core.screenshot(region=tuple(w["box"]), cursor=not a.no_cursor)
        else:
            shot = core.screenshot(monitor=a.monitor, cursor=not a.no_cursor)
        path = shot.save(a.out)
        print(f"{path}\n{shot.width}x{shot.height} origin={shot.origin} (image px + origin = screen coords)")
        print(core.desktop_summary()); return 0
    if c == "zoom":
        shot = core.zoom(a.x, a.y, a.w, a.h)
        print(f"{shot.save(a.out)}\nzoom x{shot.px_per_screen:.2f}: screen = ({a.x}+u/{shot.px_per_screen:.2f}, {a.y}+v/{shot.px_per_screen:.2f})")
        return 0
    if c == "find":
        reg = tuple(a.region) if a.region else None
        hits = core.find_text(a.query, reg, exact=a.exact) if a.query.strip() else core.ocr_lines(reg)
        if a.json:
            print(json.dumps([h.__dict__ for h in hits], ensure_ascii=False))
        else:
            for h in hits:
                print(f"{h.x}\t{h.y}\t{h.text}")
        return 0 if hits else 1
    if c == "click":
        if (a.x is None) != (a.y is None):
            raise CUError("give both x and y")
        mods = [m for m in a.mods.split(",") if m]
        core.click(a.x, a.y, a.button, a.count, mods); return 0
    if c == "click-text":
        hits = core.find_text(a.text, exact=a.exact)
        if len(hits) <= a.index:
            raise CUError(f"text {a.text!r} not found (matches: {len(hits)})")
        h = hits[a.index]; core.click(h.x, h.y, a.button, a.count)
        print(f"clicked {h.text!r} at {h.x} {h.y}"); return 0
    if c == "move":
        core.move(a.x, a.y); return 0
    if c == "drag":
        core.drag(a.x1, a.y1, a.x2, a.y2, a.button); return 0
    if c == "scroll":
        core.scroll(a.x, a.y, a.dy or (0 if a.dx else 3), a.dx); return 0
    if c in ("down", "up"):
        (core.mouse_down if c == "down" else core.mouse_up)(a.button); return 0
    if c == "cursor":
        print(*core.cursor_position()); return 0
    if c == "type":
        text = sys.stdin.read() if a.text == "-" else a.text
        core.type_text(text, via_clipboard=a.clipboard); return 0
    if c == "key":
        core.key(" ".join(a.combo), a.repeat); return 0
    if c == "windows":
        print(json.dumps(core.windows(not a.visible), ensure_ascii=False, indent=1)); return 0
    if c == "focus":
        w = core.focus_window(a.window); print(w["address"], w["class"], w["title"]); return 0
    if c == "close":
        core.close_window(a.window); return 0
    if c == "workspace":
        core.workspace(a.target); return 0
    if c == "launch":
        w = core.launch(a.command, a.wait)
        print(json.dumps(w, ensure_ascii=False) if w else "launched (no new window seen)"); return 0
    if c == "binds":
        print("\n".join(core.keybindings(a.filter))); return 0
    if c == "clipboard":
        if a.text is None:
            sys.stdout.write(core.get_clipboard())
        else:
            core.set_clipboard(a.text)
        return 0
    if c == "hyprctl":
        print(core.hyprctl(" ".join(a.args)), end=""); return 0
    if c == "wait":
        time.sleep(a.seconds); return 0
    if c == "pause" or (c == "toggle" and not core.PAUSE_FILE.exists()):
        core.CACHE.mkdir(parents=True, exist_ok=True); core.PAUSE_FILE.touch()
        _notify("agent input PAUSED"); print("paused"); return 0
    if c in ("resume", "toggle"):
        core.PAUSE_FILE.unlink(missing_ok=True); _notify("agent input allowed"); print("resumed"); return 0
    if c == "build-helper":
        exe = core.CACHE / "bin" / "hypr-pointer"
        exe.unlink(missing_ok=True); print(core.helper_path()); return 0
    raise CUError(f"unknown command {c}")


if __name__ == "__main__":
    sys.exit(main())
