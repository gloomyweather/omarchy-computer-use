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
# OCR languages: eng + rus by default; override with OMARCHY_CU_OCR_INSTALL="eng deu ..."
for l in ${OMARCHY_CU_OCR_INSTALL:-eng rus}; do
  [[ -e /usr/share/tessdata/$l.traineddata ]] || need+=("tesseract-data-$l")
done
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
  mp=omarchy-computer-use; id="omarchy-computer-use@$mp"
  # a marketplace registered from another checkout would keep serving the old copy: re-point it
  cur="$(claude plugin marketplace list --json 2>/dev/null | python3 -c '
import json, sys
try:
    for m in json.load(sys.stdin):
        if m.get("name") == sys.argv[1]:
            print(m.get("path") or (m.get("source") or {}).get("path") or "")
except Exception:
    pass' "$mp" || true)"
  if [[ -n $cur && $(readlink -f "$cur") != "$here" ]]; then
    say "marketplace $mp pointed at $cur; re-registering from $here"
    claude plugin marketplace remove "$mp" >/dev/null 2>&1 || true
  fi
  claude plugin marketplace add "$here" >/dev/null 2>&1 || claude plugin marketplace update "$mp" >/dev/null 2>&1 || true
  # `install` is a no-op when already installed: `update` picks up a newer version on re-runs
  claude plugin install "$id" >/dev/null 2>&1 || true
  claude plugin update "$id" >/dev/null 2>&1 || true
  say "restart Claude Code (or /mcp reconnect) to load the new version"
fi

# 5. Codex CLI
if ((codex)) && command -v codex >/dev/null; then
  say "registering MCP server with codex"
  codex mcp remove computer >/dev/null 2>&1 || true
  codex mcp add computer -- "$here/bin/omarchy-cu" mcp || echo "warning: codex mcp add failed" >&2
fi

# 6. kill-switch keybinding
if ((keybind)); then
  lua="$HOME/.config/hypr/bindings.lua" conf="$HOME/.config/hypr/bindings.conf"
  cmd="$HOME/.local/bin/omarchy-cu toggle"
  if grep -qs "omarchy-cu toggle" "$lua" "$conf"; then
    say "kill switch already bound"
  elif [[ -f $lua ]]; then
    cp "$lua" "$lua.bak.omarchy-cu"
    printf '\n-- Pause / resume agent computer use (omarchy-cu kill switch).\no.bind("SUPER + CTRL + ALT + P", "Pause agent computer use", "%s")\n' "$cmd" >> "$lua"
    hyprctl reload >/dev/null || true
    say "kill switch: SUPER+CTRL+ALT+P"
  elif [[ -f $conf ]]; then
    cp "$conf" "$conf.bak.omarchy-cu"
    printf '\n# Pause / resume agent computer use (omarchy-cu kill switch).\nbindd = SUPER CTRL ALT, P, Pause agent computer use, exec, %s\n' "$cmd" >> "$conf"
    hyprctl reload >/dev/null || true
    say "kill switch: SUPER+CTRL+ALT+P"
  else
    echo "warning: no ~/.config/hypr/bindings.lua or bindings.conf; bind \`$cmd\` to a key yourself" >&2
  fi
fi
say "done. in Claude Code: /mcp should list 'computer'; or use the CLI: omarchy-cu --help"
