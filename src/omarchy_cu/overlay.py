"""On-screen indicator: "<Agent> is using your computer".

A click-through GTK4 layer-shell overlay (one per monitor) that draws a thin frame around the
screen plus a small pill with the agent's name. It is shown while the agent was active in the last
OMARCHY_CU_IDLE seconds, turns red while input is paused, and hides itself for a moment whenever
omarchy-cu captures the screen (SIGUSR1 = hide, SIGUSR2 = show) so it never appears in screenshots.
Clicking the pill toggles pause (kill switch).

Started on demand by omarchy-cu (core.ensure_overlay); exits after a long idle period.
Needs: python-gobject, gtk4, gtk4-layer-shell (all part of a stock Omarchy install).
"""
from __future__ import annotations

import fcntl
import os
import signal
import sys
import time
from pathlib import Path

CACHE = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "omarchy-cu"
ACTIVITY = CACHE / "activity"
PAUSE = CACHE / "paused"
PIDFILE = CACHE / "overlay.pid"
HIDE_HOLD = float(os.environ.get("OMARCHY_CU_HIDE_HOLD", "1.5"))
IDLE = float(os.environ.get("OMARCHY_CU_IDLE", "20"))
EXIT_AFTER = float(os.environ.get("OMARCHY_CU_OVERLAY_EXIT", "900"))


def _theme_colors() -> tuple[str, str, str]:
    """(accent, background, foreground) from the current Omarchy theme, Claude-ish fallback."""
    accent, bg, fg = "#d97757", "#1a1a1a", "#f2f2f2"
    env = os.environ.get("OMARCHY_CU_COLOR")
    f = Path.home() / ".local/state/omarchy/current/theme/colors.toml"
    try:
        vals = {}
        for line in f.read_text().splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                k, v = line.split("=", 1)
                v = v.strip()
                if v.startswith('"') and '"' in v[1:]:
                    vals[k.strip()] = v[1:v.index('"', 1)]
        accent = vals.get("orange") or accent
        bg = vals.get("background") or bg
        fg = vals.get("foreground") or fg
    except OSError:
        pass
    return env or accent, bg, fg


def _agent_name() -> str:
    n = os.environ.get("OMARCHY_CU_AGENT_NAME")
    if n:
        return n
    if os.environ.get("CLAUDECODE") or os.environ.get("CLAUDE_CODE_ENTRYPOINT"):
        return "Claude"
    if any(k.startswith("CODEX") for k in os.environ):
        return "Codex"
    return "AI agent"


def _kill_binding() -> str:
    """Human-readable combo of the user's kill-switch keybinding, if one is installed."""
    import json
    import subprocess
    try:
        binds = json.loads(subprocess.run(["hyprctl", "binds", "-j"], capture_output=True, text=True, timeout=3).stdout)
    except Exception:
        return ""
    for b in binds:
        if "omarchy-cu" in str(b.get("arg", "")) or "agent computer use" in str(b.get("description", "")).lower():
            mods = [n for bit, n in ((64, "Super"), (4, "Ctrl"), (8, "Alt"), (1, "Shift")) if b.get("modmask", 0) & bit]
            return "+".join(mods + [str(b.get("key", "")).upper()])
    return ""


def _no_anim_rule() -> None:
    """Hyprland fades layers in/out; disable that for our namespaces so hiding for a
    screenshot is instant. Runtime-only rule (re-applied on every overlay start)."""
    import subprocess
    for cmd in (["hyprctl", "eval", 'hl.layer_rule({ match = { namespace = "^omarchy-cu" }, no_anim = true, animation = "none" })'],
                ["hyprctl", "keyword", "layerrule", "noanim,^omarchy-cu"]):
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=3)
            if r.stdout.strip() == "ok":
                return
        except Exception:
            pass


def main() -> int:
    CACHE.mkdir(parents=True, exist_ok=True)
    lock = open(CACHE / "overlay.lock", "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return 0  # already running
    PIDFILE.write_text(str(os.getpid()))

    import gi
    gi.require_version("Gtk", "4.0")
    gi.require_version("Gdk", "4.0")
    gi.require_version("Gtk4LayerShell", "1.0")
    from gi.repository import Gdk, GLib, Gtk  # noqa: E402
    from gi.repository import Gtk4LayerShell as LS  # noqa: E402

    accent, bg, fg = _theme_colors()
    name = _agent_name()
    kb = os.environ.get("OMARCHY_CU_KILL_HINT") or _kill_binding()
    css = f"""
    window.cu-frame {{ background: transparent; }}
    .cu-border {{ border: 3px solid {accent}; border-radius: 10px; background: transparent; }}
    .cu-border.paused {{ border-color: #e5484d; }}
    .cu-pill {{ background: alpha({bg}, 0.92); color: {fg}; border: 1.5px solid {accent};
               border-radius: 999px; padding: 3px 14px; font-weight: 600; font-size: 12px; }}
    .cu-pill.paused {{ border-color: #e5484d; }}
    .cu-dot {{ color: {accent}; font-size: 13px; }}
    .cu-pill.paused .cu-dot {{ color: #e5484d; }}
    .cu-hint {{ color: alpha({fg}, 0.6); font-weight: 400; }}
    """

    _no_anim_rule()
    app = Gtk.Application(application_id=None)
    state = {"wins": [], "hidden_for_capture": False, "visible": False, "paused": None, "last_seen": time.time(), "active": False}

    def build(app_):
        prov = Gtk.CssProvider()
        prov.load_from_string(css) if hasattr(prov, "load_from_string") else prov.load_from_data(css.encode())
        disp = Gdk.Display.get_default()
        Gtk.StyleContext.add_provider_for_display(disp, prov, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
        mons = disp.get_monitors()
        for i in range(mons.get_n_items()):
            mon = mons.get_item(i)
            # frame: full-screen, click-through
            w = Gtk.Window(application=app_)
            w.add_css_class("cu-frame")
            LS.init_for_window(w)
            LS.set_namespace(w, "omarchy-cu-frame")
            LS.set_layer(w, LS.Layer.OVERLAY)
            LS.set_monitor(w, mon)
            for e in (LS.Edge.TOP, LS.Edge.BOTTOM, LS.Edge.LEFT, LS.Edge.RIGHT):
                LS.set_anchor(w, e, True)
            LS.set_exclusive_zone(w, -1)
            LS.set_keyboard_mode(w, LS.KeyboardMode.NONE)
            border = Gtk.Box()
            border.add_css_class("cu-border")
            border.set_can_target(False)
            w.set_child(border)
            w.set_can_target(False)

            def on_realize(win):
                surf = win.get_surface()
                if surf is not None:
                    try:
                        import cairo
                        surf.set_input_region(cairo.Region())
                    except Exception:
                        pass
            w.connect("realize", on_realize)
            w.connect("map", on_realize)

            # pill: small, clickable (toggles pause)
            p = Gtk.Window(application=app_)
            p.add_css_class("cu-frame")
            LS.init_for_window(p)
            LS.set_namespace(p, "omarchy-cu-pill")
            LS.set_layer(p, LS.Layer.OVERLAY)
            LS.set_monitor(p, mon)
            LS.set_anchor(p, LS.Edge.TOP, True)
            LS.set_margin(p, LS.Edge.TOP, 8)
            LS.set_exclusive_zone(p, -1)
            LS.set_keyboard_mode(p, LS.KeyboardMode.NONE)
            box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=7)
            box.add_css_class("cu-pill")
            dot = Gtk.Label(label="●"); dot.add_css_class("cu-dot")
            text = Gtk.Label(label=f"{name} is using your computer")
            hint = Gtk.Label(label=f"· click or {kb} to pause" if kb else "· click to pause"); hint.add_css_class("cu-hint")
            for c in (dot, text, hint):
                box.append(c)
            p.set_child(box)
            click = Gtk.GestureClick()

            def on_click(*_a):
                if PAUSE.exists():
                    PAUSE.unlink(missing_ok=True)
                else:
                    PAUSE.touch()
                tick()
            click.connect("released", on_click)
            box.add_controller(click)
            state["wins"].append((w, border, p, box, text, hint))
        tick()
        GLib.timeout_add(100, tick)

    def set_visible(v: bool):
        for w, _b, p, *_ in state["wins"]:
            w.set_visible(v)
            p.set_visible(v)
        state["visible"] = v

    def tick(*_a):
        now = time.time()
        if state["hidden_for_capture"] and now >= state.get("hide_until", 0):
            state["hidden_for_capture"] = False
            set_capture_hidden(False)
        try:
            last = ACTIVITY.stat().st_mtime
        except OSError:
            last = 0
        paused = PAUSE.exists()
        active = (now - last) < IDLE or paused and (now - last) < IDLE * 3
        if paused != state["paused"]:
            state["paused"] = paused
            for _w, border, _p, box, text, hint in state["wins"]:
                for wdg in (border, box):
                    (wdg.add_css_class if paused else wdg.remove_css_class)("paused")
                text.set_label(f"{name} input paused" if paused else f"{name} is using your computer")
                hint.set_label("· click to resume" if paused else (f"· click or {kb} to pause" if kb else "· click to pause"))
        state["active"] = active
        want = active and not state["hidden_for_capture"]
        if want != state["visible"]:
            set_visible(want)
        if active:
            state["last_seen"] = now
        elif now - state["last_seen"] > EXIT_AFTER:
            app.quit()
        return True

    def set_capture_hidden(h: bool):
        # Unmap the layer surfaces (Hyprland fades them out in ~0.2 s; the capturer waits for that).
        # Changing their content instead is not reliably re-presented by GTK4 + layer-shell.
        set_visible(state["active"] and not h)

    def on_hide(*_a):
        """SIGUSR1 from a capturer: hide now and stay hidden until HIDE_HOLD seconds after the
        last capture request, so a burst of screenshots doesn't make the frame flicker."""
        state["hide_until"] = time.time() + HIDE_HOLD
        if not state["hidden_for_capture"]:
            state["hidden_for_capture"] = True
            set_capture_hidden(True)
        return True

    def on_show(*_a):
        state["hide_until"] = 0
        return True

    try:
        gi.require_version("GLibUnix", "2.0")
        from gi.repository import GLibUnix
        sigadd = GLibUnix.signal_add
    except (ImportError, ValueError):
        sigadd = GLib.unix_signal_add
    sigadd(GLib.PRIORITY_HIGH, signal.SIGUSR1, on_hide)
    sigadd(GLib.PRIORITY_HIGH, signal.SIGUSR2, on_show)
    sigadd(GLib.PRIORITY_HIGH, signal.SIGTERM, lambda *_: (app.quit(), True)[1])
    app.connect("activate", build)
    app.hold()
    try:
        app.run([])
    finally:
        try:
            if PIDFILE.read_text().strip() == str(os.getpid()):
                PIDFILE.unlink()
        except OSError:
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
