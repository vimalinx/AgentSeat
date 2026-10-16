#define _GNU_SOURCE

#include <errno.h>
#include <fcntl.h>
#include <math.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>
#include <time.h>
#include <unistd.h>

#include <wayland-client.h>
#include <xkbcommon/xkbcommon.h>
#include <xkbcommon/xkbcommon-keysyms.h>

#include "agentseat-i18n.h"
#include "agentseat-input.h"
#include "agentseat-capture.h"
#include "agentseat-control-v1-client-protocol.h"
#include "virtual-keyboard-unstable-v1-client-protocol.h"
#include "wlr-virtual-pointer-unstable-v1-client-protocol.h"

#define BRIDGE_PROTOCOL_VERSION 1
#define MAX_TRACKED_KEYS 768
#define MAX_TRACKED_BUTTONS 1024
#define ABS_EXTENT 1000000U
#define INPUT_LINE_MAX 131072

struct saved_mods {
    xkb_mod_mask_t depressed;
    xkb_mod_mask_t latched;
    xkb_mod_mask_t locked;
    xkb_layout_index_t group;
};

struct input_state {
    struct wl_display* display;
    struct as_capture* capture;
    struct wl_registry* registry;
    struct agentseat_control_v1* control;
    uint32_t control_epoch;
    uint32_t window_action_token, window_action_status;
    uint32_t clipboard_token, clipboard_status, clipboard_transfers, clipboard_bytes;
    struct wl_callback* authorization;
    uint32_t authorization_requested, authorization_token, authorization_epoch;
    bool authorization_allowed;
    struct agentseat_scene scene, pending_scene;
    uint32_t submit_epoch, submit_geometry;
    uint32_t human_until_ms;
    bool gate_allowed;
    bool human_held;
    struct zwp_virtual_keyboard_manager_v1* keyboard_manager;
    struct zwlr_virtual_pointer_manager_v1* pointer_manager;
    struct wl_output* output;
    uint32_t output_global;
    uint32_t output_version;
    uint32_t pointer_manager_version;

    struct wl_seat* seat;
    struct zwp_virtual_keyboard_v1* keyboard;
    struct zwlr_virtual_pointer_v1* pointer;

    uint32_t target_global;
    uint32_t target_version;

    struct xkb_context* xkb_context;
    struct xkb_keymap* xkb_keymap;
    struct xkb_state* xkb_state;

    bool created;
    bool paused;
    bool quit;
    bool pressed_keys[MAX_TRACKED_KEYS];
    bool pressed_buttons[MAX_TRACKED_BUTTONS];
    double pointer_x;
    double pointer_y;
    char seat_id[256];
    char seat_name[256];
};

static void acquire_gate(struct input_state *self) {
    if (self->submit_geometry)
        agentseat_control_v1_acquire_geometry(self->control, self->submit_epoch, self->submit_geometry);
    else
        agentseat_control_v1_acquire(self->control, self->submit_epoch);
}

static uint32_t monotonic_ms(void) {
    struct timespec ts;
    if (clock_gettime(CLOCK_MONOTONIC, &ts) != 0)
        return 0;
    uint64_t ms = (uint64_t)ts.tv_sec * 1000ULL + (uint64_t)ts.tv_nsec / 1000000ULL;
    return (uint32_t)(ms & UINT32_MAX);
}

static bool utf8_next(const unsigned char* bytes, size_t length, size_t* offset, uint32_t* codepoint) {
    if (*offset >= length)
        return false;
    unsigned char first = bytes[(*offset)++];
    if (first < 0x80) {
        *codepoint = first;
        return true;
    }

    unsigned needed = 0;
    uint32_t value = 0;
    if ((first & 0xe0) == 0xc0) {
        needed = 1;
        value = first & 0x1f;
    } else if ((first & 0xf0) == 0xe0) {
        needed = 2;
        value = first & 0x0f;
    } else if ((first & 0xf8) == 0xf0) {
        needed = 3;
        value = first & 0x07;
    } else {
        return false;
    }
    if (*offset + needed > length)
        return false;
    for (unsigned i = 0; i < needed; ++i) {
        unsigned char next = bytes[(*offset)++];
        if ((next & 0xc0) != 0x80)
            return false;
        value = (value << 6) | (next & 0x3f);
    }
    if ((needed == 1 && value < 0x80) || (needed == 2 && value < 0x800) ||
        (needed == 3 && value < 0x10000) || value > 0x10ffff || (value >= 0xd800 && value <= 0xdfff))
        return false;
    *codepoint = value;
    return true;
}

static xkb_layout_index_t current_group(struct input_state* self) {
    xkb_layout_index_t count = xkb_keymap_num_layouts(self->xkb_keymap);
    for (xkb_layout_index_t i = 0; i < count; ++i) {
        if (xkb_state_layout_index_is_active(self->xkb_state, i, XKB_STATE_LAYOUT_EFFECTIVE))
            return i;
    }
    return 0;
}

static void save_mods(struct input_state* self, struct saved_mods* saved) {
    saved->depressed = xkb_state_serialize_mods(self->xkb_state, XKB_STATE_MODS_DEPRESSED);
    saved->latched = xkb_state_serialize_mods(self->xkb_state, XKB_STATE_MODS_LATCHED);
    saved->locked = xkb_state_serialize_mods(self->xkb_state, XKB_STATE_MODS_LOCKED);
    saved->group = current_group(self);
}

static void send_modifiers_to(struct input_state* self, struct zwp_virtual_keyboard_v1* keyboard) {
    if (!keyboard)
        return;
    zwp_virtual_keyboard_v1_modifiers(
        keyboard,
        xkb_state_serialize_mods(self->xkb_state, XKB_STATE_MODS_DEPRESSED),
        xkb_state_serialize_mods(self->xkb_state, XKB_STATE_MODS_LATCHED),
        xkb_state_serialize_mods(self->xkb_state, XKB_STATE_MODS_LOCKED),
        current_group(self));
}

static void update_xkb_key(struct input_state* self, uint32_t evdev_code, bool pressed) {
    if (!self->xkb_state)
        return;
    xkb_state_update_key(self->xkb_state, evdev_code + 8U, pressed ? XKB_KEY_DOWN : XKB_KEY_UP);
    send_modifiers_to(self, self->keyboard);
}

static void send_raw_key(struct input_state* self, uint32_t evdev_code, bool pressed) {
    update_xkb_key(self, evdev_code, pressed);
    zwp_virtual_keyboard_v1_key(
        self->keyboard,
        monotonic_ms(),
        evdev_code,
        pressed ? WL_KEYBOARD_KEY_STATE_PRESSED : WL_KEYBOARD_KEY_STATE_RELEASED);
}

static bool find_symbol(
    struct input_state* self,
    xkb_keysym_t symbol,
    xkb_keycode_t* code,
    xkb_layout_index_t* group,
    xkb_level_index_t* level) {
    xkb_keycode_t minimum = xkb_keymap_min_keycode(self->xkb_keymap);
    xkb_keycode_t maximum = xkb_keymap_max_keycode(self->xkb_keymap);
    xkb_layout_index_t groups = xkb_keymap_num_layouts(self->xkb_keymap);
    xkb_layout_index_t start_group = current_group(self);

    for (xkb_layout_index_t g_offset = 0; g_offset < groups; ++g_offset) {
        xkb_layout_index_t g = (start_group + g_offset) % groups;
        for (xkb_keycode_t key = minimum; key <= maximum; ++key) {
            xkb_level_index_t levels = xkb_keymap_num_levels_for_key(self->xkb_keymap, key, g);
            for (xkb_level_index_t l = 0; l < levels; ++l) {
                const xkb_keysym_t* symbols = NULL;
                int count = xkb_keymap_key_get_syms_by_level(self->xkb_keymap, key, g, l, &symbols);
                for (int index = 0; index < count; ++index) {
                    if (symbols[index] == symbol) {
                        *code = key;
                        *group = g;
                        *level = l;
                        return true;
                    }
                }
            }
        }
    }
    return false;
}

static bool type_codepoint(struct input_state* self, struct zwp_virtual_keyboard_v1* keyboard, uint32_t codepoint) {
    xkb_keysym_t symbol;
    if (codepoint == '\n')
        symbol = XKB_KEY_Return;
    else if (codepoint == '\t')
        symbol = XKB_KEY_Tab;
    else
        symbol = xkb_utf32_to_keysym(codepoint);
    if (symbol == XKB_KEY_NoSymbol)
        return false;

    xkb_keycode_t code = 0;
    xkb_layout_index_t group = 0;
    xkb_level_index_t level = 0;
    if (!find_symbol(self, symbol, &code, &group, &level))
        return false;

    struct saved_mods saved;
    save_mods(self, &saved);

    xkb_mod_mask_t candidates[8] = {0};
    int count = xkb_keymap_key_get_mods_for_level(
        self->xkb_keymap,
        code,
        group,
        level,
        candidates,
        sizeof(candidates) / sizeof(candidates[0]));
    xkb_mod_mask_t mods = count > 0 ? candidates[0] : 0;
    xkb_state_update_mask(self->xkb_state, mods, 0, 0, 0, 0, group);
    send_modifiers_to(self, keyboard);

    uint32_t evdev_code = code >= 8 ? code - 8 : code;
    zwp_virtual_keyboard_v1_key(keyboard, monotonic_ms(), evdev_code, WL_KEYBOARD_KEY_STATE_PRESSED);
    zwp_virtual_keyboard_v1_key(keyboard, monotonic_ms(), evdev_code, WL_KEYBOARD_KEY_STATE_RELEASED);

    xkb_state_update_mask(
        self->xkb_state,
        saved.depressed,
        saved.latched,
        saved.locked,
        0,
        0,
        saved.group);
    send_modifiers_to(self, keyboard);
    return true;
}

static int create_keymap_fd(const char* keymap, size_t size) {
    int fd = memfd_create("agentseat-keymap", MFD_CLOEXEC | MFD_ALLOW_SEALING);
    if (fd < 0)
        return -1;
    size_t written = 0;
    while (written < size) {
        ssize_t result = write(fd, keymap + written, size - written);
        if (result < 0 && errno == EINTR)
            continue;
        if (result <= 0) {
            close(fd);
            return -1;
        }
        written += (size_t)result;
    }
    if (lseek(fd, 0, SEEK_SET) < 0) {
        close(fd);
        return -1;
    }
    return fd;
}

static bool init_keyboard(struct input_state* self, char* error, size_t error_size) {
    struct xkb_rule_names names = {
        .rules = NULL,
        .model = "pc105",
        .layout = getenv("AGENTSEAT_XKB_LAYOUT"),
        .variant = getenv("AGENTSEAT_XKB_VARIANT"),
        .options = getenv("AGENTSEAT_XKB_OPTIONS"),
    };
    if (!names.layout || !names.layout[0])
        names.layout = "us";

    self->xkb_context = xkb_context_new(XKB_CONTEXT_NO_FLAGS);
    if (!self->xkb_context) {
        snprintf(error, error_size, "%s", AS_TR("xkb_context_new failed"));
        return false;
    }
    self->xkb_keymap = xkb_keymap_new_from_names(self->xkb_context, &names, XKB_KEYMAP_COMPILE_NO_FLAGS);
    if (!self->xkb_keymap) {
        snprintf(error, error_size, AS_TR("Cannot compile XKB keymap for layout %s"), names.layout);
        return false;
    }
    self->xkb_state = xkb_state_new(self->xkb_keymap);
    if (!self->xkb_state) {
        snprintf(error, error_size, "%s", AS_TR("xkb_state_new failed"));
        return false;
    }

    char* keymap = xkb_keymap_get_as_string(self->xkb_keymap, XKB_KEYMAP_FORMAT_TEXT_V1);
    if (!keymap) {
        snprintf(error, error_size, "%s", AS_TR("Cannot serialize XKB keymap"));
        return false;
    }
    size_t size = strlen(keymap) + 1;
    int fd = create_keymap_fd(keymap, size);
    free(keymap);
    if (fd < 0) {
        snprintf(error, error_size, AS_TR("Cannot allocate keymap memfd: %s"), strerror(errno));
        return false;
    }
    zwp_virtual_keyboard_v1_keymap(self->keyboard, WL_KEYBOARD_KEYMAP_FORMAT_XKB_V1, fd, (uint32_t)size);
    close(fd);
    send_modifiers_to(self, self->keyboard);
    return true;
}

static void teardown_keymap(struct input_state* self) {
    if (self->xkb_state)
        xkb_state_unref(self->xkb_state);
    if (self->xkb_keymap)
        xkb_keymap_unref(self->xkb_keymap);
    if (self->xkb_context)
        xkb_context_unref(self->xkb_context);
    self->xkb_state = NULL;
    self->xkb_keymap = NULL;
    self->xkb_context = NULL;
}

static void bind_target_seat(struct input_state* self) {
    if (!self->target_global || self->seat)
        return;
    uint32_t version = self->target_version > 9 ? 9 : self->target_version;
    self->seat = wl_registry_bind(self->registry, self->target_global, &wl_seat_interface, version);
}

static void control_state(
    void* data, struct agentseat_control_v1* control,
    uint32_t epoch, uint32_t allowed, uint32_t quiet_ms, uint32_t human_held) {
    struct input_state* self = data;
    (void)control;
    self->control_epoch = epoch;
    self->gate_allowed = allowed != 0;
    self->human_until_ms = monotonic_ms() + quiet_ms;
    self->human_held = human_held != 0;
}

static void scene_begin(void* data, struct agentseat_control_v1* control, uint32_t version) {
    struct input_state* self = data;
    (void)control;
    self->pending_scene = (struct agentseat_scene){.version = version};
}
static void window_info(void* data, struct agentseat_control_v1* control, uint32_t id,
    uint32_t parent_id, uint32_t pid, uint32_t flags, int32_t x, int32_t y,
    int32_t width, int32_t height, const char* title, const char* app_id) {
    struct input_state* self = data;
    (void)control;
    if (self->pending_scene.count >= AGENTSEAT_MAX_WINDOWS) {
        self->pending_scene.truncated = true;
        return;
    }
    struct agentseat_window* window = &self->pending_scene.windows[self->pending_scene.count++];
    *window = (struct agentseat_window){.id = id, .parent_id = parent_id, .pid = pid,
        .flags = flags, .x = x, .y = y, .width = width, .height = height};
    snprintf(window->title, sizeof(window->title), "%s", title);
    snprintf(window->app_id, sizeof(window->app_id), "%s", app_id);
}
static void scene_done(void* data, struct agentseat_control_v1* control, uint32_t version,
    uint32_t focused_id, int32_t width, int32_t height, int32_t pixel_width,
    int32_t pixel_height, uint32_t scale_120, uint32_t truncated) {
    struct input_state* self = data;
    (void)control;
    self->pending_scene.version = version;
    self->pending_scene.focused_id = focused_id;
    self->pending_scene.width = width;
    self->pending_scene.height = height;
    self->pending_scene.pixel_width = pixel_width;
    self->pending_scene.pixel_height = pixel_height;
    self->pending_scene.scale_120 = scale_120;
    self->pending_scene.truncated |= truncated != 0;
    self->scene = self->pending_scene;
}
static void window_action_done(void* data, struct agentseat_control_v1* control, uint32_t token, uint32_t status) {
    struct input_state* self = data;
    (void)control;
    self->window_action_token = token;
    self->window_action_status = status;
}
static void clipboard_offered(void* data, struct agentseat_control_v1* control, uint32_t token, uint32_t status) {
    struct input_state* self = data; (void)control;
    self->clipboard_token = token; self->clipboard_status = status;
}
static void clipboard_transfer(void* data, struct agentseat_control_v1* control, uint32_t token, uint32_t bytes) {
    struct input_state* self = data; (void)control;
    if (self->clipboard_token != token) return;
    self->clipboard_transfers++; self->clipboard_bytes += bytes;
}
static const struct agentseat_control_v1_listener control_listener = {
    .state = control_state, .scene_begin = scene_begin, .window_info = window_info,
    .scene_done = scene_done, .window_action_done = window_action_done,
    .clipboard_offered = clipboard_offered, .clipboard_transfer = clipboard_transfer,
};

static void registry_global(
    void* data,
    struct wl_registry* registry,
    uint32_t id,
    const char* interface,
    uint32_t version) {
    struct input_state* self = data;
    if (strcmp(interface, agentseat_control_v1_interface.name) == 0 && version >= 2 && !self->control) {
        self->control = wl_registry_bind(registry, id, &agentseat_control_v1_interface, version > 3 ? 3 : version);
        agentseat_control_v1_add_listener(self->control, &control_listener, self);
    } else if (strcmp(interface, zwp_virtual_keyboard_manager_v1_interface.name) == 0 && !self->keyboard_manager) {
        self->keyboard_manager = wl_registry_bind(registry, id, &zwp_virtual_keyboard_manager_v1_interface, 1);
    } else if (strcmp(interface, zwlr_virtual_pointer_manager_v1_interface.name) == 0 && !self->pointer_manager) {
        uint32_t bind_version = version > 2 ? 2 : version;
        self->pointer_manager = wl_registry_bind(registry, id, &zwlr_virtual_pointer_manager_v1_interface, bind_version);
        self->pointer_manager_version = bind_version;
    } else if (strcmp(interface, wl_output_interface.name) == 0 && !self->output) {
        uint32_t bind_version = version > 4 ? 4 : version;
        self->output = wl_registry_bind(registry, id, &wl_output_interface, bind_version);
        self->output_global = id;
        self->output_version = bind_version;
    } else if (strcmp(interface, wl_seat_interface.name) == 0 && !self->target_global) {
        self->target_global = id;
        self->target_version = version;
        bind_target_seat(self);
    }
}

static void registry_remove(void* data, struct wl_registry* registry, uint32_t id) {
    (void)registry;
    struct input_state* self = data;
    if (id == self->output_global) {
        if (self->output_version >= WL_OUTPUT_RELEASE_SINCE_VERSION)
            wl_output_release(self->output);
        else
            wl_output_destroy(self->output);
        self->output = NULL;
        self->output_global = 0;
        self->output_version = 0;
    }
    if (id == self->target_global) {
        self->target_global = 0;
        self->target_version = 0;
        self->created = false;
    }
}

static const struct wl_registry_listener registry_listener = {
    .global = registry_global,
    .global_remove = registry_remove,
};

static void release_all(struct input_state* self) {
    if (self->keyboard) {
        for (uint32_t key = 0; key < MAX_TRACKED_KEYS; ++key) {
            if (!self->pressed_keys[key])
                continue;
            send_raw_key(self, key, false);
            self->pressed_keys[key] = false;
        }
    }
    if (self->pointer) {
        for (uint32_t button = 0; button < MAX_TRACKED_BUTTONS; ++button) {
            if (!self->pressed_buttons[button])
                continue;
            zwlr_virtual_pointer_v1_button(
                self->pointer,
                monotonic_ms(),
                button,
                WL_POINTER_BUTTON_STATE_RELEASED);
            self->pressed_buttons[button] = false;
        }
        zwlr_virtual_pointer_v1_frame(self->pointer);
    }
    if (self->display)
        wl_display_flush(self->display);
}

static void destroy_seat(struct input_state* self) {
    if (self->control && self->created)
        agentseat_control_v1_revoke(self->control);
    release_all(self);
    if (self->pointer)
        zwlr_virtual_pointer_v1_destroy(self->pointer);
    if (self->keyboard)
        zwp_virtual_keyboard_v1_destroy(self->keyboard);
    if (self->seat)
        wl_seat_release(self->seat);
    self->pointer = NULL;
    self->keyboard = NULL;
    self->seat = NULL;
    self->created = false;
    self->paused = true;
    memset(self->pressed_keys, 0, sizeof(self->pressed_keys));
    memset(self->pressed_buttons, 0, sizeof(self->pressed_buttons));
    teardown_keymap(self);
    if (self->display)
        wl_display_flush(self->display);
}

static bool create_seat(
    struct input_state* self,
    const char* seat_id,
    const char* name,
    char* error,
    size_t error_size) {
    if (self->created) {
        snprintf(error, error_size, "%s", AS_TR("An AgentSeat input scope already exists"));
        return false;
    }
    if (!self->keyboard_manager || !self->pointer_manager || !self->target_global) {
        snprintf(
            error,
            error_size,
            AS_TR("Required globals missing: seat=%d keyboard=%d pointer=%d"),
            self->target_global != 0,
            self->keyboard_manager != NULL,
            self->pointer_manager != NULL);
        return false;
    }

    snprintf(self->seat_id, sizeof(self->seat_id), "%s", seat_id);
    snprintf(self->seat_name, sizeof(self->seat_name), "%s", name);
    bind_target_seat(self);
    if (!self->seat) {
        snprintf(
            error,
            error_size,
            AS_TR("Cannot bind AgentSeat micro-host seat global %u"),
            self->target_global);
        destroy_seat(self);
        return false;
    }

    self->submit_epoch = self->control_epoch;
    acquire_gate(self);
    self->keyboard = zwp_virtual_keyboard_manager_v1_create_virtual_keyboard(self->keyboard_manager, self->seat);
    self->pointer = self->output && self->pointer_manager_version >= 2
        ? zwlr_virtual_pointer_manager_v1_create_virtual_pointer_with_output(self->pointer_manager, self->seat, self->output)
        : zwlr_virtual_pointer_manager_v1_create_virtual_pointer(self->pointer_manager, self->seat);
    if (!self->keyboard || !self->pointer) {
        snprintf(error, error_size, "%s", AS_TR("Cannot create virtual keyboard or pointer"));
        destroy_seat(self);
        return false;
    }
    if (!init_keyboard(self, error, error_size)) {
        destroy_seat(self);
        return false;
    }

    self->pointer_x = 0.5;
    self->pointer_y = 0.5;
    self->paused = false;
    self->created = true;
    wl_display_flush(self->display);
    return true;
}

static bool supported_codepoint(struct input_state* self, uint32_t codepoint) {
    xkb_keysym_t symbol;
    if (codepoint == '\n')
        symbol = XKB_KEY_Return;
    else if (codepoint == '\t')
        symbol = XKB_KEY_Tab;
    else
        symbol = xkb_utf32_to_keysym(codepoint);
    if (symbol == XKB_KEY_NoSymbol)
        return false;
    xkb_keycode_t code = 0;
    xkb_layout_index_t group = 0;
    xkb_level_index_t level = 0;
    return find_symbol(self, symbol, &code, &group, &level);
}

static bool input_state_init(struct input_state* self, char* error, size_t error_size) {
    memset(self, 0, sizeof(*self));
    self->paused = true;
    self->pointer_x = 0.5;
    self->pointer_y = 0.5;

    self->display = wl_display_connect(NULL);
    if (!self->display) {
        snprintf(error, error_size, "%s", AS_TR("Cannot connect to WAYLAND_DISPLAY"));
        return false;
    }
    self->capture = as_capture_create(self->display);
    self->registry = wl_display_get_registry(self->display);
    if (!self->registry) {
        snprintf(error, error_size, "%s", AS_TR("wl_display_get_registry failed"));
        return false;
    }
    wl_registry_add_listener(self->registry, &registry_listener, self);
    if (wl_display_roundtrip(self->display) < 0 || wl_display_roundtrip(self->display) < 0) {
        snprintf(error, error_size, "%s", AS_TR("Wayland registry roundtrip failed"));
        return false;
    }
    if (!self->control || !self->control_epoch) {
        snprintf(error, error_size, "Private compositor input gate is unavailable; use a matching AgentSeat build");
        return false;
    }
    return true;
}

static void input_state_destroy(struct input_state* self) {
    destroy_seat(self);
    if (self->authorization) wl_callback_destroy(self->authorization);
    as_capture_destroy(self->capture);
    if (self->control)
        agentseat_control_v1_destroy(self->control);
    if (self->pointer_manager)
        zwlr_virtual_pointer_manager_v1_destroy(self->pointer_manager);
    if (self->keyboard_manager)
        zwp_virtual_keyboard_manager_v1_destroy(self->keyboard_manager);
    if (self->output) {
        if (self->output_version >= WL_OUTPUT_RELEASE_SINCE_VERSION)
            wl_output_release(self->output);
        else
            wl_output_destroy(self->output);
    }
    if (self->registry)
        wl_registry_destroy(self->registry);
    if (self->display)
        wl_display_disconnect(self->display);
    memset(self, 0, sizeof(*self));
}

static struct input_state* input_state(struct agentseat_input* input) {
    return (struct input_state*)input;
}

static const struct input_state* input_state_const(const struct agentseat_input* input) {
    return (const struct input_state*)input;
}

static bool require_input_active(struct input_state* self, char* error, size_t error_size) {
    if (!self || !self->created) {
        snprintf(error, error_size, "%s", AS_TR("AgentSeat input scope is not created"));
        return false;
    }
    if (self->paused) {
        snprintf(error, error_size, "%s", AS_TR("AgentSeat input scope is paused"));
        return false;
    }
    return true;
}

struct agentseat_input* agentseat_input_open(char* error, size_t error_size) {
    struct input_state* self = calloc(1, sizeof(*self));
    if (!self) {
        snprintf(error, error_size, "%s", AS_TR("Cannot allocate AgentSeat input state"));
        return NULL;
    }
    if (!input_state_init(self, error, error_size)) {
        input_state_destroy(self);
        free(self);
        return NULL;
    }
    return (struct agentseat_input*)self;
}

void agentseat_input_close(struct agentseat_input* input) {
    struct input_state* self = input_state(input);
    if (!self)
        return;
    input_state_destroy(self);
    free(self);
}

int agentseat_input_fd(const struct agentseat_input* input) {
    const struct input_state* self = input_state_const(input);
    return self && self->display ? wl_display_get_fd(self->display) : -1;
}

bool agentseat_input_dispatch(struct agentseat_input* input, char* error, size_t error_size) {
    struct input_state* self = input_state(input);
    if (!self || !self->display || wl_display_dispatch(self->display) < 0) {
        snprintf(error, error_size, "%s", AS_TR("AgentSeat Wayland input connection was lost"));
        return false;
    }
    return true;
}

bool agentseat_input_create(
    struct agentseat_input* input,
    const char* seat_id,
    const char* seat_name,
    char* error,
    size_t error_size) {
    return create_seat(input_state(input), seat_id, seat_name, error, error_size);
}

void agentseat_input_destroy(struct agentseat_input* input) {
    struct input_state* self = input_state(input);
    if (self)
        destroy_seat(self);
}

bool agentseat_input_pause(struct agentseat_input* input, char* error, size_t error_size) {
    struct input_state* self = input_state(input);
    if (!self || !self->created) {
        snprintf(error, error_size, "%s", AS_TR("AgentSeat input scope is not created"));
        return false;
    }
    release_all(self);
    self->paused = true;
    return true;
}

bool agentseat_input_resume(struct agentseat_input* input, char* error, size_t error_size) {
    struct input_state* self = input_state(input);
    if (!self || !self->created) {
        snprintf(error, error_size, "%s", AS_TR("AgentSeat input scope is not created"));
        return false;
    }
    self->paused = false;
    return true;
}

void agentseat_input_status(const struct agentseat_input* input, struct agentseat_input_status* status) {
    const struct input_state* self = input_state_const(input);
    memset(status, 0, sizeof(*status));
    if (!self)
        return;
    status->control_protocol_version = self->control ? agentseat_control_v1_get_version(self->control) : 0;
    status->created = self->created;
    status->paused = self->paused;
    status->control_epoch = self->control_epoch;
    status->clipboard_token = self->clipboard_token;
    status->clipboard_status = self->clipboard_status;
    status->clipboard_transfers = self->clipboard_transfers;
    status->clipboard_bytes = self->clipboard_bytes;
    status->window_action_token = self->window_action_token;
    status->window_action_status = self->window_action_status;
    status->authorization_token = self->authorization_token;
    status->authorization_epoch = self->authorization_epoch;
    status->authorization_allowed = self->authorization_allowed;
    status->gate_allowed = self->gate_allowed;
    status->human_held = self->human_held;
    int32_t remaining = (int32_t)(self->human_until_ms - monotonic_ms());
    status->human_quiet_ms = remaining > 0 ? (unsigned)remaining : 0;
    status->seat_global = self->target_global;
    status->pointer_x = self->pointer_x;
    status->pointer_y = self->pointer_y;
}

static bool flush_input(struct input_state* self, char* error, size_t error_size) {
    if (wl_display_flush(self->display) >= 0)
        return true;
    snprintf(error, error_size, "%s", AS_TR("Cannot flush AgentSeat input events"));
    return false;
}

bool agentseat_input_move_absolute(
    struct agentseat_input* input,
    double x,
    double y,
    char* error,
    size_t error_size) {
    struct input_state* self = input_state(input);
    if (!require_input_active(self, error, error_size))
        return false;
    if (!isfinite(x) || !isfinite(y) || x < 0.0 || x > 1.0 || y < 0.0 || y > 1.0) {
        snprintf(
            error,
            error_size,
            "%s",
            AS_TR("Pointer coordinates must be normalized from 0 to 1"));
        return false;
    }
    acquire_gate(self);
    self->pointer_x = x;
    self->pointer_y = y;
    zwlr_virtual_pointer_v1_motion_absolute(
        self->pointer,
        monotonic_ms(),
        (uint32_t)(x * ABS_EXTENT),
        (uint32_t)(y * ABS_EXTENT),
        ABS_EXTENT,
        ABS_EXTENT);
    zwlr_virtual_pointer_v1_frame(self->pointer);
    return flush_input(self, error, error_size);
}

bool agentseat_input_button(
    struct agentseat_input* input,
    uint32_t button,
    bool pressed,
    char* error,
    size_t error_size) {
    struct input_state* self = input_state(input);
    if (!require_input_active(self, error, error_size))
        return false;
    if (button >= MAX_TRACKED_BUTTONS) {
        snprintf(error, error_size, "%s", AS_TR("Button code is outside the supported range"));
        return false;
    }
    acquire_gate(self);
    if (self->pressed_buttons[button] != pressed) {
        zwlr_virtual_pointer_v1_button(
            self->pointer,
            monotonic_ms(),
            button,
            pressed ? WL_POINTER_BUTTON_STATE_PRESSED : WL_POINTER_BUTTON_STATE_RELEASED);
        self->pressed_buttons[button] = pressed;
    }
    zwlr_virtual_pointer_v1_frame(self->pointer);
    return flush_input(self, error, error_size);
}

bool agentseat_input_scroll(
    struct agentseat_input* input,
    bool horizontal,
    int value120,
    char* error,
    size_t error_size) {
    struct input_state* self = input_state(input);
    if (!require_input_active(self, error, error_size))
        return false;
    acquire_gate(self);
    const uint32_t axis = horizontal ? WL_POINTER_AXIS_HORIZONTAL_SCROLL : WL_POINTER_AXIS_VERTICAL_SCROLL;
    int discrete = value120 / 120;
    if (discrete == 0 && value120 != 0)
        discrete = value120 > 0 ? 1 : -1;
    zwlr_virtual_pointer_v1_axis_source(self->pointer, WL_POINTER_AXIS_SOURCE_WHEEL);
    zwlr_virtual_pointer_v1_axis_discrete(
        self->pointer,
        monotonic_ms(),
        axis,
        wl_fixed_from_double((double)value120 / 12.0),
        discrete);
    zwlr_virtual_pointer_v1_frame(self->pointer);
    return flush_input(self, error, error_size);
}

bool agentseat_input_key(
    struct agentseat_input* input,
    uint32_t keycode,
    bool pressed,
    char* error,
    size_t error_size) {
    struct input_state* self = input_state(input);
    if (!require_input_active(self, error, error_size))
        return false;
    if (keycode >= MAX_TRACKED_KEYS) {
        snprintf(error, error_size, "%s", AS_TR("Keycode is outside the supported range"));
        return false;
    }
    acquire_gate(self);
    if (self->pressed_keys[keycode] != pressed) {
        send_raw_key(self, keycode, pressed);
        self->pressed_keys[keycode] = pressed;
    }
    return flush_input(self, error, error_size);
}

bool agentseat_input_prepare_text(
    struct agentseat_input* input, const char* text,
    uint32_t** output, size_t* count, bool* unsupported,
    char* error, size_t error_size) {
    struct input_state* self = input_state(input);
    *output = NULL;
    *count = 0;
    *unsupported = false;
    if (!require_input_active(self, error, error_size))
        return false;
    const size_t length = strlen(text);
    uint32_t* codepoints = calloc(length + 1, sizeof(*codepoints));
    if (!codepoints) {
        snprintf(error, error_size, "%s", AS_TR("Cannot allocate text input buffer"));
        return false;
    }
    size_t offset = 0;
    /* Cache ASCII support within this request; long repeated text must not
     * rescan the complete XKB map for each occurrence during validation. */
    unsigned char supported_ascii[128] = {0};
    while (offset < length) {
        uint32_t codepoint = 0;
        bool valid = utf8_next((const unsigned char*)text, length, &offset, &codepoint);
        bool supported = valid && codepoint < 128 && supported_ascii[codepoint] != 0;
        if (valid && !supported)
            supported = supported_codepoint(self, codepoint);
        if (!valid || !supported) {
            *unsupported = true;
            snprintf(error, error_size, "%s", AS_TR("Text contains invalid UTF-8 or a character unavailable in the configured XKB keymap"));
            free(codepoints);
            return false;
        }
        if (codepoint < 128)
            supported_ascii[codepoint] = 1;
        codepoints[(*count)++] = codepoint;
    }
    *output = codepoints;
    return true;
}

bool agentseat_input_type_codepoint(
    struct agentseat_input* input, uint32_t codepoint,
    char* error, size_t error_size) {
    struct input_state* self = input_state(input);
    if (!require_input_active(self, error, error_size))
        return false;
    acquire_gate(self);
    if (!type_codepoint(self, self->keyboard, codepoint)) {
        snprintf(error, error_size, "%s", AS_TR("A key became unavailable while typing"));
        return false;
    }
    return flush_input(self, error, error_size);
}

void agentseat_input_release_all(struct agentseat_input* input) {
    struct input_state* self = input_state(input);
    if (self)
        release_all(self);
}

void agentseat_input_arm(struct agentseat_input* input, uint32_t epoch) {
    struct input_state* self = input_state(input);
    self->submit_epoch = epoch;
}

void agentseat_input_revoke(struct agentseat_input* input) {
    struct input_state* self = input_state(input);
    if (self && self->control) {
        agentseat_control_v1_revoke(self->control);
        release_all(self);
    }
}

void agentseat_input_hold_timeout(struct agentseat_input* input, unsigned milliseconds) {
    struct input_state* self = input_state(input);
    if (self && self->control)
        agentseat_control_v1_hold_timeout(self->control, milliseconds);
}

const struct agentseat_scene* agentseat_input_scene(const struct agentseat_input* input) {
    const struct input_state* self = input_state_const(input);
    return self ? &self->scene : NULL;
}
void agentseat_input_window_action(struct agentseat_input* input, uint32_t token,
    uint32_t window_id, uint32_t action, int first, int second) {
    struct input_state* self = input_state(input);
    acquire_gate(self);
    agentseat_control_v1_window_action(self->control, self->submit_epoch, token, window_id, action, first, second);
    wl_display_flush(self->display);
}

struct as_capture* agentseat_input_capture(struct agentseat_input* input) {
    return input_state(input)->capture;
}
json_object* agentseat_input_capture_start(struct agentseat_input* input, const char* socket_path) {
    struct input_state* self = input_state(input);
    return as_capture_start(self->capture, self->control, socket_path, self->scene.version);
}

void agentseat_input_geometry(struct agentseat_input* input, uint32_t version) {
    input_state(input)->submit_geometry = version;
}

static void authority_done(void* data, struct wl_callback* callback, uint32_t serial) {
    (void)serial;
    struct input_state* self = data;
    self->authorization_token = self->authorization_requested;
    self->authorization_epoch = self->control_epoch;
    self->authorization_allowed = self->gate_allowed;
    self->authorization = NULL;
    wl_callback_destroy(callback);
}
static const struct wl_callback_listener authority_listener = {.done = authority_done};
void agentseat_input_check_authority(struct agentseat_input* input, uint32_t token) {
    struct input_state* self = input_state(input);
    if (self->authorization) wl_callback_destroy(self->authorization);
    self->authorization_requested = token;
    acquire_gate(self);
    self->authorization = wl_display_sync(self->display);
    wl_callback_add_listener(self->authorization, &authority_listener, self);
    wl_display_flush(self->display);
}

bool agentseat_input_paste_offer(struct agentseat_input* input, uint32_t token,
    const char* text, char* error, size_t error_size) {
    struct input_state* self = input_state(input);
    if (!require_input_active(self, error, error_size)) return false;
    for (size_t i = 0; i < MAX_TRACKED_KEYS; i++) {
        if (self->pressed_keys[i]) {
            snprintf(error, error_size, "Release held virtual keys before paste"); return false;
        }
    }
    if (agentseat_control_v1_get_version(self->control) < 3) {
        snprintf(error, error_size, "Private clipboard requires microhost control version 3"); return false;
    }
    size_t size = strlen(text), offset = 0;
    uint32_t codepoint;
    while (offset < size) {
        if (!utf8_next((const unsigned char*)text, size, &offset, &codepoint)) {
            snprintf(error, error_size, "Clipboard text is not valid UTF-8"); return false;
        }
    }
    int fd = memfd_create("agentseat-clipboard", MFD_CLOEXEC | MFD_ALLOW_SEALING);
    if (fd < 0 || write(fd, text, size) != (ssize_t)size ||
        fcntl(fd, F_ADD_SEALS, F_SEAL_WRITE | F_SEAL_GROW | F_SEAL_SHRINK | F_SEAL_SEAL) < 0) {
        if (fd >= 0) close(fd);
        snprintf(error, error_size, "Cannot prepare sealed clipboard payload"); return false;
    }
    self->clipboard_token = 0; self->clipboard_status = 0;
    self->clipboard_transfers = self->clipboard_bytes = 0;
    acquire_gate(self);
    agentseat_control_v1_clipboard_offer(self->control, self->submit_epoch, token, fd, size);
    close(fd);
    /* Once queued, a flush failure is an unknown outcome; do not report a
     * pre-submission failure that could encourage a replay. Dispatch owns it. */
    wl_display_flush(self->display);
    return true;
}
