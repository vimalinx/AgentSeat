#!/bin/sh
set -eu

script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
project_root=$(dirname "$script_dir")
po_dir="$project_root/po"
temporary_dir=$(mktemp -d)
trap 'rm -rf "$temporary_dir"' EXIT HUP INT TERM
cd "$project_root"

for tool in xgettext msgcat msgmerge; do
    command -v "$tool" >/dev/null 2>&1 || {
        printf '%s\n' "update-translations: $tool is required" >&2
        exit 1
    }
done

xgettext --from-code=UTF-8 --language=Python --keyword=_ \
    -o "$temporary_dir/python.pot" bin/agentseat
xgettext --from-code=UTF-8 --language=C --keyword=AS_TR \
    -o "$temporary_dir/native.pot" \
    native/agentseat-eye.c native/agentseatd.c native/agentseat-input.c
msgcat --use-first "$temporary_dir/python.pot" "$temporary_dir/native.pot" \
    -o "$po_dir/agentseat.pot"

for language in zh_CN zh_TW; do
    msgmerge --quiet --update --backup=none "$po_dir/$language.po" "$po_dir/agentseat.pot"
done

printf '%s\n' "Updated AgentSeat translation template and catalogs"
