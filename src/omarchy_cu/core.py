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
    """Called before every input action: enforces the kill switch and lights the indicator."""
    if PAUSE_FILE.exists():
        raise CUError("computer use is PAUSED by the user (kill switch). Stop and ask the user; "
                      "they can resume with `omarchy-cu resume` or by clicking the on-screen pill.")
    mark_activity()


# --------------------------------------------------------------- self-protection

def _ancestor_pids() -> set[int]:
    """PIDs of this process and all its ancestors (agent CLI, shell, terminal emulator...)."""
    out, pid = set(), os.getpid()
    for _ in range(64):
        out.add(pid)
        try:
            stat = Path(f"/proc/{pid}/stat").read_text()
            ppid = int(stat.rsplit(")", 1)[1].split()[1])
        except (OSError, ValueError, IndexError):
            break
        if ppid <= 1:
            break
        pid = ppid
    return out


def own_window_address() -> str | None:
    """Address of the window that hosts this agent (e.g. the terminal running Claude Code)."""
    anc = _ancestor_pids()
    try:
        for c in Hypr.j("clients"):
            if c.get("pid") in anc:
                return c["address"]
    except Exception:
        pass
    return None


def guard_keyboard_target() -> None:
    """Refuse to type into the agent's own terminal: a stray Escape / ctrl+c / Enter there would
    interrupt or drive the agent itself. Override with OMARCHY_CU_ALLOW_SELF=1."""
    if os.environ.get("OMARCHY_CU_ALLOW_SELF") == "1":
        return
    try:
        active = Hypr.j("activewindow") or {}
    except Exception:
        return
    if active.get("pid") and active.get("pid") in _ancestor_pids():
        if _interactive_layer_open():
            return  # a menu / launcher layer has keyboard focus, not the terminal
        raise CUError(f"the focused window [{active.get('class')}] {active.get('title', '')!r} is the agent's own "
                      "terminal; keyboard input there would hit the agent itself. Focus the target window first "
                      "(focus_window / click into it), then retry.")


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
    try:
        pid = int((CACHE / "overlay.pid").read_text().strip())
        os.kill(pid, 0)
        return pid
    except (OSError, ValueError):
        return None


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
    polarity-normalised (dark themes inverted) and binarised — B catches colored links /
    low-contrast text that A drops. Returns words in image px of the *upscaled* image + factor."""
    langs = _ocr_langs()
    with tempfile.TemporaryDirectory() as td:
        src = Path(td) / "s.png"
        src.write_bytes(png)
        iw, ih = png_size(png)
        up = 2 if max(iw, ih) <= 2600 else 1
        if not shutil.which("magick"):
            return _tess_words(src, langs), 1
        a, b = Path(td) / "a.png", Path(td) / "b.png"
        mean = float(run(["magick", str(src), "-colorspace", "gray", "-format", "%[fx:mean]", "info:"], timeout=20) or 0.5)
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
    rows: list[list[tuple]] = []
    for w in words:
        cy = w[2] + w[4] / 2
        for r in rows:
            ry0 = min(x[2] for x in r); ry1 = max(x[2] + x[4] for x in r)
            if ry0 <= cy <= ry1 and abs((ry1 - ry0) - w[4]) < max(w[4], ry1 - ry0) * 0.8:
                r.append(w)
                break
        else:
            rows.append([w])
    out = []
    for r in rows:
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
    seq0 = combo.split() if " " in combo.strip() and combo.strip() != " " else [combo]
    # super+... combos are compositor shortcuts (Hyprland binds), safe even over the agent's terminal
    if not all("logo" in parse_combo(c)[0] for c in seq0):
        guard_keyboard_target()
    seq = combo.split() if " " in combo.strip() and combo.strip() != " " else [combo]
    args: list[str] = []
    for _ in range(max(1, int(repeat))):
        for c in seq:
            mods, k = parse_combo(c)
            # modifiers are sent as modifier state (-M/-m), never as real Shift_L/Alt_L key presses:
            # those would trip xkb layout-switch options like grp:alt_shift_toggle.
            press = [("-M", m, "-m") for m in mods]
            for on, sym, _off in press:
                args += [on, sym]
            args += ["-k", k]
            for _on, sym, off in reversed(press):
                args += [off, sym]
            args += ["-s", "20"]
    _wtype(*args)


def type_text(text: str, delay_ms: int = 4, via_clipboard: bool = False) -> None:
    guard_keyboard_target()
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
    stripped = args.strip()
    if stripped.startswith("dispatch "):
        # send verbatim over the socket: hyprctl's argv joining would strip lua string quotes
        check_not_paused()
        return Hypr.request("/dispatch " + stripped[len("dispatch "):].strip())
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
            Hypr.dispatch(f'hl.dsp.focus({{ direction = "{d}" }})', f"movefocus {d}")
        elif act == "swap":
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
    if shutil.which("uwsm-app"):
        cmd = f"uwsm-app -- {did}"
    elif shutil.which("gtk-launch"):
        cmd = f"gtk-launch {did[:-8] if did.endswith('.desktop') else did}"
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
    if parts and parts[0] not in ("commands", "--help", "-h", "help") and "--help" not in parts:
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
        line = f"{c['route']} {c.get('args', '')}".strip()
        line += f"  — {c.get('summary', '')}" + (" [sudo]" if c.get("requires_sudo") else "")
        if not q or all(w in line.lower() for w in q.split()):
            out.append(line)
    return out


def notify(title: str, body: str = "", urgency: str = "normal", timeout_ms: int = 6000) -> None:
    """Desktop notification to the user (Omarchy notification center, fallback notify-send)."""
    exe = shutil.which("omarchy-notification-send")
    if exe:
        cmd = [exe, "--app-name", _agent_label(), "-u", urgency, "-t", str(timeout_ms), title] + ([body] if body else [])
    elif shutil.which("notify-send"):
        cmd = ["notify-send", "-a", _agent_label(), "-u", urgency, "-t", str(timeout_ms), title] + ([body] if body else [])
    else:
        raise CUError("no notification tool found")
    run(cmd, timeout=10, check=False)


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
    try:
        p = subprocess.run(cmd, input=("\n".join(options or [])) if cmd[0].endswith("walker") else None,
                           capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return None
    ans = p.stdout.strip()
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
            except CUError:
                found = None
        else:
            hits = find_text(text)
            found = hits[0].__dict__ if hits else None
        if bool(found) != gone:
            return {"ok": True, "seconds": round(time.time() - t0, 1), "match": found}
        if time.time() - t0 > timeout:
            return {"ok": False, "seconds": round(time.time() - t0, 1), "match": None}
        time.sleep(interval)
