#!/bin/sh
set -eu

script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
project_root=$(dirname "$script_dir")
brand_dir="$project_root/assets/brand"

command -v rsvg-convert >/dev/null 2>&1 || {
    printf '%s\n' "render-brand-assets: rsvg-convert is required" >&2
    exit 1
}

for size in 64 128 256 512; do
    rsvg-convert -w "$size" -h "$size" \
        "$brand_dir/agentseat-mark.svg" \
        -o "$brand_dir/agentseat-mark-$size.png"
done

rsvg-convert -w 1280 -h 640 \
    "$brand_dir/agentseat-social-preview.svg" \
    -o "$brand_dir/agentseat-social-preview.png"

printf '%s\n' "Rendered AgentSeat brand assets in $brand_dir"
