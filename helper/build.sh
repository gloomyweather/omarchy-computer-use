#!/usr/bin/env bash
# build hypr-pointer into $1 (default: ~/.cache/omarchy-cu/bin)
set -euo pipefail
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
out="${1:-${XDG_CACHE_HOME:-$HOME/.cache}/omarchy-cu/bin}"
mkdir -p "$out"
tmp="$(mktemp -d)"; trap 'rm -rf "$tmp"' EXIT
xml="$here/wlr-virtual-pointer-unstable-v1.xml"
wayland-scanner client-header "$xml" "$tmp/wlr-virtual-pointer-unstable-v1-client-protocol.h"
wayland-scanner private-code  "$xml" "$tmp/wlr-virtual-pointer-protocol.c"
cc -O2 -Wall -Wextra -I"$tmp" -o "$out/hypr-pointer.new" "$here/hypr-pointer.c" "$tmp/wlr-virtual-pointer-protocol.c" $(pkg-config --cflags --libs wayland-client)
mv "$out/hypr-pointer.new" "$out/hypr-pointer"
echo "$out/hypr-pointer"
