#!/bin/sh
set -eu

script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
project_root=$(dirname "$script_dir")
output_root=${AGENTSEAT_LOCALE_BUILD_DIR:-$project_root/build/locale}

command -v msgfmt >/dev/null 2>&1 || {
    printf '%s\n' "build-translations: GNU msgfmt is required" >&2
    exit 1
}

for language in zh_CN zh_TW; do
    output_dir="$output_root/$language/LC_MESSAGES"
    install -d "$output_dir"
    msgfmt --check --check-format \
        -o "$output_dir/agentseat.mo" \
        "$project_root/po/$language.po"
done

printf '%s\n' "Built AgentSeat translations in $output_root"
