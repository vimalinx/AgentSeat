"""Installation activation must be reversible and preserve conflicting user files."""
import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

@pytest.fixture
def installation(tmp_path):
    source = tmp_path / 'source'
    (source/'scripts').mkdir(parents=True)
    shutil.copy2(ROOT/'scripts/install.sh', source/'scripts/install.sh')
    build = source/'scripts/build.sh'; build.write_text('#!/bin/sh\nexit 0\n'); build.chmod(0o755)
    (source/'bin').mkdir()
    for name in ['agentseat','agentseat-a11y','launch-app','run-microhost','x11-clipboard-owner','agentseatd','agentseat-eye','agentseat-microhost']:
        path=source/'bin'/name;path.write_text('#!/bin/sh\nexit 0\n');path.chmod(0o755)
    (source/'python').mkdir()
    (source/'python/agentseat_client.py').write_text('SDK fixture\n')
    (source/'lib').mkdir()
    (source/'lib/libagentseat-atk-dispatch.so').write_bytes(b'ATK dispatch fixture')
    (source/'lib/agentseat_cli').mkdir()
    (source/'lib/agentseat_cli/__init__.py').write_text('controller fixture\n')
    (source/'config').mkdir()
    for name in ['LICENSE','THIRD_PARTY_NOTICES.md','config/config.toml.example']:
        (source/name).write_text('fixture\n')
    for language in ['zh_CN','zh_TW']:
        path=source/'build/locale'/language/'LC_MESSAGES';path.mkdir(parents=True);(path/'agentseat.mo').write_bytes(b'fixture')
    runtime=tmp_path/'runtime';runtime.mkdir()
    env={**os.environ,'XDG_DATA_HOME':str(tmp_path/'data'),'XDG_BIN_HOME':str(tmp_path/'bin'),'XDG_RUNTIME_DIR':str(runtime)}
    current=tmp_path/'data/agentseat/current'
    def run(*args):
        return subprocess.run([str(source/'scripts/install.sh'),*args],env=env,capture_output=True,text=True)
    return source,current,tmp_path/'bin/agentseat',run


def test_install_repeat_and_rollback(installation):
    source,current,command,run=installation
    assert run().returncode==0
    first=current.resolve(); assert command.resolve()==first/'bin/agentseat'
    assert run().returncode==0 and current.resolve()==first
    (source/'bin/agentseat').write_text('#!/bin/sh\nexit 2\n')
    assert run().returncode==0 and current.resolve()!=first
    assert run('--rollback',first.name).returncode==0 and current.resolve()==first


def test_conflicting_user_command_does_not_activate(installation):
    source,current,command,run=installation
    assert run().returncode==0
    original=current.resolve()
    command.unlink();command.write_text('user-owned command')
    (source/'bin/agentseat').write_text('#!/bin/sh\nexit 2\n')
    result=run()
    assert result.returncode!=0 and 'Refusing to replace non-symlink' in result.stderr
    assert current.resolve()==original and command.read_text()=='user-owned command'


def test_corrupt_release_cannot_be_reactivated(installation):
    source,current,command,run=installation
    assert run().returncode==0
    original=current.resolve()
    (original/'bin/agentseat').write_text('corrupted')
    (source/'bin/agentseat').write_text('#!/bin/sh\nexit 2\n')
    assert run().returncode==0
    replacement=current.resolve()
    result=run('--rollback',original.name)
    assert result.returncode!=0 and 'integrity verification failed' in result.stderr
    assert current.resolve()==replacement


def test_invalid_rollback_never_changes_activation(installation):
    _,current,_,run=installation
    assert run().returncode==0
    original=current.resolve()
    assert run('--rollback','../../elsewhere').returncode!=0
    assert current.resolve()==original


def test_sdk_changes_release_digest_and_is_verified(installation):
    source,current,_,run=installation
    assert run().returncode==0
    first=current.resolve()
    assert (first/'share/python/agentseat_client.py').read_text()=='SDK fixture\n'
    (source/'python/agentseat_client.py').write_text('SDK v2\n')
    assert run().returncode==0 and current.resolve()!=first
    (first/'share/python/agentseat_client.py').write_text('corrupted')
    assert run('--rollback',first.name).returncode!=0


def test_dispatch_library_changes_digest_and_is_verified(installation):
    source,current,_,run=installation
    assert run().returncode==0
    first=current.resolve()
    library='lib/libagentseat-atk-dispatch.so'
    assert (first/library).read_bytes()==b'ATK dispatch fixture'
    (source/library).write_bytes(b'updated dispatch library')
    assert run().returncode==0 and current.resolve()!=first
    (first/library).write_bytes(b'corrupted')
    assert run('--rollback',first.name).returncode!=0
