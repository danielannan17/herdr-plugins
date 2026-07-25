#!/usr/bin/env bash
# Opens the provider.env editor as an overlay pane. Invoked by the configure-ai
# action, which cannot host an interactive editor itself.
set -euo pipefail

exec "${HERDR_BIN_PATH:-herdr}" plugin pane open \
  --plugin "$HERDR_PLUGIN_ID" --entrypoint editor --placement overlay
