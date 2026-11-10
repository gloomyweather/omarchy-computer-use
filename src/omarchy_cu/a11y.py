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
    """The app's top-level frame that corresponds to this Hyprland window (by title, then active)."""
    title = win.get("title") or ""
    frames = []
    for i in range(app.get_child_count()):
        try:
            f = app.get_child_at_index(i)
            if f is not None:
                frames.append(f)
        except Exception:
            pass
    for f in frames:
        try:
            if title and (f.get_name() or "") == title:
                return f
        except Exception:
            pass
    for f in frames:
        try:
            if f.get_state_set().contains(Atspi.StateType.ACTIVE):
                return f
        except Exception:
            pass
    return frames[0] if frames else None


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
        return {"window": win, "elements": [], "note": "app exposes no accessibility tree (use OCR / screenshot)"}
    frame = _frame_for_window(Atspi, app, win)
    if frame is None:
        return {"window": win, "elements": [], "note": "no accessible frame"}
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
            item = {"id": f"e{len(out) + 1}", "path": ".".join(map(str, path)), "role": role, "name": name[:120]}
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
                    ti = node.get_text_iface()
                    if ti is not None and role != "password text":
                        item["value"] = (ti.get_text(0, min(ti.get_character_count(), 200)) or "")
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
    _save_ids(win["address"], {e["id"]: e.pop("path") for e in out})
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


def _path_for(eid: str, address: str) -> str:
    import json
    if not eid.startswith("e"):
        return eid  # raw child-index path
    try:
        data = json.loads(IDS_FILE.read_text())
    except (OSError, ValueError):
        raise CUError("no ui_tree cached; call ui_tree first")
    if data.get("address") != address:
        raise CUError("element ids belong to another window; call ui_tree for this window")
    path = data["ids"].get(eid)
    if not path:
        raise CUError(f"unknown element {eid}; call ui_tree again")
    return path


def _node_by_id(Atspi, win: dict, eid: str):
    eid = _path_for(eid, win["address"])
    app = _app_for_window(Atspi, win)
    if app is None:
        raise CUError("app exposes no accessibility tree")
    node = app
    try:
        for part in eid.split("."):
            node = node.get_child_at_index(int(part))
            if node is None:
                raise CUError(f"element {eid} no longer exists (UI changed); call ui_tree again")
    except ValueError:
        raise CUError(f"bad element id {eid!r}")
    return node


def act(eid: str, action: str = "click", text: str | None = None, window: str | None = None) -> str:
    if window is None:
        import json
        try:  # default to the window the ids came from
            window = json.loads(IDS_FILE.read_text()).get("address")
        except (OSError, ValueError):
            window = None
    """Perform an action on an element from `tree`: click (default action), focus, set_text,
    or any named action the element lists (e.g. 'activate', 'toggle', 'expand or contract')."""
    core.check_not_paused()
    core.mark_activity()
    Atspi = _atspi()
    win = _resolve_window(window)
    node = _node_by_id(Atspi, win, eid)
    name = node.get_name() or node.get_role_name()
    if action == "focus":
        comp = node.get_component_iface()
        if comp is None or not comp.grab_focus():
            raise CUError("element can't take focus")
        return f"focused {name!r}"
    if action == "set_text":
        et = node.get_editable_text_iface()
        if et is None:
            raise CUError("element is not editable text")
        if not et.set_text_contents(text or ""):
            raise CUError("app refused set_text")
        return f"set text of {name!r}"
    ai = node.get_action_iface()
    if ai is None or ai.get_n_actions() == 0:
        raise CUError(f"{name!r} has no actions; click its center coordinates instead")
    names = [ai.get_action_name(k) for k in range(ai.get_n_actions())]
    if action == "click":
        pref = ("click", "press", "activate", "toggle", "jump", "open")
        idx = next((names.index(p) for p in pref if p in names), 0)
    elif action in names:
        idx = names.index(action)
    else:
        raise CUError(f"{name!r} has actions {names}, not {action!r}")
    if not ai.do_action(idx):
        raise CUError(f"action {names[idx]!r} failed")
    return f"{names[idx]} on {name!r}"
