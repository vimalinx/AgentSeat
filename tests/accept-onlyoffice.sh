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
while [ "$attempt" -lt 20 ]; do
    sleep 1
    $agentseat observe "$open_probe" >/dev/null
    if ! command -v tesseract >/dev/null 2>&1 || \
       tesseract "$open_probe" stdout --psm 6 2>/dev/null | grep -F 'AgentSeat OnlyOffice live fixture' >/dev/null; then
        ready=1
        break
    fi
    attempt=$((attempt + 1))
done
[ "$ready" -eq 1 ]

# Focus the document body. First-run callouts may remain visible but do not
# cover this target; the final capture must show the inserted proof text.
$agentseat move 0.50 0.50 >/dev/null
$agentseat click >/dev/null

# Ctrl+A gives the private Agent keyboard a deterministic replacement target.
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
# keyboard.type reports typed; the chunked private paste lane reports transferred codepoints.
[ "$(printf '%s' "$type_result" | jq -r '.typed // .transferred_codepoints')" -eq "${#text}" ]
[ -s "$evidence" ]

ocr=not-checked
if command -v tesseract >/dev/null 2>&1; then
    normalized_text=$(printf '%s' "$text" | tr -d ' _')
    # A mostly blank document page is frequently mis-segmented by Tesseract's
    # automatic page mode. Uniform-block mode keeps the inserted proof line;
    # sparse mode also reads it when the caret touches its last glyph.
    ocr=failed
    for psm in 6 11; do
        if tesseract "$evidence" stdout --psm "$psm" 2>/dev/null | tr -d ' _' | grep -Fi "$normalized_text" >/dev/null; then
            ocr=passed
            break
        fi
    done
    [ "$ocr" = passed ] || exit 1
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
    --arg lane "$(printf '%s' "$type_result" | jq -r '.lane // empty')$(printf '%s' "$after" | jq -r 'if (.windows[] | select(.primary) | .xwayland) then " via private-xwayland" else " via native-wayland" end')" \
    '{passed:true,text:$text,evidence:$evidence,open_probe:$open_probe,type:$type,observe:$observe,host_focus_before:$before_focus,host_focus_after:$after_focus,host_outer_never_active:true,ocr:$ocr,lane:$lane}'
