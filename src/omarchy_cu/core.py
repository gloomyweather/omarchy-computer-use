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
import signal
import socket
import subprocess
import sys
import tempfile
import threading
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


class NoMatch(CUError):
    """A selector matched nothing (as opposed to an IPC failure)."""


# set by the MCP server when the client cancels the running tool call; long waits poll it
CANCEL = threading.Event()


# ----------------------------------------------------------------------------- utils

def run(cmd: list[str], *, input: bytes | None = None, timeout: float = 20, check: bool = True) -> bytes:
    try:
        # never let children inherit our stdin: under MCP it is the JSON-RPC stream
        p = subprocess.run(cmd, input=input, capture_output=True, timeout=timeout,
                           stdin=None if input is not None else subprocess.DEVNULL)
    except FileNotFoundError:
        raise CUError(f"missing program: {cmd[0]} (install it with pacman)")
    except subprocess.TimeoutExpired:
        raise CUError(f"timeout: {' '.join(cmd[:3])}")
    if check and p.returncode != 0:
        raise CUError(f"{cmd[0]} failed ({p.returncode}): {p.stderr.decode(errors='replace').strip()}")
    return p.stdout


def check_not_paused() -> None:
    """Called before every input action: enforces the kill switch and lights the indicator."""
    if PAUSE_FILE.exists():
        raise CUError("computer use is PAUSED by the user (kill switch). Stop and ask the user; "
                      "they can resume with `omarchy-cu resume` or by clicking the on-screen pill.")
    mark_activity()


# --------------------------------------------------------------- self-protection

def _parent(pid: int) -> int:
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
        return int(stat.rsplit(")", 1)[1].split()[1])
    except (OSError, ValueError, IndexError):
        return 0


def _comm(pid: int) -> str:
    try:
        return Path(f"/proc/{pid}/comm").read_text().strip()
    except OSError:
        return ""


def _chain(pid: int) -> list[int]:
    out = []
    for _ in range(64):
        if pid <= 1 or pid in out:
            break
        out.append(pid)
        pid = _parent(pid)
    return out


def _mux_client_pids(server_pid: int, comm: str) -> list[int]:
    """PIDs of terminal clients attached to a tmux / screen / zellij server we run under."""
    try:
        if comm.startswith("tmux"):
            out = subprocess.run(["tmux", "list-clients", "-F", "#{client_pid}"], capture_output=True, text=True,
                                 timeout=2, stdin=subprocess.DEVNULL).stdout
            return [int(x) for x in out.split() if x.isdigit()]
    except Exception:
        pass
    # zellij / screen / unknown: every process with this server's name that is not the server itself
    pids = []
    for d in Path("/proc").iterdir():
        if d.name.isdigit() and int(d.name) != server_pid and _comm(int(d.name)) == comm:
            pids.append(int(d.name))
    return pids


_MUX = ("tmux", "tmux: server", "zellij", "screen", "SCREEN")


def _ancestor_pids() -> set[int]:
    """PIDs of this process and all its ancestors (agent CLI, shell, terminal emulator...). If we run
    inside a terminal multiplexer, the ancestors of its attached clients count too: their terminal
    window is where keystrokes would reach the agent."""
    out = set(_chain(os.getpid()))
    for pid in list(out):
        comm = _comm(pid)
        if comm in _MUX or comm.startswith("tmux"):
            for c in _mux_client_pids(pid, comm):
                out.update(_chain(c))
    return out


def own_window_addresses() -> set[str]:
    """Addresses of the window(s) hosting this agent (e.g. the terminal running Claude Code).
    OMARCHY_CU_OWN_WINDOW=0x... pins it explicitly (useful for single-process terminals that host
    many windows: then only that one is protected)."""
    pinned = os.environ.get("OMARCHY_CU_OWN_WINDOW")
    if pinned:
        return {pinned if pinned.startswith("0x") else f"0x{pinned}"}
    anc = _ancestor_pids()
    try:
        return {c["address"] for c in Hypr.j("clients") if c.get("pid") in anc}
    except Exception:
        return set()


def own_window_address() -> str | None:
    own = own_window_addresses()
    return next(iter(own)) if len(own) == 1 else (sorted(own)[0] if own else None)


def guard_keyboard_target(combos: list[tuple[list[str], str]] | None = None) -> None:
    """Refuse to type into the agent's own terminal: a stray Escape / ctrl+c / Enter there would
    interrupt or drive the agent itself. Override with OMARCHY_CU_ALLOW_SELF=1.
    `combos` (for key()): super+X combos that are plain compositor navigation binds are allowed."""
    if os.environ.get("OMARCHY_CU_ALLOW_SELF") == "1":
        return
    try:
        active = Hypr.j("activewindow") or {}
    except Exception:
        return
    if not active.get("address") or active.get("address") not in own_window_addresses():
        return
    if combos and all(_safe_compositor_combo(m, k) for m, k in combos):
        return
    if _interactive_layer_open():
        return  # a menu / launcher layer has keyboard focus, not the terminal
    raise CUError(f"the focused window [{active.get('class')}] {active.get('title', '')!r} is the agent's own "
                  "terminal; keyboard input there would hit the agent itself. Focus the target window first "
                  "(focus_window / click into it), then retry.")


# super+X binds go to the compositor, not the focused window, unless the bind acts on / types into it.
_UNSAFE_BIND = re.compile(r"close|kill|quit|exit|paste|copy|cut|select all|send|type|minimi|pop .*out|"
                          r"transparen|move window|pass", re.I)
_MODBIT = {"logo": 64, "ctrl": 4, "alt": 8, "shift": 1, "altgr": 128, "capslock": 2}
_DIGIT_KEYCODE = {str((i + 1) % 10): 10 + i for i in range(10)}  # xkb keycodes of the 1..9,0 row


def _safe_compositor_combo(mods: list[str], key: str) -> bool:
    """True if mods+key is a Hyprland bind (so the compositor eats it) that doesn't act on the window."""
    if "logo" not in mods:
        return False
    mask = sum(_MODBIT.get(m, 0) for m in set(mods))
    try:
        binds = Hypr.j("binds")
    except Exception:
        return False
    k = key.lower()
    for b in binds:
        if b.get("modmask") != mask or b.get("mouse") or b.get("submap"):
            continue
        bk = str(b.get("key", "")).lower()
        desc = b.get("description") or ""
        digit = (not bk and k.isdigit() and  # Omarchy's lua digit binds report neither key nor keycode
                 (_DIGIT_KEYCODE.get(k) == b.get("keycode") or
                  (not b.get("keycode") and re.search(rf"\bworkspace {int(k) or 10}$", desc))))
        if bk == k or digit:
            # without a description (lua binds report only an opaque `__lua <n>`) we can't tell: unsafe
            return bool(desc.strip()) and not _UNSAFE_BIND.search(desc)
    return False


PASSIVE_LAYERS = re.compile(r"^(omarchy-(background|bar|dock|notifications|osd)|omarchy-cu-.*|wallpaper|hyprpaper|swaybg|"
                            r"waybar|mako|swaync.*|notifications|gtk-layer-shell|hyprpicker|selection)$")


def _interactive_layer_open() -> bool:
    try:
        data = Hypr.j("layers")
    except Exception:
        return False
    for mon in data.values():
        for level in ("2", "3"):  # top, overlay
            for l in mon.get("levels", {}).get(level, []):
                if not PASSIVE_LAYERS.match(l.get("namespace", "")):
                    return True
    return False


# ------------------------------------------------------------------- indicator

ACTIVITY_FILE = CACHE / "activity"
HIDE_SENT = CACHE / "overlay.hide-sent"
INDICATOR = os.environ.get("OMARCHY_CU_INDICATOR", "1") not in ("0", "false", "no")
LAYER_SHELL_LIB = "/usr/lib/libgtk4-layer-shell.so"


def _overlay_pid() -> int | None:
    """PID of our running overlay, verified by its command line (a stale pidfile may name a reused pid:
    never signal an unrelated process)."""
    try:
        pid = int((CACHE / "overlay.pid").read_text().strip())
        cmdline = Path(f"/proc/{pid}/cmdline").read_bytes()
    except (OSError, ValueError):
        return None
    return pid if any(a.endswith(b"omarchy_cu/overlay.py") for a in cmdline.split(b"\0")) else None


def ensure_overlay() -> None:
    if not INDICATOR or _overlay_pid() or not Path(LAYER_SHELL_LIB).exists():
        return
    py = "/usr/bin/python3" if Path("/usr/bin/python3").exists() else (shutil.which("python3") or "python3")
    env = dict(os.environ)
    env["LD_PRELOAD"] = LAYER_SHELL_LIB + (":" + env["LD_PRELOAD"] if env.get("LD_PRELOAD") else "")
    try:
        subprocess.Popen([py, "-I", str(Path(__file__).with_name("overlay.py"))], env=env,
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         start_new_session=True, close_fds=True)
    except OSError:
        pass


def mark_activity() -> None:
    try:
        CACHE.mkdir(parents=True, exist_ok=True)
        ACTIVITY_FILE.touch()
    except OSError:
        return
    ensure_overlay()


def hide_overlay_for_capture() -> None:
    """Make sure the indicator is not in the captured pixels. The overlay hides its content on
    SIGUSR1 and keeps it hidden ~1.5 s after the last request; Hyprland needs ~0.2 s to drop it."""
    pid = _overlay_pid()
    if not pid:
        return
    try:
        recently = time.time() - HIDE_SENT.stat().st_mtime < 1.2
    except OSError:
        recently = False
    try:
        os.kill(pid, signal.SIGUSR1)
        HIDE_SENT.touch()
    except OSError:
        return
    if not recently:
        time.sleep(float(os.environ.get("OMARCHY_CU_HIDE_DELAY", "0.25")))


# ------------------------------------------------------------------------- hyprland

class Hypr:
    """Minimal Hyprland IPC client (same socket hyprctl uses)."""

    _lua: bool | None = None

    @staticmethod
    def _sock() -> str:
        sig = os.environ.get("HYPRLAND_INSTANCE_SIGNATURE")
        rt = os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
        if sig and Path(f"{rt}/hypr/{sig}/.socket.sock").exists():
            return f"{rt}/hypr/{sig}/.socket.sock"
        # launched outside the session env, or the env names a dead instance: use the newest one
        base = Path(rt) / "hypr"
        cands = sorted(base.glob("*/.socket.sock"), key=lambda p: p.stat().st_mtime, reverse=True) if base.exists() else []
        if not cands:
            raise CUError("not inside a Hyprland session (no Hyprland IPC socket found)")
        return str(cands[0])

    @classmethod
    def request(cls, msg: str) -> str:
        try:
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
        except OSError as e:  # includes socket.timeout
            raise CUError(f"hyprland ipc failed: {e}")
        return b"".join(chunks).decode(errors="replace")

    @classmethod
    def j(cls, what: str):
        return json.loads(cls.request(f"j/{what}"))

    @classmethod
    def lua(cls) -> bool:
        """Hyprland >= 0.55 with lua config wants `hl.dsp.*` expressions for dispatch."""
        if cls._lua is None:
            # side-effect-free probe (a cursor warp here could refocus a window with follow_mouse)
            cls._lua = cls.request("/dispatch hl.dsp.no_op()").strip() == "ok"
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
    hide_overlay_for_capture()
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
    words: list | None = None  # [(text, (x, y, w, h)), ...] per OCR word, screen space

    def public(self) -> dict:
        return {"text": self.text, "x": self.x, "y": self.y, "box": self.box, "conf": self.conf}


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


def _tess_words(img: Path, langs: str) -> list[tuple]:
    tsv = run(["tesseract", str(img), "-", "-l", langs, "--psm", "11", "tsv"], timeout=60).decode(errors="replace")
    out = []
    for row in csv.DictReader(io.StringIO(tsv), delimiter="\t", quoting=csv.QUOTE_NONE):
        t = (row.get("text") or "").strip()
        try:
            conf = float(row.get("conf", -1))
        except (TypeError, ValueError):
            continue
        if not t or conf < 35:
            continue
        out.append((t, int(row["left"]), int(row["top"]), int(row["width"]), int(row["height"]), conf))
    return out


def _overlap(a: tuple, b: tuple) -> float:
    ax0, ay0, ax1, ay1 = a[1], a[2], a[1] + a[3], a[2] + a[4]
    bx0, by0, bx1, by1 = b[1], b[2], b[1] + b[3], b[2] + b[4]
    iw, ih = min(ax1, bx1) - max(ax0, bx0), min(ay1, by1) - max(ay0, by0)
    if iw <= 0 or ih <= 0:
        return 0.0
    return iw * ih / max(1, min(a[3] * a[4], b[3] * b[4]))


def _ocr_words(png: bytes) -> tuple[list[tuple], int]:
    """Two OCR passes run in parallel and merged: (A) upscaled original, (B) grayscale,
    polarity-normalised (dark themes inverted) and binarised. B catches colored links and
    low-contrast text that A drops. Returns words in px of the upscaled image + the factor."""
    langs = _ocr_langs()
    with tempfile.TemporaryDirectory() as td:
        src = Path(td) / "s.png"
        src.write_bytes(png)
        iw, ih = png_size(png)
        if not shutil.which("magick"):
            return _tess_words(src, langs), 1
        up = 2 if max(iw, ih) <= 2600 else 1
        a, b = Path(td) / "a.png", Path(td) / "b.png"
        try:
            mean = float(run(["magick", str(src), "-colorspace", "gray", "-format", "%[fx:mean]", "info:"], timeout=20) or 0.5)
        except ValueError:
            mean = 0.5
        run(["magick", str(src), "-resize", f"{up * 100}%", str(a)], timeout=30)
        run(["magick", str(src), "-colorspace", "gray", *(["-negate"] if mean < 0.5 else []),
             "-resize", f"{up * 100}%", "-threshold", "55%", str(b)], timeout=30)
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(2) as ex:
            wa, wb = ex.map(lambda p: _tess_words(p, langs), (a, b))
    words = list(wa)
    for w in wb:
        if all(_overlap(w, x) < 0.3 for x in words):
            words.append(w)
    return words, up


def _rows(words: list[tuple]) -> list[list[tuple]]:
    """Cluster words into visual text runs: same row (vertical overlap) and small horizontal gaps."""
    words = sorted(words, key=lambda w: (w[2] + w[4] / 2, w[1]))
    rows: list[list] = []  # [y0, y1, words]
    for w in words:
        cy = w[2] + w[4] / 2
        for r in rows:
            if r[0] <= cy <= r[1] and 0.5 < w[4] / max(1, r[1] - r[0]) < 2.0:
                r[2].append(w)
                r[0] = min(r[0], w[2]); r[1] = max(r[1], w[2] + w[4])
                break
        else:
            rows.append([w[2], w[2] + w[4], [w]])
    out = []
    for _, _, r in rows:
        r.sort(key=lambda w: w[1])
        cur = [r[0]]
        for w in r[1:]:
            prev = cur[-1]
            if w[1] - (prev[1] + prev[3]) > 1.6 * max(prev[4], w[4]):
                out.append(cur); cur = [w]
            else:
                cur.append(w)
        out.append(cur)
    return out


def ocr_lines(region: tuple[float, float, float, float] | None = None, monitor: str | None = None) -> list[TextHit]:
    """OCR the screen at native resolution; returns one hit per visual text run."""
    mons = monitors()
    S = scale_factor(mons)
    native = max(m.scale for m in mons)
    if region:
        x, y, w, h = region
        lx, ly = to_logical(x, y, S)
        lw, lh = max(1, round(w / S)), max(1, round(h / S))
        png = _grim(f"{lx},{ly} {lw}x{lh}", None, native, False)
        org_l, span_l = (lx, ly), lw
    else:
        m = pick_monitor(monitor, mons)
        if m is None:
            png = _grim(None, None, native, False)
            org_l = (min(mm.x for mm in mons), min(mm.y for mm in mons))
            span_l = max(mm.x + mm.w for mm in mons) - org_l[0]
        else:
            png = _grim(None, m.name, native, False)
            org_l, span_l = (m.x, m.y), m.w
    iw, _ = png_size(png)
    words, up = _ocr_words(png)
    f = span_l / iw / up * S  # upscaled-image px -> screen px
    hits = []
    for g in _rows(words):
        x0 = min(w[1] for w in g); y0 = min(w[2] for w in g)
        x1 = max(w[1] + w[3] for w in g); y1 = max(w[2] + w[4] for w in g)
        bx, by = org_l[0] * S + x0 * f, org_l[1] * S + y0 * f
        bw, bh = (x1 - x0) * f, (y1 - y0) * f
        ws = [(w[0], (round(org_l[0] * S + w[1] * f), round(org_l[1] * S + w[2] * f),
                      round(w[3] * f), round(w[4] * f))) for w in g]
        hits.append(TextHit(" ".join(w[0] for w in g), round(bx + bw / 2), round(by + bh / 2),
                            (round(bx), round(by), round(bw), round(bh)),
                            round(sum(w[5] for w in g) / len(g), 1), ws))
    hits.sort(key=lambda h: (h.box[1] // 8, h.box[0]))
    return hits


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip().lower()


def _span_hit(h: TextHit, ws: list, q: str, conf: float) -> TextHit:
    """Hit for a contiguous span of OCR words; the box is narrowed further when the query is only part
    of a single word."""
    x0 = min(b[0] for _, b in ws); y0 = min(b[1] for _, b in ws)
    x1 = max(b[0] + b[2] for _, b in ws); y1 = max(b[1] + b[3] for _, b in ws)
    text = " ".join(t for t, _ in ws)
    if len(ws) == 1:
        t = _norm(text)
        i = t.find(q)
        if i > 0 or (i == 0 and len(q) < len(t)):
            n = max(1, len(t))
            nx0 = x0 + (x1 - x0) * i / n
            x1 = nx0 + (x1 - x0) * len(q) / n
            x0 = nx0
    x0, x1 = round(x0), round(x1)
    return TextHit(text, (x0 + x1) // 2, (y0 + y1) // 2, (x0, y0, x1 - x0, y1 - y0), conf, ws)


def _matches(h: TextHit, q: str, exact: bool) -> list[tuple[int, TextHit]]:
    """(rank, hit) for every minimal word span of an OCR run that matches q. rank 0 = the span is exactly
    q, 1 = q matches at word boundaries, 2 = plain substring (exact=True keeps rank 0 only)."""
    ws = h.words or [(h.text, h.box)]
    out = []
    for i in range(len(ws)):
        for j in range(i, len(ws)):
            t = _norm(" ".join(w for w, _ in ws[i:j + 1]))
            if j > i and len(t) > len(q) + 64:
                break
            if q not in t:
                continue
            # minimal from the left too: dropping word i must lose the match
            if j > i and q in _norm(" ".join(w for w, _ in ws[i + 1:j + 1])):
                break
            rank = 0 if t == q else 1 if re.search(rf"(?<!\w){re.escape(q)}(?!\w)", t) else 2
            if rank == 0 or not exact:
                out.append((rank, _span_hit(h, ws[i:j + 1], q, h.conf)))
            break
    return out


def find_text(query: str, region=None, monitor=None, exact: bool = False) -> list[TextHit]:
    """OCR text matches, best first: exact label > whole-word match > substring; ties in reading order.
    Without any match (and exact=False) falls back to fuzzy matching of whole runs."""
    q = _norm(query)
    if not q:
        raise CUError("empty query")
    lines = ocr_lines(region, monitor)
    ranked = []
    for n, h in enumerate(lines):
        for rank, hit in _matches(h, q, exact):
            ranked.append((rank, len(_norm(h.text)) - len(q), n, hit))  # a run that IS the label wins
    ranked.sort(key=lambda r: r[:3])
    out = [r[3] for r in ranked]
    if not out and not exact:
        import difflib
        scored = sorted(((difflib.SequenceMatcher(None, q, _norm(h.text)).ratio(), i, h) for i, h in enumerate(lines)),
                        key=lambda p: (-p[0], p[1]))
        out = [h for r, _, h in scored[:3] if r >= 0.75]
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


def _clear_pill(points: list[tuple[int, int]]) -> None:
    """The indicator pill is hidden from screenshots but clickable (it toggles pause). Before pointer
    input lands on it, hide the overlay so the click reaches the app underneath instead."""
    try:
        layers = Hypr.j("layers")
    except Exception:
        return
    for mon in layers.values():
        for lv in mon.get("levels", {}).values():
            for l in lv:
                if l.get("namespace") != "omarchy-cu-pill":
                    continue
                x, y, w, h = l["x"], l["y"], l["w"], l["h"]
                if any(x - 4 <= px <= x + w + 4 and y - 4 <= py <= y + h + 4 for px, py in points):
                    hide_overlay_for_capture()
                    return


def pointer(*args: str | int, release: bool = False, pill_cleared: bool = False) -> None:
    """Run the pointer helper. release=True marks a pure button release, which must work even while
    paused (the kill switch must never leave a button stuck down)."""
    if release:
        mark_activity()
    else:
        check_not_paused()
    pts = [(int(args[i + 1]), int(args[i + 2])) for i, a in enumerate(args) if a == "move" and i + 2 < len(args)]
    if not pts and any(a in ("click", "down") for a in args):
        try:
            pts = [Hypr.cursor()]
        except Exception:
            pass
    if pts and not pill_cleared:
        _clear_pill(pts)
    timeout = 60 + sum(int(args[i + 1]) for i, a in enumerate(args) if a == "sleep" and i + 1 < len(args)) / 1000
    exe = str(helper_path())
    p = subprocess.Popen([exe, *map(str, args)], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.PIPE)
    try:
        _, err = p.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        p.terminate()  # SIGTERM: the helper releases any button it still holds
        try:
            p.communicate(timeout=3)
        except subprocess.TimeoutExpired:
            p.kill()
        raise CUError("timeout: pointer helper")
    if p.returncode != 0:
        raise CUError(f"pointer helper failed ({p.returncode}): {err.decode(errors='replace').strip()}")


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
    mods = sorted({_mod(m) for m in (modifiers or [])})
    if not mods:
        pointer(*args)
        return
    check_not_paused()
    # A separate wtype process must hold the modifiers during the click (wtype drops them on exit). It
    # must also release them itself with -m: if it is just killed, apps keep the stale modifier state
    # and every later plain click arrives as ctrl/shift-click. So it holds for a fixed time and exits
    # normally. Everything slow (helper build, hiding the indicator pill) happens before it starts.
    helper_path()
    try:
        _clear_pill([tuple(args[1:3])] if args[0] == "move" else [Hypr.cursor()])
    except Exception:
        pass
    n = max(1, int(count))
    hold_ms = 600 + 90 * n
    cmd = ["wtype", *sum((["-M", m] for m in mods), []), "-s", str(hold_ms), *sum((["-m", m] for m in mods), [])]
    try:
        hp = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    except FileNotFoundError:
        raise CUError("missing program: wtype")
    try:
        time.sleep(0.1)  # let the modifier state reach the compositor
        if hp.poll() is not None:
            raise CUError(f"wtype failed: {hp.stderr.read().decode(errors='replace').strip()}")
        pointer(*args, pill_cleared=True)
    finally:
        try:
            hp.wait(hold_ms / 1000 + 3)
        except subprocess.TimeoutExpired:
            hp.kill()


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
    pointer("up", button, release=True)


def cursor_position() -> tuple[int, int]:
    return to_screen(*Hypr.cursor())


# ----------------------------------------------------------------------- keyboard

MODS = {"ctrl": "ctrl", "control": "ctrl", "shift": "shift", "alt": "alt", "option": "alt",
        "super": "logo", "win": "logo", "meta": "logo", "cmd": "logo", "logo": "logo", "mod4": "logo",
        "altgr": "altgr", "caps": "capslock", "capslock": "capslock"}

KEYS = {
    "enter": "Return", "return": "Return", "esc": "Escape", "escape": "Escape", "tab": "Tab",
    "space": "space", "backspace": "BackSpace", "bksp": "BackSpace", "delete": "Delete", "del": "Delete",
    "insert": "Insert", "ins": "Insert", "home": "Home", "end": "End", "pageup": "Prior", "pgup": "Prior",
    "pagedown": "Next", "pgdn": "Next", "page_up": "Prior", "page_down": "Next",
    "up": "Up", "down": "Down", "left": "Left", "right": "Right",
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
        if k.isascii() and k.isalnum():
            return lk
        return f"U{ord(k):04X}"  # any other character (?, !, *, ф, …) via its unicode keysym
    return k  # assume a valid xkb keysym name (e.g. XF86AudioPlay, KP_Enter)


def _wtype(*args: str, timeout: float = 60) -> None:
    check_not_paused()
    run(["wtype", *args], timeout=timeout)


def parse_combo(combo: str) -> tuple[list[str], str]:
    c = combo.strip() or (" " if combo else "")
    if c in ("+", " "):
        return [], _key(c)
    parts = [p for p in re.split(r"\s*\+\s*", c) if p]
    if c.endswith("++"):
        parts = parts + ["+"]
    if not parts:
        raise CUError("empty key combo")
    *mods, key = parts
    return [_mod(m) for m in mods], _key(key)


_MOD_WORDS = "|".join(sorted(MODS, key=len, reverse=True))


def split_combos(combo: str) -> list[str]:
    """'ctrl+a Delete' -> ['ctrl+a', 'Delete']; spaces around '+' after a modifier don't split
    ('ctrl + a'), a lone '+' stays its own key ('ctrl+a +'). ' ' alone is the space key."""
    if not combo:
        raise CUError("empty key combo")
    if not combo.strip():
        return [" "]
    c = re.sub(rf"(?i)\b({_MOD_WORDS})\s*\+\s*(?=\S)", r"\1+", combo.strip())
    return c.split()


def key(combo: str, repeat: int = 1) -> None:
    """Press a key combo like 'ctrl+shift+t', 'super+Return', 'Escape', 'alt+F4'. Space-separate
    several combos to press them in sequence: 'ctrl+a Delete'."""
    seq = split_combos(combo)
    parsed = [parse_combo(c) for c in seq]
    guard_keyboard_target(parsed)
    args: list[str] = []
    for _ in range(max(1, int(repeat))):
        for mods, k in parsed:
            # modifiers are sent as modifier state (-M/-m), never as real Shift_L/Alt_L key presses:
            # those would trip xkb layout-switch options like grp:alt_shift_toggle.
            for m in mods:
                args += ["-M", m]
            args += ["-k", k]
            for m in reversed(mods):
                args += ["-m", m]
            args += ["-s", "20"]
    _wtype(*args)


TYPE_CHUNK = 48  # chars per wtype call: pause / focus are re-checked between chunks


def type_text(text: str, delay_ms: int = 4, via_clipboard: bool = False) -> None:
    check_not_paused()
    guard_keyboard_target()
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    if via_clipboard:
        _paste_text(text)
        return
    lines = text.split("\n")
    first, done = True, 0
    try:
        for i, line in enumerate(lines):
            if i:
                guard_keyboard_target()
                _wtype("-k", "Return")
                done += 1
            for j in range(0, len(line), TYPE_CHUNK):
                if not first:
                    guard_keyboard_target()  # focus may have moved (e.g. the app closed after Enter)
                first = False
                chunk = line[j:j + TYPE_CHUNK]
                # wtype treats a leading '-' as an option; '--' ends option parsing
                _wtype("-d", str(delay_ms), "--", chunk, timeout=20 + len(chunk) * (delay_ms + 10) / 1000)
                done += len(chunk)
    except CUError as e:
        if done:
            raise CUError(f"{e} [stopped after typing {done} of {len(text)} chars: don't retype them]")
        raise


# --------------------------------------------------------------------- clipboard

_TEXT_MIMES = ("text/plain;charset=utf-8", "text/plain", "UTF8_STRING", "STRING", "TEXT", "text")


def _clip_types() -> list[str]:
    try:
        return run(["wl-paste", "-l"], timeout=3, check=False).decode(errors="replace").split()
    except CUError:
        return []


def _clip_save() -> tuple[str, bytes] | None:
    """(mime, raw bytes) of the current clipboard's preferred type, or None when empty."""
    types = _clip_types()
    if not types:
        return None
    mime = next((t for t in _TEXT_MIMES if t in types), types[0])
    try:
        p = subprocess.run(["wl-paste", "-n", "-t", mime], capture_output=True, timeout=5, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return (mime, p.stdout) if p.returncode == 0 else None


def _wl_copy(data: bytes, mime: str | None = None, sensitive: bool = False) -> subprocess.Popen:
    cmd = ["wl-copy"] + (["-t", mime] if mime else []) + (["--sensitive"] if sensitive else [])
    try:
        p = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             start_new_session=True)
    except FileNotFoundError:
        raise CUError("missing program: wl-copy (pacman -S wl-clipboard)")
    p.stdin.write(data)
    p.stdin.close()
    return p


PASTE_WAIT = float(os.environ.get("OMARCHY_CU_PASTE_WAIT", "0.6"))


def _paste_text(text: str) -> None:
    """Paste via the clipboard, then restore the user's previous clipboard (any mime type). The restore
    waits PASTE_WAIT s so slow apps fetch the text first (`--paste-once` can't be used to detect the
    paste: Omarchy's clipboard history watcher is always the first reader)."""
    old = _clip_save()
    # --sensitive: keep it out of clipboard history (it may be a password)
    p = _wl_copy(text.encode(), "text/plain;charset=utf-8", sensitive=True)
    try:
        p.wait(timeout=5)  # forks into the background once it owns the selection
    except subprocess.TimeoutExpired:
        pass
    time.sleep(0.05)
    try:
        key("ctrl+v")
        time.sleep(PASTE_WAIT)
    finally:
        if old is not None:  # note: only the preferred mime type of a multi-type selection survives
            _wl_copy(old[1], old[0]).wait(timeout=5)
        else:
            subprocess.run(["wl-copy", "--clear"], timeout=5, stdin=subprocess.DEVNULL,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def get_clipboard() -> str:
    types = _clip_types()
    if not types:
        return ""
    mime = next((t for t in _TEXT_MIMES if t in types), None)
    if mime is None:
        try:
            n = len(run(["wl-paste", "-n", "-t", types[0]], timeout=5, check=False))
        except CUError:
            n = 0
        return f"[clipboard holds non-text data: {types[0]}, {n} bytes]"
    return run(["wl-paste", "-n", "-t", mime], timeout=5, check=False).decode(errors="replace")


def set_clipboard(text: str) -> None:
    p = _wl_copy(text.encode(), "text/plain;charset=utf-8")
    try:
        p.wait(timeout=5)
    except subprocess.TimeoutExpired:
        pass


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
    raise NoMatch(f"no window matches {selector!r}")


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
    if shutil.which("uwsm-app") and not command.lstrip().startswith("uwsm"):
        cmd = f"uwsm-app -- {_shell_argv(command)}"
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


def _shell_argv(command: str) -> str:
    """`command` as uwsm-app argv: unchanged if it is a plain `prog args…`, else wrapped in `sh -c` so
    env assignments (`A=1 app`), `cd x && app`, pipes etc. keep working."""
    import shlex
    try:
        toks = shlex.split(command)
    except ValueError:
        toks = []
    plain = (toks and not re.search(r"[;&|<>()$`*?~{}\[\]!#\n]", command) and "=" not in toks[0])
    return command if plain else f"sh -c {shlex.quote(command)}"


_HYPRCTL_READONLY = {"activewindow", "activeworkspace", "animations", "binds", "clients", "configerrors",
                     "cursorpos", "decorations", "devices", "getoption", "getprop", "globalshortcuts",
                     "instances", "layers", "layouts", "monitors", "rollinglog", "splash", "systeminfo",
                     "version", "workspacerules", "workspaces", "descriptions", "help", "--help", "-h"}
_HYPRCTL_FLAGS_WITH_VALUE = {"-i", "--instance"}


def hyprctl(args: str) -> str:
    """Raw hyprctl passthrough (e.g. 'clients -j', 'dispatch hl.dsp.window.float({action=\"toggle\"})')."""
    hc = shutil.which("hyprctl")
    if not hc:
        raise CUError("hyprctl not found")
    import shlex
    m = re.match(r"^\s*((?:-[jrq]\s+)*)(dispatch|eval)\s+(.*)$", args, re.S)
    if m:
        # send verbatim over the socket: hyprctl's argv joining would strip lua string quotes
        check_not_paused()
        prefix = "j" if "-j" in m.group(1).split() else ""
        return Hypr.request(f"{prefix}/{m.group(2)} {m.group(3).strip()}")
    try:
        parts = shlex.split(args)
    except ValueError as e:
        raise CUError(f"bad hyprctl args: {e}")
    # find the command word (skip flags like -j, -r, -i N); anything not known read-only (incl. --batch)
    # counts as a desktop change and is blocked by the kill switch
    i, verb = 0, None
    while i < len(parts):
        t = parts[i]
        if t in _HYPRCTL_FLAGS_WITH_VALUE:
            i += 2; continue
        if t.startswith("-") and t not in ("--help", "-h"):
            if t == "--batch":
                verb = "--batch"; break
            i += 1; continue
        verb = t; break
    if verb not in _HYPRCTL_READONLY or "--batch" in parts:
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
    own = own_window_address()
    for w in windows(all_workspaces=False):
        flag = "*" if w["focused"] else " "
        me = "  <- agent's own terminal (don't type here)" if w["address"] == own else ""
        lines.append(f"{flag} window {w['address']} [{w['class']}] {w['title'][:80]!r} box={tuple(w['box'])}{me}")
    return "\n".join(lines)


def doctor() -> list[tuple[str, bool, str]]:
    res = []
    for t, pkg in (("grim", "grim"), ("wtype", "wtype"), ("wl-copy", "wl-clipboard"), ("tesseract", "tesseract tesseract-data-eng"),
                   ("hyprctl", "hyprland"), ("cc", "base-devel"), ("wayland-scanner", "wayland"), ("magick", "imagemagick (optional)")):
        res.append((t, bool(shutil.which(t)), f"pacman -S {pkg}"))
    if shutil.which("tesseract"):
        langs = _ocr_langs()
        has_rus = "rus" in langs.split("+")
        res.append(("ocr languages", has_rus, langs if has_rus else
                    f"{langs}; no Cyrillic: pacman -S tesseract-data-rus (or set OMARCHY_CU_OCR_LANGS)"))
    try:
        Hypr.j("version")
        res.append(("hyprland ipc", True, f"lua dispatch={Hypr.lua()}"))
    except Exception as e:
        res.append(("hyprland ipc", False, str(e)))
    gi_ok = subprocess.run([sys.executable, "-c", "import gi; gi.require_version('Atspi', '2.0')"],
                           capture_output=True, stdin=subprocess.DEVNULL).returncode == 0
    res.append(("python-gobject", gi_ok, "ok" if gi_ok else f"{sys.executable} lacks gi: pacman -S python-gobject "
                "(ui_tree / indicator; or set OMARCHY_CU_PYTHON=/usr/bin/python3)"))
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


# ------------------------------------------------------------- window management

def _target(sel: str | None) -> tuple[str, str]:
    """(lua `window = ...` fragment, legacy `address:...` selector) for a window selector."""
    if not sel:
        return "", ""
    a = find_window(sel)["address"]
    return f', window = "address:{a}"', f"address:{a}"


def window_action(action: str, window: str | None = None, workspace: str | None = None,
                  x: float | None = None, y: float | None = None,
                  width: float | None = None, height: float | None = None,
                  direction: str | None = None) -> str:
    """Hyprland window management. Coordinates/sizes are in screen space."""
    check_not_paused()
    lw, lg = _target(window)
    lw0 = lw[2:] if lw else ""  # without leading ', '
    S = scale_factor()
    act = action.lower()
    if act in ("float", "toggle_float", "tile"):
        mode = {"float": "on", "tile": "off"}.get(act, "toggle")
        legacy = {"on": "setfloating", "off": "settiled", "toggle": "togglefloating"}[mode]
        Hypr.dispatch(f'hl.dsp.window.float({{ action = "{mode}"{lw} }})', f"{legacy} {lg}".strip())
    elif act in ("fullscreen", "maximize"):
        mode = "fullscreen" if act == "fullscreen" else "maximized"
        if lg and not Hypr.lua():
            focus_window(lg)  # legacy `fullscreen` only acts on the focused window
        Hypr.dispatch(f'hl.dsp.window.fullscreen({{ mode = "{mode}", action = "toggle"{lw} }})',
                      f"fullscreen {0 if mode == 'fullscreen' else 1}")
    elif act in ("move_to_workspace", "send_to_workspace"):
        if not workspace:
            raise CUError("workspace is required")
        follow = "true" if act == "move_to_workspace" else "false"
        legacy = "movetoworkspace" if act == "move_to_workspace" else "movetoworkspacesilent"
        Hypr.dispatch(f'hl.dsp.window.move({{ workspace = {lua_str(str(workspace))}, follow = {follow}{lw} }})',
                      f"{legacy} {workspace}" + (f",{lg}" if lg else ""))
    elif act == "move":
        if x is None or y is None:
            raise CUError("x and y are required (screen space, top-left of the window)")
        lx, ly = to_logical(x, y, S)
        Hypr.dispatch(f'hl.dsp.window.move({{ x = {lx}, y = {ly}{lw} }})',
                      f"movewindowpixel exact {lx} {ly}" + (f",{lg}" if lg else ""))
    elif act == "resize":
        if width is None or height is None:
            raise CUError("width and height are required (screen space)")
        lx, ly = round(width / S), round(height / S)
        Hypr.dispatch(f'hl.dsp.window.resize({{ x = {lx}, y = {ly}{lw} }})',
                      f"resizewindowpixel exact {lx} {ly}" + (f",{lg}" if lg else ""))
    elif act in ("swap", "move_direction", "focus_direction"):
        d = (direction or "").lower()[:1]
        if d not in ("l", "r", "u", "d"):
            raise CUError("direction must be left/right/up/down")
        if act == "focus_direction":
            if lg:
                focus_window(lg)
            Hypr.dispatch(f'hl.dsp.focus({{ direction = "{d}" }})', f"movefocus {d}")
            time.sleep(0.15)
            return f"{action} ok"
        if lg:
            focus_window(lg)  # swap / directional move act on the focused window
        if act == "swap":
            Hypr.dispatch(f'hl.dsp.window.swap({{ direction = "{d}" }})', f"swapwindow {d}")
        else:
            Hypr.dispatch(f'hl.dsp.window.move({{ direction = "{d}" }})', f"movewindow {d}")
    elif act == "pin":
        Hypr.dispatch(f"hl.dsp.window.pin({{ {lw0} }})" if lw0 else "hl.dsp.window.pin()", f"pin {lg}".strip())
    elif act == "close":
        close_window(window)
    elif act == "focus":
        focus_window(window or "")
    else:
        raise CUError("action must be one of: focus, close, float, tile, toggle_float, fullscreen, maximize, "
                      "move_to_workspace, send_to_workspace, move, resize, swap, move_direction, focus_direction, pin")
    time.sleep(0.15)
    return f"{action} ok"


# ------------------------------------------------------------------ apps (.desktop)

def apps(query: str = "") -> list[dict]:
    """Installed desktop applications (name, desktop id, comment), filtered by query."""
    dirs = [Path.home() / ".local/share/applications"]
    for d in os.environ.get("XDG_DATA_DIRS", "/usr/local/share:/usr/share").split(":"):
        dirs.append(Path(d) / "applications")
    seen, out = set(), []
    q = query.lower().strip()
    for d in dirs:
        if not d.is_dir():
            continue
        for f in sorted(d.rglob("*.desktop")):
            did = str(f.relative_to(d)).replace("/", "-")
            if did in seen:
                continue
            seen.add(did)
            info, in_entry = {}, False
            try:
                for line in f.read_text(errors="replace").splitlines():
                    if line.startswith("["):
                        in_entry = line.strip() == "[Desktop Entry]"
                        continue
                    if in_entry and "=" in line:
                        k, v = line.split("=", 1)
                        info.setdefault(k.strip(), v.strip())
            except OSError:
                continue
            if info.get("NoDisplay") == "true" or info.get("Hidden") == "true" or info.get("Type", "Application") != "Application":
                continue
            item = {"name": info.get("Name", did), "id": did, "comment": info.get("Comment", "")[:80]}
            hay = f"{item['name']} {did} {item['comment']} {info.get('Keywords', '')} {info.get('GenericName', '')}".lower()
            if not q or q in hay:
                out.append(item)
    return out


def launch_app(name: str, wait_for_window: float = 8.0) -> dict | None:
    """Launch an installed app by name or desktop id (fuzzy), like the app launcher does."""
    cands = apps(name)
    if not cands:
        raise CUError(f"no installed app matches {name!r} (see `apps`)")
    low = name.lower()
    cands.sort(key=lambda a: (a["name"].lower() != low, a["id"].lower() != f"{low}.desktop",
                              not a["name"].lower().startswith(low), len(a["name"])))
    did = cands[0]["id"]
    import shlex
    if shutil.which("uwsm-app"):
        cmd = f"uwsm-app -- {shlex.quote(did)}"
    elif shutil.which("gtk-launch"):
        cmd = f"gtk-launch {shlex.quote(did[:-8] if did.endswith('.desktop') else did)}"
    else:
        raise CUError("need uwsm-app or gtk-launch to start desktop apps")
    return launch(cmd, wait_for_window)


# ---------------------------------------------------------------- omarchy native

def omarchy(args: str, timeout: float = 60) -> str:
    """Run the Omarchy command center: `omarchy <args>` (theme, font, audio, brightness, wifi,
    bluetooth, screenshot, notification, menu, toggle, ...)."""
    import shlex
    exe = shutil.which("omarchy")
    if not exe:
        raise CUError("`omarchy` command not found (not an Omarchy system?)")
    parts = shlex.split(args)
    if parts and parts[0] == "omarchy":  # catalog routes are listed as 'omarchy theme set …'
        parts = parts[1:]
    if not parts:
        raise CUError("give omarchy arguments, e.g. 'theme list' (see omarchy_commands)")
    if parts[0] not in ("commands", "--help", "-h", "help") and "--help" not in parts:
        check_not_paused()
    p = subprocess.run([exe, *parts], capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL)
    out = (p.stdout + (("\n" + p.stderr) if p.stderr.strip() else "")).strip()
    if p.returncode != 0:
        raise CUError(f"omarchy {args} exited {p.returncode}: {out[-2000:]}")
    return out or "ok"


def omarchy_commands(query: str = "") -> list[str]:
    exe = shutil.which("omarchy")
    if not exe:
        raise CUError("`omarchy` command not found")
    data = json.loads(run([exe, "commands", "--json"], timeout=20))
    q = query.lower().strip()
    out = []
    for c in data.get("commands", []):
        if c.get("hidden"):
            continue
        route = c["route"][len("omarchy "):] if c["route"].startswith("omarchy ") else c["route"]
        line = f"{route} {c.get('args', '')}".strip()
        line += f"  — {c.get('summary', '')}" + (" [sudo]" if c.get("requires_sudo") else "")
        if not q or all(w in line.lower() for w in q.split()):
            out.append(line)
    return out


def notify(title: str, body: str = "", urgency: str = "normal", timeout_ms: int = 6000) -> None:
    """Desktop notification to the user (Omarchy notification center, fallback notify-send)."""
    exe = shutil.which("omarchy-notification-send")
    if exe:
        # no `--` support there: keep a text starting with '-' from being parsed as an option
        t, b = (" " + x if x.startswith("-") else x for x in (title, body))
        cmd = [exe, "--app-name", _agent_label(), "-u", urgency, "-t", str(timeout_ms), t] + ([b] if body else [])
    elif shutil.which("notify-send"):
        cmd = ["notify-send", "-a", _agent_label(), "-u", urgency, "-t", str(timeout_ms), "--", title] + ([body] if body else [])
    else:
        raise CUError("no notification tool found")
    run(cmd, timeout=10)


def _agent_label() -> str:
    n = os.environ.get("OMARCHY_CU_AGENT_NAME")
    if n:
        return n
    if os.environ.get("CLAUDECODE") or os.environ.get("CLAUDE_CODE_ENTRYPOINT"):
        return "Claude"
    return "AI agent"


def ask_user(question: str, options: list[str] | None = None, timeout: float = 300) -> str | None:
    """Ask the user via a native Omarchy menu popup: pick one of `options`, or free text if none.
    Returns the answer, or None if dismissed / timed out."""
    sel, inp = shutil.which("omarchy-menu-select"), shutil.which("omarchy-menu-input")
    title = f"{_agent_label()}: {question}"
    if options:
        if sel:
            cmd = [sel, title, *options]
        elif shutil.which("walker"):
            cmd = ["walker", "--dmenu", "-p", title]
        else:
            raise CUError("no menu tool (omarchy-menu-select / walker) available")
    else:
        if inp:
            cmd = [inp, title]
        elif shutil.which("walker"):
            cmd = ["walker", "--dmenu", "-p", title]
        else:
            raise CUError("no input tool (omarchy-menu-input / walker) available")
    walker = cmd[0].endswith("walker")
    p = subprocess.Popen(cmd, stdin=subprocess.PIPE if walker else subprocess.DEVNULL, stdout=subprocess.PIPE,
                         stderr=subprocess.DEVNULL, text=True, start_new_session=True)
    if walker:
        p.stdin.write("\n".join(options or []))
        p.stdin.close()
    deadline = time.time() + timeout
    while p.poll() is None:
        if time.time() > deadline or CANCEL.is_set():
            # the helper only waits for the shell's menu; killing it leaves the popup open with keyboard
            # focus, so close the menu too
            try:
                os.killpg(p.pid, signal.SIGTERM)
            except OSError:
                pass
            if shutil.which("omarchy-shell") and not walker:
                subprocess.run(["omarchy-shell", "-q", "shell", "hide", "omarchy.menu"], timeout=5,
                               stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            p.wait(5)
            return None
        time.sleep(0.1)
    ans = (p.stdout.read() or "").strip()
    return ans if p.returncode == 0 and ans else None


# --------------------------------------------------------------------- waiting

def wait_for(text: str | None = None, window: str | None = None, gone: bool = False,
             timeout: float = 15, interval: float = 0.7) -> dict:
    """Poll until on-screen text (OCR) or a window (class/title) appears — or disappears if gone."""
    if not text and not window:
        raise CUError("give text or window")
    t0 = time.time()
    while True:
        found = None
        if window:
            try:
                found = find_window(window)
            except NoMatch:
                found = None
        else:
            hits = find_text(text)
            found = hits[0].public() if hits else None
        if bool(found) != gone:
            return {"ok": True, "seconds": round(time.time() - t0, 1), "match": found}
        if time.time() - t0 > timeout or CANCEL.is_set():
            return {"ok": False, "seconds": round(time.time() - t0, 1), "match": None}
        time.sleep(interval)
