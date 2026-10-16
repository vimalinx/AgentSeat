"""Run the actual Wayland text-input bridge against disposable protocol peers."""
import os
from pathlib import Path
import shlex
import subprocess
ROOT = Path(__file__).resolve().parents[1]

def test_text_input_protocol_round_trip(tmp_path):
    protocols = subprocess.check_output(['pkg-config','--variable=pkgdatadir','wayland-protocols'],text=True).strip()
    xml = str(Path(protocols)/'unstable/text-input/text-input-unstable-v3.xml')
    for kind, name in [('client-header','text-input-client-protocol.h'),('private-code','text-input-client-protocol.c')]:
        subprocess.run(['wayland-scanner',kind,xml,str(tmp_path/name)],check=True,capture_output=True)
    (tmp_path/'config.h').write_text('#define CAGE_HAS_XWAYLAND 1\n')
    flags = shlex.split(subprocess.check_output(['pkg-config','--cflags','--libs','wlroots-0.20','wayland-client','wayland-server'],text=True))
    executable = tmp_path/'relay'
    subprocess.run(['cc','-std=c11','-DWLR_USE_UNSTABLE','-Wall','-Wextra','-Werror','-Wno-unused-parameter',
        '-I'+str(ROOT/'vendor/cage'),'-I'+str(tmp_path),str(ROOT/'tests/text_input_relay.c'),
        str(ROOT/'vendor/cage/host-text-input.c'),str(tmp_path/'text-input-client-protocol.c'),
        *flags,'-lm','-o',str(executable)],check=True,capture_output=True,text=True)
    runtime=tmp_path/'runtime';runtime.mkdir(mode=0o700)
    result = subprocess.run([str(executable)],env={**os.environ,'XDG_RUNTIME_DIR':str(runtime)},
        check=True,capture_output=True,text=True,timeout=20)
    assert result.stdout.startswith('PASS: real v3 state')
