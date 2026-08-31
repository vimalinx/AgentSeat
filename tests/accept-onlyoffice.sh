#!/bin/sh
set -eu

root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
agentseat="$root/bin/agentseat"
evidence_dir=$($agentseat paths | jq -r .captures)
mkdir -p "$evidence_dir"
evidence="$evidence_dir/onlyoffice-acceptance.png"
open_probe="$evidence_dir/onlyoffice-open-probe.png"
text='AGENTSEAT TEST PASSED'

before=$($agentseat status)

# The caller opens an isolated temporary DOCX directly. Wait for the editor
# canvas instead of depending on OnlyOffice's remembered Home-page state.
ready=0
attempt=0
while [ "$attempt" -lt 4 ]; do
    sleep 1
    $agentseat observe "$open_probe" >/dev/null
    if ! command -v tesseract >/dev/null 2>&1 || \
       tesseract "$open_probe" stdout 2>/dev/null | grep -F 'Word count' >/dev/null; then
        ready=1
        break
    fi
    attempt=$((attempt + 1))
done
[ "$ready" -eq 1 ]

# Ctrl+A gives the private Agent keyboard a deterministic replacement target
# without relying on toolkit or display-scale-specific pointer coordinates.
$agentseat key 29 down >/dev/null
$agentseat key 30 down >/dev/null
$agentseat key 30 up >/dev/null
$agentseat key 29 up >/dev/null
type_result=$($agentseat type "$text")
sleep 2
observe_result=$($agentseat observe "$evidence")
after=$($agentseat status)

before_focus=$(printf '%s' "$before" | jq -c '[.host_now.workspace,.host_now.window,.host_now.title]')
after_focus=$(printf '%s' "$after" | jq -c '[.host_now.workspace,.host_now.window,.host_now.title]')
outer_window=$(printf '%s' "$after" | jq -r '.state.outer_window')
after_window=$(printf '%s' "$after" | jq -r '.host_now.window')
[ "$after_window" != "$outer_window" ]
[ "$(printf '%s' "$type_result" | jq -r .typed)" -eq "${#text}" ]
[ -s "$evidence" ]

ocr=not-checked
if command -v tesseract >/dev/null 2>&1; then
    normalized_text=$(printf '%s' "$text" | tr -d ' _')
    if tesseract "$evidence" stdout 2>/dev/null | tr -d ' _' | grep -F "$normalized_text" >/dev/null; then
        ocr=passed
    else
        ocr=failed
        exit 1
    fi
fi

jq -n \
    --arg text "$text" \
    --arg evidence "$evidence" \
    --arg open_probe "$open_probe" \
    --argjson type "$type_result" \
    --argjson observe "$observe_result" \
    --arg before_focus "$before_focus" \
    --arg after_focus "$after_focus" \
    --arg ocr "$ocr" \
    --arg lane native-wayland-virtual-keyboard \
    '{passed:true,text:$text,evidence:$evidence,open_probe:$open_probe,type:$type,observe:$observe,host_focus_before:$before_focus,host_focus_after:$after_focus,host_outer_never_active:true,ocr:$ocr,lane:$lane}'
