#!/bin/sh
set -eu

root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
receipt=${AGENTSEAT_TEST_RECEIPT:?AGENTSEAT_TEST_RECEIPT is required}
document=${AGENTSEAT_TEST_DOCUMENT:-/tmp/agentseat-embedded-hud.txt}
fixture_state=${AGENTSEAT_TEST_STATE:-/tmp/agentseat-embedded-hud-state}

umask 077
mkdir -p "$fixture_state/config" "$fixture_state/cache" "$fixture_state/data" "$fixture_state/state"
export XDG_CONFIG_HOME="$fixture_state/config"
export XDG_CACHE_HOME="$fixture_state/cache"
export XDG_DATA_HOME="$fixture_state/data"
export XDG_STATE_HOME="$fixture_state/state"
printf '{"wayland_display":"%s"}\n' "$WAYLAND_DISPLAY" >"$receipt"

/usr/bin/mousepad --disable-server "$document" &
app_pid=$!
eye_pid=

cleanup() {
    kill "$app_pid" "$eye_pid" 2>/dev/null || true
}
trap cleanup EXIT INT TERM

sleep 0.5
"$root/bin/agentseat-eye" --embedded --socket /tmp/agentseat-no-daemon.sock &
eye_pid=$!

wait "$app_pid"
