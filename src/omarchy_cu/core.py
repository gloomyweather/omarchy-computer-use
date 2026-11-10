"""omarchy-cu core: screen capture + input + window control for Hyprland (Omarchy).

Coordinate model
----------------
Every x/y that tools accept or return is in *screen space*: Hyprland logical layout
pixels multiplied by a fixed factor ``S`` (so screenshots stay small enough for a model
to read). With one 1920x1080 monitor and the default max edge of 1280, S = 2/3 and the
screen space is 1280x720. ``S`` is derived from the largest monitor and never depends on
which screenshot was taken last, so coordinates read from any full screenshot stay valid.

Only stdlib + CLI tools that ship with Omarchy: grim, wtype, hyprctl (via socket),
wl-clipboard, tesseract. Pointer input uses a tiny C helper (hypr-pointer) that speaks
zwlr_virtual_pointer_v1, so no root / uinput / ydotoold is needed.
"""
from __future__ import annotations

import base64
import csv
import io
import json
import os
import re
import shutil
import socket
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CACHE = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "omarchy-cu"
HELPER_SRC = ROOT / "helper"
PAUSE_FILE = CACHE / "paused"
MAX_EDGE = int(os.environ.get("OMARCHY_CU_MAX_EDGE", "1280"))


class CUError(RuntimeError):
    pass


# ----------------------------------------------------------------------------- utils

def run(cmd: list[str], *, input: bytes | None = None, timeout: float = 20, check: bool = True) -> bytes:
    try:
        p = subprocess.run(cmd, input=input, capture_output=True, timeout=timeout)
    except FileNotFoundError:
        raise CUError(f"missing program: {cmd[0]} (install it with pacman)")
    except subprocess.TimeoutExpired:
        raise CUError(f"timeout: {' '.join(cmd[:3])}")
    if check and p.returncode != 0:
        raise CUError(f"{cmd[0]} failed ({p.returncode}): {p.stderr.decode(errors='replace').strip()}")
    return p.stdout


def check_not_paused() -> None:
    if PAUSE_FILE.exists():
        raise CUError("computer use is paused by the user (run `omarchy-cu resume` to allow input again)")


# ------------------------------------------------------------------------- hyprland

class Hypr:
    """Minimal Hyprland IPC client (same socket hyprctl uses)."""

    _lua: bool | None = None

    @staticmethod
    def _sock() -> str:
        sig = os.environ.get("HYPRLAND_INSTANCE_SIGNATURE")
        rt = os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
        if not sig:
            # agents launched outside the session env: pick the newest instance
            base = Path(rt) / "hypr"
            cands = sorted(base.glob("*/.socket.sock"), key=lambda p: p.stat().st_mtime, reverse=True) if base.exists() else []
            if not cands:
                raise CUError("not inside a Hyprland session (HYPRLAND_INSTANCE_SIGNATURE unset)")
            return str(cands[0])
        return f"{rt}/hypr/{sig}/.socket.sock"

    @classmethod
    def request(cls, msg: str) -> str:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(5)
            s.connect(cls._sock())
            s.sendall(msg.encode())
            chunks = []
            while True:
                b = s.recv(65536)
                if not b:
                    break
                chunks.append(b)
        return b"".join(chunks).decode(errors="replace")

    @classmethod
    def j(cls, what: str):
        return json.loads(cls.request(f"j/{what}"))

    @classmethod
    def lua(cls) -> bool:
        """Hyprland >= 0.55 with lua config wants `hl.dsp.*` expressions for dispatch."""
        if cls._lua is None:
            r = cls.request("/dispatch hl.dsp.cursor.move({ x = %d, y = %d })" % tuple(cls.cursor()))
            cls._lua = r.strip() == "ok"
        return cls._lua

    @classmethod
    def dispatch(cls, lua_expr: str | None, legacy: str | None) -> str:
        expr = lua_expr if cls.lua() else legacy
        if expr is None:
            raise CUError("this action is not supported by the running Hyprland config mode")
        r = cls.request(f"/dispatch {expr}").strip()
        if r != "ok":
            raise CUError(f"hyprland: {r}")
        return r

    @classmethod
    def cursor(cls) -> tuple[int, int]:
        c = cls.j("cursorpos")
        return int(c["x"]), int(c["y"])


def lua_str(s: str) -> str:
    return json.dumps(s, ensure_ascii=False)  # JSON string literal is a valid lua string literal


# ---------------------------------------------------------------------- geometry

@dataclass
class Monitor:
    name: str
    x: int
    y: int
    w: int  # logical
    h: int
    scale: float
    focused: bool
    workspace: int


def monitors() -> list[Monitor]:
    out = []
    for m in Hypr.j("monitors"):
        sc = float(m.get("scale", 1) or 1)
        w, h = m["width"], m["height"]
        if m.get("transform", 0) % 2 == 1:
            w, h = h, w
        out.append(Monitor(m["name"], m["x"], m["y"], round(w / sc), round(h / sc), sc,
                           bool(m.get("focused")), m.get("activeWorkspace", {}).get("id", 0)))
    if not out:
        raise CUError("no monitors reported by Hyprland")
    return out


def scale_factor(mons: list[Monitor] | None = None) -> float:
    mons = mons or monitors()
    longest = max(max(m.w, m.h) for m in mons)
    return min(1.0, MAX_EDGE / longest)


def to_logical(x: float, y: float, S: float | None = None) -> tuple[int, int]:
    S = S or scale_factor()
    return round(x / S), round(y / S)


def to_screen(x: float, y: float, S: float | None = None) -> tuple[int, int]:
    S = S or scale_factor()
    return round(x * S), round(y * S)


def pick_monitor(which: str | None, mons: list[Monitor]) -> Monitor | None:
    """None/'focused' -> focused monitor, 'all' -> None (whole layout), else by name/index."""
    if which in (None, "", "focused", "current"):
        return next((m for m in mons if m.focused), mons[0])
    if which == "all":
        return None
    for i, m in enumerate(mons):
        if which in (m.name, str(i)):
            return m
    raise CUError(f"unknown monitor {which!r}; have: {', '.join(m.name for m in mons)}")


# --------------------------------------------------------------------- screenshot

@dataclass
class Shot:
    png: bytes
    width: int
    height: int
    origin: tuple[int, int]  # screen-space coords of the image's top-left pixel
    S: float                 # screen-space px per logical px
    px_per_screen: float     # image px per screen-space px (1.0 for normal shots, >1 for zoom)

    def b64(self) -> str:
        return base64.b64encode(self.png).decode()

    def save(self, path: str | Path | None = None) -> Path:
        if path is None:
            d = Path(tempfile.gettempdir()) / "omarchy-cu"
            d.mkdir(exist_ok=True)
            path = d / f"shot-{time.strftime('%H%M%S')}-{int(time.time() * 1000) % 1000:03d}.png"
        Path(path).write_bytes(self.png)
        return Path(path)


def png_size(b: bytes) -> tuple[int, int]:
    if b[:8] != b"\x89PNG\r\n\x1a\n":
        raise CUError("grim did not return a PNG")
    return int.from_bytes(b[16:20], "big"), int.from_bytes(b[20:24], "big")


def _grim(geom: str | None, output: str | None, scale: float, cursor: bool) -> bytes:
    cmd = ["grim", "-s", f"{scale:.6f}", "-l", "3"]
    if cursor:
        cmd.append("-c")
    if geom:
        cmd += ["-g", geom]
    elif output:
        cmd += ["-o", output]
    cmd.append("-")
    return run(cmd, timeout=15)


def screenshot(monitor: str | None = None, region: tuple[float, float, float, float] | None = None,
               cursor: bool = True) -> Shot:
    """Screenshot in screen space. region = (x, y, w, h) in screen space."""
    mons = monitors()
    S = scale_factor(mons)
    if region:
        x, y, w, h = region
        lx, ly = to_logical(x, y, S)
        lw, lh = max(1, round(w / S)), max(1, round(h / S))
        png = _grim(f"{lx},{ly} {lw}x{lh}", None, S, cursor)
        origin = (round(x), round(y))
    else:
        m = pick_monitor(monitor, mons)
        if m is None:
            minx, miny = min(m.x for m in mons), min(m.y for m in mons)
            png = _grim(None, None, S, cursor)
            origin = to_screen(minx, miny, S)
        else:
            png = _grim(None, m.name, S, cursor)
            origin = to_screen(m.x, m.y, S)
    w, h = png_size(png)
    return Shot(png, w, h, origin, S, 1.0)


def zoom(x: float, y: float, w: float, h: float, cursor: bool = False) -> Shot:
    """High-detail crop of a screen-space region, rendered at native monitor resolution
    (capped so the long edge stays <= 1600px)."""
    mons = monitors()
    S = scale_factor(mons)
    lx, ly = to_logical(x, y, S)
    lw, lh = max(1, round(w / S)), max(1, round(h / S))
    native = max(m.scale for m in mons)
    k = min(native, 1600 / max(lw, lh))
    k = max(k, S)
    png = _grim(f"{lx},{ly} {lw}x{lh}", None, k, cursor)
    iw, ih = png_size(png)
    return Shot(png, iw, ih, (round(x), round(y)), S, iw / max(1, w))


# --------------------------------------------------------------------------- OCR

@dataclass
class TextHit:
    text: str
    x: int  # center, screen space
    y: int
    box: tuple[int, int, int, int]  # x, y, w, h screen space
    conf: float


def _ocr_langs() -> str:
    want = os.environ.get("OMARCHY_CU_OCR_LANGS")
    if want:
        return want
    try:
        have = set(subprocess.run(["tesseract", "--list-langs"], capture_output=True, text=True, timeout=10).stdout.split())
    except Exception:
        have = set()
    langs = [l for l in ("eng", "rus", "ukr", "deu", "fra", "spa") if l in have]
    return "+".join(langs or ["eng"])


def ocr_lines(region: tuple[float, float, float, float] | None = None, monitor: str | None = None) -> list[TextHit]:
    """OCR the screen (native resolution for accuracy); returns one hit per text line."""
    mons = monitors()
    S = scale_factor(mons)
    native = max(m.scale for m in mons)
    if region:
        x, y, w, h = region
        lx, ly = to_logical(x, y, S)
        lw, lh = max(1, round(w / S)), max(1, round(h / S))
        png = _grim(f"{lx},{ly} {lw}x{lh}", None, native, False)
        org_l = (lx, ly)
    else:
        m = pick_monitor(monitor, mons)
        if m is None:
            png = _grim(None, None, native, False)
            org_l = (min(mm.x for mm in mons), min(mm.y for mm in mons))
        else:
            png = _grim(None, m.name, native, False)
            org_l = (m.x, m.y)
    iw, _ = png_size(png)
    # image px -> logical: depends on what grim produced
    lw_total = (lw if region else (m.w if m else (max(mm.x + mm.w for mm in mons) - org_l[0])))
    k = lw_total / iw  # logical px per image px
    # upscale small UI text for tesseract
    upscale = 2 if native < 1.5 else 1
    with tempfile.TemporaryDirectory() as td:
        src = Path(td) / "s.png"
        src.write_bytes(png)
        if upscale > 1 and shutil.which("magick"):
            run(["magick", str(src), "-resize", f"{upscale * 100}%", str(src)], timeout=30)
        else:
            upscale = 1
        tsv = run(["tesseract", str(src), "-", "-l", _ocr_langs(), "--psm", "11", "tsv"],
                  timeout=60).decode(errors="replace")
    words: dict[tuple, list] = {}
    for row in csv.DictReader(io.StringIO(tsv), delimiter="\t", quoting=csv.QUOTE_NONE):
        t = (row.get("text") or "").strip()
        try:
            conf = float(row.get("conf", -1))
        except ValueError:
            continue
        if not t or conf < 30:
            continue
        key = (row["block_num"], row["par_num"], row["line_num"])
        words.setdefault(key, []).append((t, int(row["left"]), int(row["top"]), int(row["width"]), int(row["height"]), conf))
    hits = []
    for ws in words.values():
        ws.sort(key=lambda w: w[1])
        # split a tesseract "line" on big horizontal gaps (separate UI elements)
        groups, cur = [], [ws[0]]
        for w in ws[1:]:
            prev = cur[-1]
            gap = w[1] - (prev[1] + prev[3])
            if gap > 2.2 * max(prev[4], w[4]):
                groups.append(cur)
                cur = [w]
            else:
                cur.append(w)
        groups.append(cur)
        for g in groups:
            x0 = min(w[1] for w in g); y0 = min(w[2] for w in g)
            x1 = max(w[1] + w[3] for w in g); y1 = max(w[2] + w[4] for w in g)
            f = k / upscale * S
            bx, by = org_l[0] * S + x0 * f, org_l[1] * S + y0 * f
            bw, bh = (x1 - x0) * f, (y1 - y0) * f
            hits.append(TextHit(" ".join(w[0] for w in g), round(bx + bw / 2), round(by + bh / 2),
                                (round(bx), round(by), round(bw), round(bh)),
                                round(sum(w[5] for w in g) / len(g), 1)))
    hits.sort(key=lambda h: (h.box[1] // 8, h.box[0]))
    return hits


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip().lower()


def find_text(query: str, region=None, monitor=None, exact: bool = False) -> list[TextHit]:
    q = _norm(query)
    out = []
    for h in ocr_lines(region, monitor):
        t = _norm(h.text)
        if (t == q) if exact else (q in t):
            if not exact and t != q:
                # narrow the box to the matched substring (approximate by char ratio)
                i = t.find(q)
                n = max(1, len(t))
                bx, by, bw, bh = h.box
                nx = bx + bw * i / n
                nw = bw * len(q) / n
                h = TextHit(h.text, round(nx + nw / 2), h.y, (round(nx), by, round(nw), bh), h.conf)
            out.append(h)
    if not out:
        # fuzzy fallback
        import difflib
        lines = ocr_lines(region, monitor)
        scored = sorted(((difflib.SequenceMatcher(None, q, _norm(h.text)).ratio(), h) for h in lines),
                        key=lambda p: -p[0])
        out = [h for r, h in scored[:3] if r >= 0.75]
    return out


# ------------------------------------------------------------------------ pointer

def helper_path() -> Path:
    exe = CACHE / "bin" / "hypr-pointer"
    src = [HELPER_SRC / "hypr-pointer.c", HELPER_SRC / "wlr-virtual-pointer-unstable-v1.xml", HELPER_SRC / "build.sh"]
    if not exe.exists() or any(p.stat().st_mtime > exe.stat().st_mtime for p in src if p.exists()):
        if not (HELPER_SRC / "build.sh").exists():
            raise CUError(f"helper sources missing in {HELPER_SRC}")
        for tool in ("cc", "wayland-scanner", "pkg-config"):
            if not shutil.which(tool):
                raise CUError(f"need `{tool}` to build the pointer helper: sudo pacman -S --needed base-devel wayland")
        run(["bash", str(HELPER_SRC / "build.sh"), str(exe.parent)], timeout=120)
    return exe


def pointer(*args: str | int) -> None:
    check_not_paused()
    run([str(helper_path()), *map(str, args)], timeout=60)


BUTTONS = {"left", "right", "middle", "back", "forward"}


def move(x: float, y: float) -> tuple[int, int]:
    lx, ly = to_logical(x, y)
    pointer("move", lx, ly)
    return lx, ly


def click(x: float | None, y: float | None, button: str = "left", count: int = 1,
          modifiers: list[str] | None = None) -> None:
    if button not in BUTTONS:
        raise CUError(f"button must be one of {sorted(BUTTONS)}")
    args: list = []
    if x is not None and y is not None:
        args += ["move", *to_logical(x, y), "sleep", 30]
    args += ["click", button, max(1, int(count))]
    mods = [_mod(m) for m in (modifiers or [])]
    if mods:
        check_not_paused()
        _wtype(*sum((["-M", m] for m in mods), []))  # press mods
        try:
            pointer(*args)
        finally:
            _wtype(*sum((["-m", m] for m in mods), []))
    else:
        pointer(*args)


def drag(x1: float, y1: float, x2: float, y2: float, button: str = "left", steps: int = 12) -> None:
    a, b = to_logical(x1, y1), to_logical(x2, y2)
    seq: list = ["move", *a, "sleep", 40, "down", button, "sleep", 60]
    for i in range(1, steps + 1):
        t = i / steps
        seq += ["move", round(a[0] + (b[0] - a[0]) * t), round(a[1] + (b[1] - a[1]) * t), "sleep", 16]
    seq += ["sleep", 60, "up", button]
    pointer(*seq)


def scroll(x: float | None, y: float | None, dy: int = 0, dx: int = 0) -> None:
    args: list = []
    if x is not None and y is not None:
        args += ["move", *to_logical(x, y), "sleep", 30]
    args += ["scroll", int(dy), int(dx)]
    pointer(*args)


def mouse_down(button: str = "left") -> None:
    pointer("down", button)


def mouse_up(button: str = "left") -> None:
    pointer("up", button)


def cursor_position() -> tuple[int, int]:
    return to_screen(*Hypr.cursor())


# ----------------------------------------------------------------------- keyboard

MODS = {"ctrl": "ctrl", "control": "ctrl", "shift": "shift", "alt": "alt", "option": "alt",
        "super": "logo", "win": "logo", "meta": "logo", "cmd": "logo", "logo": "logo", "mod4": "logo",
        "altgr": "altgr", "caps": "caps"}

KEYS = {
    "enter": "Return", "return": "Return", "esc": "Escape", "escape": "Escape", "tab": "Tab",
    "space": "space", "backspace": "BackSpace", "bksp": "BackSpace", "delete": "Delete", "del": "Delete",
    "insert": "Insert", "ins": "Insert", "home": "Home", "end": "End", "pageup": "Prior", "pgup": "Prior",
    "pagedown": "Next", "pgdn": "Next", "up": "Up", "down": "Down", "left": "Left", "right": "Right",
    "menu": "Menu", "print": "Print", "printscreen": "Print", "capslock": "Caps_Lock",
    "minus": "minus", "plus": "plus", "equal": "equal", "comma": "comma", "period": "period", "dot": "period",
    "slash": "slash", "backslash": "backslash", "semicolon": "semicolon", "apostrophe": "apostrophe",
    "grave": "grave", "bracketleft": "bracketleft", "bracketright": "bracketright",
    "volumeup": "XF86AudioRaiseVolume", "volumedown": "XF86AudioLowerVolume", "mute": "XF86AudioMute",
    "playpause": "XF86AudioPlay", "brightnessup": "XF86MonBrightnessUp", "brightnessdown": "XF86MonBrightnessDown",
}
PUNCT = {"-": "minus", "=": "equal", ",": "comma", ".": "period", "/": "slash", "\\": "backslash",
         ";": "semicolon", "'": "apostrophe", "`": "grave", "[": "bracketleft", "]": "bracketright",
         "+": "plus", " ": "space"}


def _mod(m: str) -> str:
    k = MODS.get(m.strip().lower())
    if not k:
        raise CUError(f"unknown modifier {m!r}; use ctrl/shift/alt/super/altgr")
    return k


def _key(k: str) -> str:
    if k in PUNCT:
        return PUNCT[k]
    lk = k.lower()
    if lk in KEYS:
        return KEYS[lk]
    if re.fullmatch(r"f([1-9]|1[0-9]|2[0-4])", lk):
        return lk.upper()
    if len(k) == 1:
        return k.lower() if k.isalpha() else k
    return k  # assume a valid xkb keysym name (e.g. XF86AudioPlay, KP_Enter)


def _wtype(*args: str) -> None:
    check_not_paused()
    run(["wtype", *args], timeout=60)


def parse_combo(combo: str) -> tuple[list[str], str]:
    parts = [p for p in re.split(r"\s*\+\s*", combo.strip()) if p] if combo.strip() not in ("+",) else ["+"]
    if combo.strip().endswith("++"):
        parts = parts + ["+"]
    if not parts:
        raise CUError("empty key combo")
    *mods, key = parts
    return [_mod(m) for m in mods], _key(key)


def key(combo: str, repeat: int = 1) -> None:
    """Press a key combo like 'ctrl+shift+t', 'super+Return', 'Escape', 'alt+F4'. Space-separate
    several combos to press them in sequence: 'ctrl+a Delete'."""
    seq = combo.split() if " " in combo.strip() and combo.strip() != " " else [combo]
    args: list[str] = []
    for _ in range(max(1, int(repeat))):
        for c in seq:
            mods, k = parse_combo(c)
            for m in mods:
                args += ["-M", m]
            args += ["-k", k]
            for m in reversed(mods):
                args += ["-m", m]
            args += ["-s", "20"]
    _wtype(*args)


def type_text(text: str, delay_ms: int = 4, via_clipboard: bool = False) -> None:
    if via_clipboard:
        old = None
        try:
            old = run(["wl-paste", "-n"], timeout=3, check=False)
        except CUError:
            pass
        set_clipboard(text)
        time.sleep(0.05)
        key("ctrl+v")
        time.sleep(0.15)
        if old is not None:
            set_clipboard(old.decode(errors="replace"))
        return
    # wtype treats a leading '-' as an option; '--' ends option parsing
    for i, chunk in enumerate(text.split("\n")):
        if i:
            _wtype("-k", "Return")
        if chunk:
            _wtype("-d", str(delay_ms), "--", chunk)


def get_clipboard() -> str:
    return run(["wl-paste", "-n"], timeout=5, check=False).decode(errors="replace")


def set_clipboard(text: str) -> None:
    p = subprocess.Popen(["wl-copy"], stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         start_new_session=True)
    p.communicate(text.encode(), timeout=5)


# ------------------------------------------------------------------------ windows

def windows(all_workspaces: bool = True) -> list[dict]:
    S = scale_factor()
    active = Hypr.j("activewindow") or {}
    visible_ws = {m["activeWorkspace"]["id"] for m in Hypr.j("monitors")}
    out = []
    for c in Hypr.j("clients"):
        if not c.get("mapped", True) or c.get("hidden"):
            continue
        ws = c.get("workspace", {})
        vis = ws.get("id") in visible_ws
        if not all_workspaces and not vis:
            continue
        x, y = c["at"]; w, h = c["size"]
        out.append({
            "address": c["address"], "class": c.get("class", ""), "title": c.get("title", ""),
            "workspace": ws.get("name", ws.get("id")), "visible": vis,
            "focused": c["address"] == active.get("address"),
            "floating": c.get("floating", False), "fullscreen": c.get("fullscreen", 0),
            "pid": c.get("pid"),
            "box": [round(x * S), round(y * S), round(w * S), round(h * S)],
        })
    out.sort(key=lambda w: (not w["focused"], not w["visible"], str(w["workspace"])))
    return out


def find_window(selector: str) -> dict:
    """selector: '0xaddr' | 'address:0x..' | class name | title substring (case-insensitive)."""
    wins = windows()
    s = selector.strip()
    if s.startswith("address:"):
        s = s[8:]
    for w in wins:
        if w["address"] == s:
            return w
    low = s.lower()
    for pred in (lambda w: w["class"].lower() == low,
                 lambda w: low in w["class"].lower(),
                 lambda w: low in w["title"].lower()):
        m = [w for w in wins if pred(w)]
        if m:
            return m[0]
    raise CUError(f"no window matches {selector!r}")


def focus_window(selector: str) -> dict:
    check_not_paused()
    w = find_window(selector)
    a = w["address"]
    Hypr.dispatch(f'hl.dsp.focus({{ window = "address:{a}" }})', f"focuswindow address:{a}")
    time.sleep(0.15)
    return w


def close_window(selector: str | None = None) -> None:
    check_not_paused()
    if selector:
        a = find_window(selector)["address"]
        Hypr.dispatch(f'hl.dsp.window.close({{ window = "address:{a}" }})', f"closewindow address:{a}")
    else:
        Hypr.dispatch("hl.dsp.window.close()", "killactive")


def workspace(target: str) -> None:
    check_not_paused()
    Hypr.dispatch(f"hl.dsp.focus({{ workspace = {lua_str(str(target))} }})", f"workspace {target}")


def launch(command: str, wait_for_window: float = 8.0) -> dict | None:
    """Launch a command through Hyprland (detached, gets normal window rules). If
    wait_for_window > 0, wait for a new window to appear and return it."""
    check_not_paused()
    before = {c["address"] for c in Hypr.j("clients")}
    cmd = command
    if shutil.which("uwsm-app") and not command.startswith("uwsm"):
        cmd = f"uwsm-app -- {command}"
    Hypr.dispatch(f"hl.dsp.exec_cmd({lua_str(cmd)})", f"exec {cmd}")
    if wait_for_window <= 0:
        return None
    deadline = time.time() + wait_for_window
    while time.time() < deadline:
        time.sleep(0.25)
        new = [c for c in Hypr.j("clients") if c["address"] not in before and c.get("mapped", True)]
        if new:
            time.sleep(0.4)  # let it draw its first frame
            addr = new[0]["address"]
            return next((w for w in windows() if w["address"] == addr), None)
    return None


def hyprctl(args: str) -> str:
    """Raw hyprctl passthrough (e.g. 'clients -j', 'dispatch hl.dsp.window.float({action=\"toggle\"})')."""
    hc = shutil.which("hyprctl")
    if not hc:
        raise CUError("hyprctl not found")
    import shlex
    parts = shlex.split(args)
    if parts and parts[0] in ("dispatch", "keyword", "eval", "reload", "kill", "setcursor", "switchxkblayout", "output"):
        check_not_paused()
    return run([hc, *parts], timeout=15, check=False).decode(errors="replace")


# --------------------------------------------------------------------------- misc

def desktop_summary() -> str:
    mons = monitors()
    S = scale_factor(mons)
    cx, cy = cursor_position()
    lines = [f"screen space: S={S:.4f} (screen px per logical px); cursor at ({cx}, {cy})"]
    for i, m in enumerate(mons):
        ox, oy = to_screen(m.x, m.y, S)
        lines.append(f"monitor {i} {m.name}{' [focused]' if m.focused else ''}: box=({ox},{oy},{round(m.w * S)},{round(m.h * S)}) workspace={m.workspace}")
    for w in windows(all_workspaces=False):
        flag = "*" if w["focused"] else " "
        lines.append(f"{flag} window {w['address']} [{w['class']}] {w['title'][:80]!r} box={tuple(w['box'])}")
    return "\n".join(lines)


def doctor() -> list[tuple[str, bool, str]]:
    res = []
    for t, pkg in (("grim", "grim"), ("wtype", "wtype"), ("wl-copy", "wl-clipboard"), ("tesseract", "tesseract tesseract-data-eng"),
                   ("hyprctl", "hyprland"), ("cc", "base-devel"), ("wayland-scanner", "wayland"), ("magick", "imagemagick (optional)")):
        res.append((t, bool(shutil.which(t)), f"pacman -S {pkg}"))
    try:
        Hypr.j("version")
        res.append(("hyprland ipc", True, f"lua dispatch={Hypr.lua()}"))
    except Exception as e:
        res.append(("hyprland ipc", False, str(e)))
    try:
        res.append(("pointer helper", True, str(helper_path())))
    except Exception as e:
        res.append(("pointer helper", False, str(e)))
    res.append(("paused", not PAUSE_FILE.exists(), "run `omarchy-cu resume`" if PAUSE_FILE.exists() else "no"))
    return res


_MODBITS = [(64, "SUPER"), (4, "CTRL"), (8, "ALT"), (1, "SHIFT"), (128, "ALTGR")]


def keybindings(filter: str = "") -> list[str]:
    """Compact list of the user's Hyprland keybindings: 'SUPER+SHIFT+RETURN  Browser'."""
    out = []
    f = filter.lower().strip()
    for b in Hypr.j("binds"):
        if b.get("mouse") or b.get("submap"):
            continue
        mods = "+".join(n for bit, n in _MODBITS if b.get("modmask", 0) & bit)
        k = b.get("key") or (f"code:{b['keycode']}" if b.get("keycode") else "?")
        combo = f"{mods}+{k}" if mods else k
        desc = b.get("description") or f"{b.get('dispatcher')} {b.get('arg')}".strip()
        line = f"{combo:28} {desc}"
        if not f or f in line.lower():
            out.append(line)
    return out
