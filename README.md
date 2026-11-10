# omarchy-computer-use

Computer use for AI agents on [Omarchy](https://omarchy.org) / Hyprland: screenshots, mouse, keyboard,
OCR-based text targeting, window and workspace control.

- **No root, no `ydotool`/`uinput`**: the mouse is a native Wayland virtual pointer
  (`zwlr_virtual_pointer_v1`, tiny C helper built on first use), keyboard via `wtype`, screen via `grim`.
- **Zero Python dependencies**: stdlib MCP server + CLI. Runs on a stock Omarchy install.
- **Hyprland-native**: windows/workspaces/launch via the Hyprland IPC socket. Supports the new Lua
  dispatch syntax (Hyprland ≥ 0.55, current Omarchy) and the legacy one.
- **Model-friendly coordinates**: one stable "screen space" (screenshot pixels, 1280 px long edge by
  default) for every tool; HiDPI/fractional scaling and multi-monitor are mapped for you.
- **Omarchy-native**: ~400 `omarchy` system commands (themes, audio, display, toggles…), installed apps by
  name, native popups (`ask_user`) and notifications to talk to the user.
- **Semantic UI access**: AT-SPI accessibility tree (`ui_tree` / `ui_action`) for GTK/Qt/Chromium apps —
  exact elements and actions without pixel guessing; OCR (two-pass, dark-theme aware) for everything else.
- **"Claude is using your computer" indicator**: themed frame + pill while the agent acts, auto-hidden for
  every capture so it never pollutes screenshots. Click the pill or press `SUPER+CTRL+ALT+P` to pause.
- **Self-protection**: keyboard input into the agent's own terminal is refused (a stray `Escape` would
  interrupt the agent itself).

## Install

```bash
git clone <this repo> ~/.local/share/omarchy-computer-use
cd ~/.local/share/omarchy-computer-use
./install.sh --keybind            # add --codex to also register with Codex CLI
```

This installs missing packages (`grim wtype wl-clipboard tesseract base-devel …`), builds the pointer
helper, links `~/.local/bin/omarchy-cu`, and installs the Claude Code plugin (MCP server `computer` +
`computer-use` skill) from the bundled local marketplace.

Manual Claude Code install:

```bash
claude plugin marketplace add ~/.local/share/omarchy-computer-use
claude plugin install omarchy-computer-use@omarchy-computer-use
```

## Browser

For web pages, agents should prefer a DOM-level browser tool (Claude in Chrome, Playwright MCP, …) when one
is connected. The skill tells Claude to fall back to pixel-level computer use only without one, or for
things outside the page (OS file pickers, dialogs, other apps).

## Other agents

Any MCP client: command `…/bin/omarchy-cu`, args `["mcp"]` (stdio).

```toml
# Codex ~/.codex/config.toml
[mcp_servers.computer]
command = "/home/you/.local/share/omarchy-computer-use/bin/omarchy-cu"
args = ["mcp"]
```

```jsonc
// opencode.json
{ "mcp": { "computer": { "type": "local", "command": ["/home/you/.local/share/omarchy-computer-use/bin/omarchy-cu", "mcp"] } } }
```

Shell-only agents can use the CLI and read the saved PNGs:

```bash
omarchy-cu screenshot            # -> /tmp/omarchy-cu/shot-….png + window summary
omarchy-cu find "Sign in"        # OCR -> "x<TAB>y<TAB>text"
omarchy-cu click-text "Sign in"
omarchy-cu click 640 360 --double
omarchy-cu type "привет, world"; omarchy-cu key ctrl+Return
omarchy-cu launch "chromium https://example.com"
omarchy-cu --help
```

`skills/computer-use/SKILL.md` doubles as an `AGENTS.md` snippet for agents without skills.

## MCP tools

| tool | what |
|---|---|
| `screenshot` | focused monitor / all / one window, with cursor + text summary of windows |
| `zoom` | magnified crop for small text |
| `find_text`, `click_text` | tesseract OCR → exact coordinates / click |
| `click`, `move_mouse`, `drag`, `scroll`, `mouse_button` | pointer (buttons, double/triple, modifier-click) |
| `type_text`, `key` | Unicode typing (layout-independent), combos and sequences |
| `windows`, `focus_window`, `close_window`, `workspace`, `launch` | Hyprland control |
| `window` | float/tile/fullscreen/maximize, move to workspace, move/resize, swap, pin |
| `ui_tree`, `ui_action` | accessibility tree → click/focus/set_text by element id |
| `apps`, `launch app=…` | installed desktop apps, launched like the app launcher |
| `omarchy`, `omarchy_commands` | the Omarchy command center |
| `notify`, `ask_user` | talk to the user: notification, native pick-list / text popup |
| `wait_for` | wait for text (OCR) or a window to appear / disappear |
| `keybindings`, `clipboard`, `wait`, `hyprctl` | utilities |

Action tools return a fresh screenshot by default (`screenshot: false` to skip).

## Configuration (env)

| var | default | |
|---|---|---|
| `OMARCHY_CU_MAX_EDGE` | `1280` | long edge of screen space / screenshots |
| `OMARCHY_CU_AUTO_SCREENSHOT` | `1` | screenshot after actions |
| `OMARCHY_CU_SETTLE` | `0.35` | seconds to wait before that screenshot |
| `OMARCHY_CU_OCR_LANGS` | auto | e.g. `eng+rus` (install `tesseract-data-rus`) |
| `OMARCHY_CU_INDICATOR` | `1` | on-screen "agent is using your computer" indicator |
| `OMARCHY_CU_IDLE` | `20` | seconds of inactivity before the indicator hides |
| `OMARCHY_CU_AGENT_NAME` | auto | name in the pill (`Claude` under Claude Code) |
| `OMARCHY_CU_COLOR` | theme orange | indicator color |
| `OMARCHY_CU_ALLOW_SELF` | `0` | allow keyboard input into the agent's own terminal |

## Layout

```
.claude-plugin/   plugin.json, marketplace.json, icon
.mcp.json         MCP server registration (plugin)
skills/           computer-use skill
bin/omarchy-cu    launcher (CLI + `mcp` server)
src/omarchy_cu/   core.py (capture/input/hyprland/omarchy), a11y.py (AT-SPI), overlay.py (indicator),
                  server.py (MCP), cli.py
helper/           hypr-pointer.c (virtual pointer) + protocol XML + build.sh
```

## License

MIT
