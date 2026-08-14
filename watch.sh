#!/bin/sh
# Poll one pane for the life of its agent, because herdr fires only at startup.

set -eu

POLL_SECONDS="${SMART_RENAME_POLL_SECONDS:-60}"

cd "$(dirname "$0")"

pane_id=$(printf '%s' "${HERDR_PLUGIN_EVENT_JSON:-}" | sed -n 's/.*"pane_id":"\([^"]*\)".*/\1/p')
[ -n "$pane_id" ] || pane_id="${HERDR_PANE_ID:-}"
[ -n "$pane_id" ] || exit 0

herdr_bin="${HERDR_BIN_PATH:-herdr}"
state_dir="${HERDR_PLUGIN_STATE_DIR:-/tmp/agent-smart-rename}"
lock="$state_dir/watchers/$(printf '%s' "$pane_id" | tr ':' '-').lock"

mkdir -p "$state_dir/watchers"
# mkdir is atomic, so repeat detections cannot stack watchers on one pane.
if ! mkdir "$lock" 2>/dev/null; then
    # A pane id is reused after a restart, so drop a lock whose owner is gone.
    owner=$(cat "$lock/pid" 2>/dev/null || true)
    if [ -z "$owner" ] || kill -0 "$owner" 2>/dev/null; then
        exit 0
    fi
    rm -rf "$lock"
    mkdir "$lock" 2>/dev/null || exit 0
fi
echo "$$" >"$lock/pid"
trap 'rm -rf "$lock"' EXIT HUP INT TERM

while sleep "$POLL_SECONDS"; do
    "$herdr_bin" agent get "$pane_id" >/dev/null 2>&1 || exit 0
    env -u HERDR_PLUGIN_EVENT -u HERDR_PLUGIN_EVENT_JSON -u HERDR_PLUGIN_CONTEXT_JSON \
        HERDR_PANE_ID="$pane_id" uv run --script rename.py || true
done
