#!/usr/bin/env bash
# omarchy-computer-use installer
#   ./install.sh                 deps + pointer helper + `omarchy-cu` CLI + Claude Code plugin
#   ./install.sh --keybind       also bind SUPER+CTRL+ALT+P to pause/resume agent input
#   ./install.sh --codex         also register the MCP server with Codex CLI
#   ./install.sh --no-claude     skip the Claude Code plugin
set -euo pipefail
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
keybind=0; codex=0; claude=1
for a in "$@"; do case "$a" in
  --keybind) keybind=1 ;; --codex) codex=1 ;; --no-claude) claude=0 ;;
  -h|--help) sed -n 2,7p "$0"; exit 0 ;; *) echo "unknown option $a" >&2; exit 2 ;; esac; done

say() { printf '\033[1m==>\033[0m %s\n' "$*"; }

# 1. dependencies (all in Arch repos; most ship with Omarchy)
need=()
for pair in grim:grim wtype:wtype wl-copy:wl-clipboard tesseract:tesseract hyprctl:hyprland \
            cc:base-devel wayland-scanner:wayland pkg-config:pkgconf python3:python; do
  command -v "${pair%%:*}" >/dev/null || need+=("${pair#*:}")
done
[[ -e /usr/share/tessdata/eng.traineddata ]] || need+=(tesseract-data-eng)
if ((${#need[@]})); then
  say "installing: ${need[*]}"
  sudo pacman -S --needed --noconfirm "${need[@]}"
fi

# 2. pointer helper
say "building pointer helper"
"$here/helper/build.sh" >/dev/null

# 3. CLI
mkdir -p "$HOME/.local/bin"
ln -sf "$here/bin/omarchy-cu" "$HOME/.local/bin/omarchy-cu"
say "cli: ~/.local/bin/omarchy-cu"
"$here/bin/omarchy-cu" doctor || true

# 4. Claude Code plugin (local marketplace -> plugin)
if ((claude)) && command -v claude >/dev/null; then
  say "installing Claude Code plugin"
  claude plugin marketplace add "$here" 2>/dev/null || claude plugin marketplace update omarchy-computer-use || true
  claude plugin install omarchy-computer-use@omarchy-computer-use || true
fi

# 5. Codex CLI
if ((codex)) && command -v codex >/dev/null; then
  say "registering MCP server with codex"
  codex mcp remove computer >/dev/null 2>&1 || true
  codex mcp add computer -- "$here/bin/omarchy-cu" mcp
fi

# 6. kill-switch keybinding
if ((keybind)); then
  f="$HOME/.config/hypr/bindings.lua"
  if [[ -f "$f" ]] && ! grep -q "omarchy-cu toggle" "$f"; then
    cp "$f" "$f.bak.omarchy-cu"
    printf '\n-- Pause / resume agent computer use (omarchy-cu kill switch).\no.bind("SUPER + CTRL + ALT + P", "Pause agent computer use", "%s toggle")\n' \
      "$HOME/.local/bin/omarchy-cu" >> "$f"
    hyprctl reload >/dev/null || true
    say "kill switch: SUPER+CTRL+ALT+P"
  fi
fi
say "done. in Claude Code: /mcp should list 'computer'; or use the CLI: omarchy-cu --help"
