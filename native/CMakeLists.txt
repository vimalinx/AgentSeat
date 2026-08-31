cmake_minimum_required(VERSION 3.20)
project(agentseat_native VERSION 0.5.0 LANGUAGES C)

set(CMAKE_C_STANDARD 17)
set(CMAKE_C_STANDARD_REQUIRED ON)
set(CMAKE_RUNTIME_OUTPUT_DIRECTORY "${CMAKE_CURRENT_BINARY_DIR}/bin")

find_package(PkgConfig REQUIRED)
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

agentseat_protocol(virtual-keyboard-unstable-v1)
agentseat_protocol(wlr-virtual-pointer-unstable-v1)

add_executable(agentseatd
  agentseatd.c
  agentseat-i18n.c
  agentseat-input.c
  ${AGENTSEAT_PROTOCOL_SOURCES}
  ${AGENTSEAT_PROTOCOL_HEADERS}
)
target_include_directories(agentseatd PRIVATE "${CMAKE_CURRENT_BINARY_DIR}")
target_link_libraries(agentseatd PRIVATE PkgConfig::JSONC PkgConfig::WAYLAND_CLIENT PkgConfig::XKBCOMMON)
target_compile_options(agentseatd PRIVATE -Wall -Wextra -Wpedantic -Werror=implicit-function-declaration)

add_executable(agentseat-eye agentseat-eye.c agentseat-i18n.c)
target_link_libraries(agentseat-eye PRIVATE PkgConfig::GTK4_LAYER_SHELL PkgConfig::GTK4 PkgConfig::JSONC m)
target_compile_options(agentseat-eye PRIVATE -Wall -Wextra -Wpedantic -Werror=implicit-function-declaration)

install(TARGETS agentseatd agentseat-eye RUNTIME DESTINATION bin)
