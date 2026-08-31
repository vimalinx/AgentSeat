#!/bin/sh
set -eu

script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
project_root=$(dirname "$script_dir")

for language in zh_CN zh_TW; do
    catalog="$project_root/po/$language.po"
    msgfmt --check --check-format -o /dev/null "$catalog"
    if msgattrib --untranslated --no-obsolete "$catalog" | grep -Eq '^msgid ".+"'; then
        printf '%s\n' "check-translations: untranslated messages in $catalog" >&2
        exit 1
    fi
    if msgattrib --only-fuzzy --no-obsolete "$catalog" | grep -Eq '^msgid ".+"'; then
        printf '%s\n' "check-translations: fuzzy messages in $catalog" >&2
        exit 1
    fi
done

printf '%s\n' "AgentSeat translations are complete and format-safe"
