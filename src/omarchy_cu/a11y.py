"""Accessibility (AT-SPI) access: semantic UI tree + actions without pixel guessing.

Works for GTK3/GTK4 (Nautilus, GNOME apps, most GTK tools), Qt apps started with
QT_LINUX_ACCESSIBILITY_ALWAYS_ON=1, and Chromium/Electron started with
--force-renderer-accessibility. Apps that expose nothing return an empty tree: use OCR then.

On Wayland AT-SPI cannot report absolute screen positions, so element boxes are computed from
window-relative extents + the Hyprland window position.
"""
from __future__ import annotations

from . import core
from .core import CUError

INTERACTIVE = {
    "push button", "toggle button", "check box", "radio button", "menu item", "check menu item",
    "radio menu item", "combo box", "text", "entry", "password text", "spin button", "slider",
    "link", "page tab", "list item", "table cell", "tree item", "menu", "switch", "button",
    "search box", "terminal", "scroll bar", "icon", "tool bar button",
}
MAX_NODES = 4000


def _atspi():
    try:
        import gi
        gi.require_version("Atspi", "2.0")
        from gi.repository import Atspi
    except (ImportError, ValueError) as e:
        raise CUError(f"AT-SPI unavailable ({e}); install python-gobject at-spi2-core")
    try:
        Atspi.set_timeout(800, 3000)
    except Exception:
        pass
    return Atspi


def _app_for_window(Atspi, win: dict):
    desk = Atspi.get_desktop(0)
    pid = win.get("pid")
    cls = (win.get("class") or "").lower()
    best = None
    for i in range(desk.get_child_count()):
        try:
            app = desk.get_child_at_index(i)
            if app is None:
                continue
            if pid and app.get_process_id() == pid:
                return app
            name = (app.get_name() or "").lower()
            if cls and name and (name in cls or cls.split(".")[-1] in name):
                best = best or app
        except Exception:
            continue
    return best


def _frame_for_window(Atspi, app, win: dict):
    """The app's top-level frame that corresponds to this Hyprland window: exact title, then a title
    containment match, then the only frame, then the ACTIVE frame (only valid if this window is the
    focused one). None if ambiguous: acting on another window's frame would be wrong."""
    title = (win.get("title") or "").strip()
    frames = []
    for i in range(app.get_child_count()):
        try:
            f = app.get_child_at_index(i)
            if f is not None:
                frames.append(f)
        except Exception:
            pass

    def name(f):
        try:
            return (f.get_name() or "").strip()
        except Exception:
            return ""
    def active(cands):
        if not win.get("focused"):
            return None
        for f in cands:
            try:
                if f.get_state_set().contains(Atspi.StateType.ACTIVE):
                    return f
            except Exception:
                pass
        return None
    if len(frames) == 1:
        return frames[0]
    if title:
        for cands in ([f for f in frames if name(f) == title],
                      [f for f in frames if name(f) and (name(f) in title or title in name(f))]):
            if len(cands) == 1:
                return cands[0]
            if len(cands) > 1:  # several windows with the same title: only the focused one is knowable
                return active(cands)
    return active(frames)


def _states(Atspi, node) -> list[str]:
    ss = node.get_state_set()
    out = []
    for name in ("FOCUSED", "CHECKED", "SELECTED", "PRESSED", "EXPANDED", "EDITABLE"):
        if ss.contains(getattr(Atspi.StateType, name)):
            out.append(name.lower())
    if not ss.contains(Atspi.StateType.SENSITIVE):
        out.append("disabled")
    return out


def _resolve_window(window: str | None) -> dict:
    if window:
        return core.find_window(window)
    for w in core.windows():
        if w["focused"]:
            return w
    raise CUError("no focused window")


def tree(window: str | None = None, query: str = "", interactive_only: bool = True, limit: int = 250) -> dict:
    """Flattened list of visible elements of a window: id (child-index path), role, name, states,
    actions, box + center in screen space."""
    Atspi = _atspi()
    win = _resolve_window(window)
    app = _app_for_window(Atspi, win)
    if app is None:
        return {"window": {k: win[k] for k in ("address", "class", "title", "box")}, "elements": [],
                "note": "app exposes no accessibility tree (use OCR / screenshot)"}
    frame = _frame_for_window(Atspi, app, win)
    if frame is None:
        return {"window": {k: win[k] for k in ("address", "class", "title", "box")}, "elements": [],
                "note": "can't tell which of the app's windows this is; focus it (focus_window) and retry"}
    S = core.scale_factor()
    wx, wy = round(win["box"][0] / S), round(win["box"][1] / S)  # logical window origin
    q = query.lower().strip()
    out: list[dict] = []
    seen = [0]
    fi = None
    for i in range(app.get_child_count()):
        if app.get_child_at_index(i) == frame:
            fi = i
            break

    def walk(node, path: list[int], depth: int):
        if seen[0] > MAX_NODES or len(out) >= limit or depth > 60:
            return
        seen[0] += 1
        try:
            ss = node.get_state_set()
            if not ss.contains(Atspi.StateType.SHOWING):
                return
            role = node.get_role_name() or ""
            name = (node.get_name() or "").strip()
            n_children = node.get_child_count()
        except Exception:
            return
        keep = (role in INTERACTIVE) if interactive_only else bool(name or role in INTERACTIVE)
        if keep and q and q not in name.lower() and q not in role:
            keep = False
        if keep:
            box = None
            try:
                comp = node.get_component_iface()
                if comp is not None:
                    e = comp.get_extents(Atspi.CoordType.WINDOW)
                    if e.width > 0 and e.height > 0:
                        x0, y0 = core.to_screen(wx + e.x, wy + e.y, S)
                        box = [x0, y0, round(e.width * S), round(e.height * S)]
            except Exception:
                pass
            actions = []
            try:
                ai = node.get_action_iface()
                if ai is not None:
                    actions = [ai.get_action_name(k) for k in range(ai.get_n_actions())]
            except Exception:
                pass
            item = {"id": f"e{len(out) + 1}", "path": ".".join(map(str, path)), "role": role, "name": name[:120],
                    "_sig": [role, name]}
            st = _states(Atspi, node)
            if st:
                item["states"] = st
            if actions:
                item["actions"] = actions
            if box:
                item["box"] = box
                item["center"] = [box[0] + box[2] // 2, box[1] + box[3] // 2]
            try:
                if node.get_editable_text_iface() is not None or role in ("text", "entry", "password text"):
                    # call through Atspi.Text explicitly: on the iface object `.get_text` resolves to the
                    # deprecated 1-arg Atspi.Accessible.get_text and raises, so the value came back empty
                    if node.get_text_iface() is not None and role != "password text":
                        n = min(Atspi.Text.get_character_count(node), 200)
                        item["value"] = Atspi.Text.get_text(node, 0, n) or ""
            except Exception:
                pass
            out.append(item)
        for k in range(min(n_children, 500)):
            try:
                ch = node.get_child_at_index(k)
            except Exception:
                continue
            if ch is not None:
                walk(ch, path + [k], depth + 1)

    walk(frame, [fi if fi is not None else 0], 0)
    _save_ids(win["address"], {e["id"]: [e.pop("path"), *e.pop("_sig")] for e in out})
    res = {"window": {k: win[k] for k in ("address", "class", "title", "box")}, "elements": out}
    if len(out) >= limit:
        res["note"] = f"truncated at {limit} elements; pass query= to narrow"
    if not out:
        res["note"] = "no visible elements exposed (app may not support accessibility; use OCR)"
    return res


IDS_FILE = core.CACHE / "ui-ids.json"


def _save_ids(address: str, mapping: dict) -> None:
    import json
    try:
        core.CACHE.mkdir(parents=True, exist_ok=True)
        IDS_FILE.write_text(json.dumps({"address": address, "ids": mapping}))
    except OSError:
        pass


def _path_for(eid: str, address: str) -> tuple[str, list | None]:
    """(child-index path, [role, name] recorded by ui_tree or None for a raw path id)."""
    import json
    if not eid.startswith("e"):
        return eid, None  # raw child-index path
    try:
        data = json.loads(IDS_FILE.read_text())
    except (OSError, ValueError):
        raise CUError("no ui_tree cached; call ui_tree first")
    if data.get("address") != address:
        raise CUError("element ids belong to another window; call ui_tree for this window")
    rec = data["ids"].get(eid)
    if not rec:
        raise CUError(f"unknown element {eid}; call ui_tree again")
    if isinstance(rec, str):
        return rec, None
    return rec[0], rec[1:]


def _node_by_id(Atspi, win: dict, eid: str):
    path, sig = _path_for(eid, win["address"])
    app = _app_for_window(Atspi, win)
    if app is None:
        raise CUError("app exposes no accessibility tree")
    node = app
    try:
        for part in path.split("."):
            node = node.get_child_at_index(int(part))
            if node is None:
                raise CUError(f"element {eid} no longer exists (UI changed); call ui_tree again")
    except ValueError:
        raise CUError(f"bad element id {eid!r}")
    if sig is not None:
        # the path is only a position; make sure the same element still sits there
        try:
            now = [node.get_role_name() or "", (node.get_name() or "").strip()]
        except Exception:
            now = None
        if now != sig:
            raise CUError(f"element {eid} changed since ui_tree (was {sig[0]} {sig[1]!r}, now "
                          f"{(now or ['?', '?'])[0]} {(now or ['?', '?'])[1]!r}); call ui_tree again")
    return node


def _click_center(Atspi, node, win: dict, name: str) -> str:
    """Fallback for elements without AT-SPI actions (e.g. GTK4 sidebar rows): real pointer click
    at the element's center, after making sure its window is the visible, focused one."""
    comp = node.get_component_iface()
    e = comp.get_extents(Atspi.CoordType.WINDOW) if comp is not None else None
    if e is None or e.width <= 0 or e.height <= 0:
        raise CUError(f"{name!r} has no actions and no on-screen box")
    if not win.get("visible") or not win.get("focused"):
        core.focus_window(win["address"])
        win = core.find_window(win["address"])
    S = core.scale_factor()
    wx, wy = win["box"][0] / S, win["box"][1] / S
    cx, cy = core.to_screen(wx + e.x + e.width / 2, wy + e.y + e.height / 2, S)
    core.click(cx, cy)
    return f"clicked {name!r} at ({cx}, {cy}) (no AT-SPI action; used the pointer)"


EDITABLE_ROLES = {"text", "entry", "password text", "search box", "spin button", "terminal", "editbar"}


def _grab_focus(node) -> bool:
    try:
        comp = node.get_component_iface()
        return bool(comp is not None and comp.grab_focus())
    except Exception:  # GTK4 answers with an AT-SPI error instead of False
        return False


def act(eid: str, action: str = "click", text: str | None = None, window: str | None = None) -> str:
    """Perform an action on an element from `tree`: click (default action), focus, set_text,
    or any named action the element lists (e.g. 'activate', 'toggle', 'expand or contract')."""
    if window is None:
        import json
        try:  # default to the window the ids came from
            window = json.loads(IDS_FILE.read_text()).get("address")
        except (OSError, ValueError):
            window = None
    action = (action or "click").strip()
    if action == "set_text" and text is None:
        raise CUError("set_text needs text (use text=\"\" to clear the field)")
    core.check_not_paused()
    core.mark_activity()
    Atspi = _atspi()
    win = _resolve_window(window)
    node = _node_by_id(Atspi, win, eid)
    name = node.get_name() or node.get_role_name()
    role = node.get_role_name() or ""
    editable = role in EDITABLE_ROLES or node.get_editable_text_iface() is not None
    if action == "focus":
        if _grab_focus(node):
            return f"focused {name!r}"
        if editable:  # clicking a text field only focuses it
            return _click_center(Atspi, node, win, name) + " to focus it"
        raise CUError(f"{name!r} can't take focus via AT-SPI (clicking it would activate it; use action=click "
                      "if that's intended)")
    if action == "set_text":
        et = node.get_editable_text_iface()
        if et is None:
            raise CUError("element is not editable text")
        if not et.set_text_contents(text):
            raise CUError("app refused set_text")
        return f"set text of {name!r}"
    if action == "click" and editable:
        # an entry's default action is 'activate' = Enter (would submit the form): focus it instead,
        # or click it with the pointer when it can't take focus
        if _grab_focus(node):
            return f"focused {name!r} ({role}; type_text to enter text, or ui_action set_text)"
        return _click_center(Atspi, node, win, name) + " (text field: focused, not activated)"
    ai = node.get_action_iface()
    if ai is None or ai.get_n_actions() == 0:
        if action != "click":
            raise CUError(f"{name!r} exposes no actions")
        return _click_center(Atspi, node, win, name)
    names = [ai.get_action_name(k) for k in range(ai.get_n_actions())]
    low = [n.lower().replace(" ", "") for n in names]
    if action == "click":
        pref = ("click", "press", "activate", "toggle", "jump", "open")
        idx = next((low.index(p) for p in pref if p in low), 0)
    elif action.lower().replace(" ", "") in low:
        idx = low.index(action.lower().replace(" ", ""))
    else:
        raise CUError(f"{name!r} has actions {names}, not {action!r}")
    if not ai.do_action(idx):
        raise CUError(f"action {names[idx]!r} failed")
    return f"{names[idx]} on {name!r}"
