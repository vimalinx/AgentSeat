cmake_minimum_required(VERSION 3.20)
project(agentseat_native VERSION 0.6.1 LANGUAGES C)

set(CMAKE_C_STANDARD 17)
set(CMAKE_C_STANDARD_REQUIRED ON)
set(CMAKE_RUNTIME_OUTPUT_DIRECTORY "${CMAKE_CURRENT_BINARY_DIR}/bin")

find_package(PkgConfig REQUIRED)
find_package(PNG REQUIRED)
find_package(Threads REQUIRED)
pkg_check_modules(GIO REQUIRED IMPORTED_TARGET gio-2.0)
pkg_check_modules(JSONC REQUIRED IMPORTED_TARGET json-c)
pkg_check_modules(WAYLAND_CLIENT REQUIRED IMPORTED_TARGET wayland-client)
pkg_check_modules(XKBCOMMON REQUIRED IMPORTED_TARGET xkbcommon)
pkg_check_modules(GTK4 REQUIRED IMPORTED_TARGET gtk4)
pkg_check_modules(GTK4_LAYER_SHELL REQUIRED IMPORTED_TARGET gtk4-layer-shell-0)
find_program(WAYLAND_SCANNER wayland-scanner REQUIRED)

function(agentseat_protocol stem)
  set(xml "${CMAKE_CURRENT_SOURCE_DIR}/protocols/${stem}.xml")
  set(header "${CMAKE_CURRENT_BINARY_DIR}/${stem}-client-protocol.h")
  set(code "${CMAKE_CURRENT_BINARY_DIR}/${stem}-protocol.c")
  add_custom_command(
    OUTPUT "${header}" "${code}"
    COMMAND "${WAYLAND_SCANNER}" client-header "${xml}" "${header}"
    COMMAND "${WAYLAND_SCANNER}" private-code "${xml}" "${code}"
    DEPENDS "${xml}"
    VERBATIM
  )
  set(AGENTSEAT_PROTOCOL_SOURCES ${AGENTSEAT_PROTOCOL_SOURCES} "${code}" PARENT_SCOPE)
  set(AGENTSEAT_PROTOCOL_HEADERS ${AGENTSEAT_PROTOCOL_HEADERS} "${header}" PARENT_SCOPE)
endfunction()

agentseat_protocol(agentseat-control-v1)
agentseat_protocol(wlr-screencopy-unstable-v1)
agentseat_protocol(virtual-keyboard-unstable-v1)
agentseat_protocol(wlr-virtual-pointer-unstable-v1)

add_executable(agentseatd
  agentseatd.c
  agentseat-i18n.c
  agentseat-input.c
  agentseat-semantic.c
  request-ledger.c
  agentseat-capture.c
  ${AGENTSEAT_PROTOCOL_SOURCES}
  ${AGENTSEAT_PROTOCOL_HEADERS}
)
target_include_directories(agentseatd PRIVATE "${CMAKE_CURRENT_BINARY_DIR}")
target_link_libraries(agentseatd PRIVATE PkgConfig::JSONC PkgConfig::WAYLAND_CLIENT PkgConfig::XKBCOMMON PNG::PNG Threads::Threads PkgConfig::GIO)
target_compile_options(agentseatd PRIVATE -Wall -Wextra -Wpedantic -Werror=implicit-function-declaration)

add_executable(agentseat-eye agentseat-eye.c agentseat-i18n.c)
target_link_libraries(agentseat-eye PRIVATE PkgConfig::GTK4_LAYER_SHELL PkgConfig::GTK4 PkgConfig::JSONC m)
target_compile_options(agentseat-eye PRIVATE -Wall -Wextra -Wpedantic -Werror=implicit-function-declaration)

install(TARGETS agentseatd agentseat-eye RUNTIME DESTINATION bin)

# Optional application-side ATK dispatch isolation; no helper process or toolkit
# adaptation in the native controller. Enabled only by the scoped a11y launcher.
pkg_check_modules(ATK REQUIRED IMPORTED_TARGET atk)
pkg_check_modules(DBUS REQUIRED IMPORTED_TARGET dbus-1)
add_library(agentseat-atk-dispatch SHARED agentseat-atk-dispatch.c)
target_link_libraries(agentseat-atk-dispatch PRIVATE PkgConfig::ATK PkgConfig::DBUS ${CMAKE_DL_LIBS})
target_compile_options(agentseat-atk-dispatch PRIVATE -Wall -Wextra -Wpedantic -Werror=implicit-function-declaration)
set_target_properties(agentseat-atk-dispatch PROPERTIES LIBRARY_OUTPUT_DIRECTORY "${CMAKE_CURRENT_BINARY_DIR}/lib")
install(TARGETS agentseat-atk-dispatch LIBRARY DESTINATION lib)
