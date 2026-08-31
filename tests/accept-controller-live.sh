#!/bin/sh
set -eu

root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
agentseat="$root/bin/agentseat"
evidence="$root/evidence/generic-controller-observe.png"

before=$($agentseat status)
version=$($agentseat version)
windows=$($agentseat windows)
observe=$($agentseat observe "$evidence")
after=$($agentseat status)

before_focus=$(printf '%s' "$before" | jq -c '[.host_now.workspace,.host_now.window,.host_now.title]')
after_focus=$(printf '%s' "$after" | jq -c '[.host_now.workspace,.host_now.window,.host_now.title]')

[ "$(printf '%s' "$version" | jq -r .protocol_version)" = "1" ]
[ "$(printf '%s' "$before" | jq -r .running)" = "true" ]
[ "$(printf '%s' "$windows" | jq 'length')" -ge 1 ]
[ "$before_focus" = "$after_focus" ]
[ -s "$evidence" ]

jq -n \
    --arg evidence "$evidence" \
    --arg before_focus "$before_focus" \
    --arg after_focus "$after_focus" \
    --argjson version "$version" \
    --argjson observe "$observe" \
    '{passed:true,evidence:$evidence,version:$version,observe:$observe,host_focus_before:$before_focus,host_focus_after:$after_focus}'
