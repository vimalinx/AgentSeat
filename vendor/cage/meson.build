project('cage', 'c',
  version: '0.3.1',
  license: 'MIT',
  meson_version: '>=0.58.1',
  default_options: [
    'c_std=c11',
    'warning_level=2',
    'werror=true',
  ],
)

add_project_arguments(
  [
    '-DWLR_USE_UNSTABLE',
    '-Wundef',
    '-Wno-unused-parameter',
  ],
  language: 'c',
)

fs = import('fs')

if get_option('buildtype').startswith('debug')
  add_project_arguments('-DDEBUG', language : 'c')
endif

cc = meson.get_compiler('c')

is_freebsd = host_machine.system().startswith('freebsd')
if is_freebsd
  add_project_arguments(
    [
      '-Wno-format-extra-args',
      '-Wno-gnu-zero-variadic-macro-arguments',
    ],
    language: 'c'
  )
endif

wlroots        = dependency('wlroots-0.20', fallback: ['wlroots', 'wlroots'])
wayland_server = dependency('wayland-server')
wayland_client = dependency('wayland-client')
wayland_protocols = dependency('wayland-protocols')
xkbcommon      = dependency('xkbcommon')
math           = cc.find_library('m')

wayland_scanner = find_program('wayland-scanner', native: true)
wayland_protocols_dir = wayland_protocols.get_variable(pkgconfig: 'pkgdatadir')
viewporter_xml = join_paths(wayland_protocols_dir, 'stable', 'viewporter', 'viewporter.xml')
viewporter_header = custom_target(
  'viewporter-client-header',
  input: viewporter_xml,
  output: 'viewporter-client-protocol.h',
  command: [wayland_scanner, 'client-header', '@INPUT@', '@OUTPUT@'],
)
viewporter_code = custom_target(
  'viewporter-client-code',
  input: viewporter_xml,
  output: 'viewporter-client-protocol.c',
  command: [wayland_scanner, 'private-code', '@INPUT@', '@OUTPUT@'],
)

text_input_xml = join_paths(wayland_protocols_dir, 'unstable', 'text-input', 'text-input-unstable-v3.xml')
text_input_header = custom_target('text-input-client-header', input: text_input_xml,
  output: 'text-input-client-protocol.h', command: [wayland_scanner, 'client-header', '@INPUT@', '@OUTPUT@'])
text_input_code = custom_target('text-input-client-code', input: text_input_xml,
  output: 'text-input-client-protocol.c', command: [wayland_scanner, 'private-code', '@INPUT@', '@OUTPUT@'])

control_xml = '../../native/protocols/agentseat-control-v1.xml'
xdg_shell_xml = join_paths(wayland_protocols_dir, 'stable', 'xdg-shell', 'xdg-shell.xml')
xdg_shell_client_header = custom_target(
  'xdg-shell-client-header', input: xdg_shell_xml,
  output: 'xdg-shell-client-protocol.h',
  command: [wayland_scanner, 'client-header', '@INPUT@', '@OUTPUT@'],
)
xdg_shell_client_code = custom_target(
  'xdg-shell-client-code', input: xdg_shell_xml,
  output: 'xdg-shell-client-protocol.c',
  command: [wayland_scanner, 'private-code', '@INPUT@', '@OUTPUT@'],
)
control_header = custom_target(
  'agentseat-control-server-header', input: control_xml,
  output: 'agentseat-control-v1-server-protocol.h',
  command: [wayland_scanner, 'server-header', '@INPUT@', '@OUTPUT@'],
)
control_code = custom_target(
  'agentseat-control-code', input: control_xml,
  output: 'agentseat-control-v1-protocol.c',
  command: [wayland_scanner, 'private-code', '@INPUT@', '@OUTPUT@'],
)

have_xwayland = wlroots.get_variable(pkgconfig: 'have_xwayland', internal: 'have_xwayland') == 'true'

version = '@0@'.format(meson.project_version())
if fs.is_dir('.git')
  git = find_program('git', native: true, required: false)
  if git.found()
    git_commit = run_command([git, 'rev-parse', '--short', 'HEAD'], check: false)
    git_branch = run_command([git, 'rev-parse', '--abbrev-ref', 'HEAD'], check: false)
    if git_commit.returncode() == 0 and git_branch.returncode() == 0
      version = '@0@-@1@ (branch \'@2@\')'.format(
        meson.project_version(),
        git_commit.stdout().strip(),
        git_branch.stdout().strip(),
      )
    endif
  endif
endif

conf_data = configuration_data()
conf_data.set10('CAGE_HAS_XWAYLAND', have_xwayland)
conf_data.set_quoted('CAGE_VERSION', version)

scdoc = dependency('scdoc', version: '>=1.9.2', native: true, required: get_option('man-pages'))
if scdoc.found()
  scdoc_prog = find_program(scdoc.get_variable('scdoc'), native: true)
  sh = find_program('sh', native: true)
  mandir = get_option('mandir')
  man_files = [
    'cage.1.scd'
  ]
  foreach filename : man_files
    topic = filename.split('.')[-3].split('/')[-1]
    section = filename.split('.')[-2]
    output = '@0@.@1@'.format(topic, section)

    custom_target(
      output,
      input: filename,
      output: output,
      command: [
        sh, '-c', '@0@ < @INPUT@ > @1@'.format(scdoc_prog.full_path(), output)
      ],
      install: true,
      install_dir: '@0@/man@1@'.format(mandir, section)
    )
  endforeach
endif

cage_sources = [
  'cage.c',
  'idle_inhibit_v1.c',
  'output.c',
  'host-window.c',
  'application-lifetime.c',
  'host-clipboard.c',
  'host-text-input.c',
  'host-keyboard.c',
  text_input_header,
  text_input_code,
  'seat.c',
  'agentseat-scene.c',
  'agentseat-clipboard.c',
  'view.c',
  'xdg_shell.c',
  control_header,
  control_code,
  viewporter_header,
  viewporter_code,
  xdg_shell_client_header,
  xdg_shell_client_code,
  configure_file(input: 'config.h.in',
                 output: 'config.h',
                 configuration: conf_data),
]

if conf_data.get('CAGE_HAS_XWAYLAND', 0) == 1
  cage_sources += 'xwayland.c'
endif

executable(
  'agentseat-microhost',
  cage_sources,
  dependencies: [
    wayland_server,
    wayland_client,
    wlroots,
    xkbcommon,
    math,
  ],
  install: true,
)

summary = [
  '',
  'Cage @0@'.format(version),
  '',
  '    xwayland: @0@'.format(have_xwayland),
  ''
]
message('\n'.join(summary))
