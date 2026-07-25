#!/usr/bin/env bash
# Runs inside a herdr overlay pane, which is the only place a plugin gets a real
# terminal — herdr runs action commands as background subprocesses with captured
# output, so an editor launched there has nothing to draw on.
set -euo pipefail

config_dir="${HERDR_PLUGIN_CONFIG_DIR:-$HOME/.config/herdr/plugins/config/chrismalone.agent-smart-rename}"
target="$config_dir/provider.env"

mkdir -p "$config_dir"
if [ ! -f "$target" ]; then
  cp "${HERDR_PLUGIN_ROOT:-$(dirname "$0")}/provider.env.example" "$target"
  chmod 600 "$target"
fi

exec ${VISUAL:-${EDITOR:-vi}} "$target"
