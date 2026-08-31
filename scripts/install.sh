#!/bin/sh
set -eu

script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
project_root=$(dirname "$script_dir")
install_prefix=${PREFIX:-$HOME/.local}
install_root="$install_prefix/lib/agentseat"
command_link="$install_prefix/bin/agentseat"

"$project_root/scripts/build.sh"

install -d "$install_root/bin" "$install_root/share/examples" "$install_prefix/bin"
for name in agentseat launch-app run-microhost x11-clipboard-owner agentseatd agentseat-eye agentseat-microhost; do
    install -m 0755 "$project_root/bin/$name" "$install_root/bin/$name"
done
install -m 0644 "$project_root/LICENSE" "$install_root/share/LICENSE"
install -m 0644 "$project_root/THIRD_PARTY_NOTICES.md" "$install_root/share/THIRD_PARTY_NOTICES.md"
install -m 0644 "$project_root/config/config.toml.example" "$install_root/share/examples/config.toml"

if [ -e "$command_link" ] && [ ! -L "$command_link" ]; then
    printf '%s\n' "Refusing to replace non-symlink: $command_link" >&2
    exit 1
fi
ln -sfn "$install_root/bin/agentseat" "$command_link"

printf '%s\n' "Installed AgentSeat at $install_root"
printf '%s\n' "Command: $command_link"
