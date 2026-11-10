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
- **Kill switch**: `omarchy-cu pause` (optionally bound to `SUPER+CTRL+ALT+P`) blocks all agent input.

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
| `keybindings`, `clipboard`, `wait`, `hyprctl` | utilities |

Action tools return a fresh screenshot by default (`screenshot: false` to skip).

## Configuration (env)

| var | default | |
|---|---|---|
| `OMARCHY_CU_MAX_EDGE` | `1280` | long edge of screen space / screenshots |
| `OMARCHY_CU_AUTO_SCREENSHOT` | `1` | screenshot after actions |
| `OMARCHY_CU_SETTLE` | `0.35` | seconds to wait before that screenshot |
| `OMARCHY_CU_OCR_LANGS` | auto | e.g. `eng+rus` (install `tesseract-data-rus`) |

## Layout

```
.claude-plugin/   plugin.json, marketplace.json, icon
.mcp.json         MCP server registration (plugin)
skills/           computer-use skill
bin/omarchy-cu    launcher (CLI + `mcp` server)
src/omarchy_cu/   core.py (capture/input/hyprland), server.py (MCP), cli.py
helper/           hypr-pointer.c (virtual pointer) + protocol XML + build.sh
```

## License

MIT
