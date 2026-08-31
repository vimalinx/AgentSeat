#!/bin/sh
set -eu

script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
project_root=$(dirname "$script_dir")
build_root=${AGENTSEAT_BUILD_DIR:-$project_root/build}
native_build="$build_root/native"
microhost_build="$build_root/microhost"

AGENTSEAT_LOCALE_BUILD_DIR="$build_root/locale" "$script_dir/build-translations.sh"

cmake -S "$project_root/native" -B "$native_build" -DCMAKE_BUILD_TYPE=Release
cmake --build "$native_build" --parallel

if [ -d "$microhost_build" ]; then
    meson setup --reconfigure "$microhost_build" "$project_root/vendor/cage" \
        --buildtype=release -Dman-pages=disabled
else
    meson setup "$microhost_build" "$project_root/vendor/cage" \
        --buildtype=release -Dman-pages=disabled
fi
meson compile -C "$microhost_build"

install -m 0755 "$native_build/bin/agentseatd" "$project_root/bin/agentseatd"
install -m 0755 "$native_build/bin/agentseat-eye" "$project_root/bin/agentseat-eye"
install -m 0755 "$microhost_build/agentseat-microhost" "$project_root/bin/agentseat-microhost"

printf '%s\n' "Built AgentSeat binaries in $project_root/bin"
