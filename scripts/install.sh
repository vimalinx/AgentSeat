#!/bin/sh
set -eu
umask 077

script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
project_root=$(dirname "$script_dir")
build_root=${AGENTSEAT_BUILD_DIR:-$project_root/build}
data_home=${XDG_DATA_HOME:-$HOME/.local/share}
bin_home=${XDG_BIN_HOME:-$HOME/.local/bin}
install_root="$data_home/agentseat"
command_link="$bin_home/agentseat"
rollback=
case ${1:-} in
    '') ;;
    --rollback)
        [ "$#" -eq 2 ] || { printf '%s\n' 'Usage: install.sh [--rollback <release-digest>]' >&2; exit 2; }
        rollback=$2
        case "$rollback" in *[!0-9a-f]*|'') printf '%s\n' 'Invalid release digest' >&2; exit 2;; esac
        [ "${#rollback}" -eq 12 ] || { printf '%s\n' 'Invalid release digest' >&2; exit 2; }
        ;;
    *) printf '%s\n' 'Usage: install.sh [--rollback <release-digest>]' >&2; exit 2 ;;
esac

# Reject conflicting user files before building or changing the current release.
for path in "$command_link" "$install_root/current"; do
    if [ -e "$path" ] && [ ! -L "$path" ]; then
        printf '%s\n' "Refusing to replace non-symlink: $path" >&2
        exit 1
    fi
done

runtime_dir=${XDG_RUNTIME_DIR:-/run/user/$(id -u)}
[ -d "$runtime_dir" ] || { printf '%s\n' 'A user runtime directory is required' >&2; exit 1; }
exec 9>"$runtime_dir/agentseat-install.lock"
flock 9
stage=
activation_link="$install_root/.current-$$"
cleanup() {
    [ -z "$stage" ] || rm -rf -- "$stage"
    [ ! -L "$activation_link" ] || rm -- "$activation_link"
}
trap cleanup EXIT
trap 'exit 130' HUP INT TERM

if [ -n "$rollback" ]; then
    digest=$rollback
    release="$install_root/releases/$digest"
    for name in agentseat agentseatd agentseat-microhost; do
        [ -x "$release/bin/$name" ] || { printf '%s\n' "Incomplete rollback release: $release" >&2; exit 1; }
    done
else
    "$project_root/scripts/build.sh"
    digest=$(
        {
            for name in agentseat agentseat-a11y launch-app run-microhost x11-clipboard-owner agentseatd agentseat-eye agentseat-microhost; do
                sha256sum "$project_root/bin/$name"
            done
            sha256sum "$project_root/lib/libagentseat-atk-dispatch.so" "$project_root/python/agentseat_client.py" "$project_root/LICENSE" "$project_root/THIRD_PARTY_NOTICES.md" "$project_root/config/config.toml.example"
            for language in zh_CN zh_TW; do
                sha256sum "$build_root/locale/$language/LC_MESSAGES/agentseat.mo"
            done
        } | sha256sum | cut -c1-12
    )
    release="$install_root/releases/$digest"
    install -d "$install_root/releases" "$bin_home"
    if [ ! -d "$release" ]; then
        stage=$(mktemp -d "$install_root/releases/.stage-$digest-XXXXXX")
        install -d "$stage/bin" "$stage/lib" "$stage/share/examples" "$stage/share/locale" "$stage/share/python"
        install -m 0644 "$project_root/lib/libagentseat-atk-dispatch.so" "$stage/lib/libagentseat-atk-dispatch.so"
        for name in agentseat agentseat-a11y launch-app run-microhost x11-clipboard-owner agentseatd agentseat-eye agentseat-microhost; do
            install -m 0755 "$project_root/bin/$name" "$stage/bin/$name"
        done
        install -m 0644 "$project_root/python/agentseat_client.py" "$stage/share/python/agentseat_client.py"
        install -m 0644 "$project_root/LICENSE" "$stage/share/LICENSE"
        install -m 0644 "$project_root/THIRD_PARTY_NOTICES.md" "$stage/share/THIRD_PARTY_NOTICES.md"
        install -m 0644 "$project_root/config/config.toml.example" "$stage/share/examples/config.toml"
        for language in zh_CN zh_TW; do
            install -d "$stage/share/locale/$language/LC_MESSAGES"
            install -m 0644 "$build_root/locale/$language/LC_MESSAGES/agentseat.mo" "$stage/share/locale/$language/LC_MESSAGES/agentseat.mo"
        done
        (cd "$stage" && sha256sum bin/* lib/*.so share/python/*.py share/LICENSE share/THIRD_PARTY_NOTICES.md share/examples/config.toml share/locale/*/LC_MESSAGES/agentseat.mo) > "$stage/manifest.sha256"
        mv "$stage" "$release"
        stage=
    elif [ ! -f "$release/manifest.sha256" ]; then
        printf '%s\n' "Existing release has no integrity manifest: $release" >&2
        exit 1
    fi
fi

if [ -f "$release/manifest.sha256" ]; then
    (cd "$release" && sha256sum --status -c manifest.sha256) || {
        printf '%s\n' "Release integrity verification failed: $release" >&2
        exit 1
    }
fi
# Repeat the conflict check under the install lock before activation.
for path in "$command_link" "$install_root/current"; do
    if [ -e "$path" ] && [ ! -L "$path" ]; then
        printf '%s\n' "Refusing to replace non-symlink: $path" >&2
        exit 1
    fi
done
install -d "$bin_home"
ln -s "$release" "$activation_link"
mv -Tf "$activation_link" "$install_root/current"
ln -sfn "$install_root/current/bin/agentseat" "$command_link"

printf '%s\n' "Active AgentSeat release: $release"
printf '%s\n' "Command: $command_link"
printf '%s\n' 'Existing application sessions were not restarted.'
