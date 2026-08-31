#define _GNU_SOURCE

#include <errno.h>
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

#include "agentseat-input.h"
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
    struct wl_registry* registry;
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

static uint32_t monotonic_ms(void) {
    struct timespec ts;
    if (clock_gettime(CLOCK_MONOTONIC, &ts) != 0)
        return 0;
    uint64_t ms = (uint64_t)ts.tv_sec * 1000ULL + (uint64_t)ts.tv_nsec / 1000000ULL;
    return (uint32_t)(ms & UINT32_MAX);
}

static void sleep_ms(unsigned ms) {
    struct timespec req = {
        .tv_sec = (time_t)(ms / 1000U),
        .tv_nsec = (long)(ms % 1000U) * 1000000L,
    };
    while (nanosleep(&req, &req) != 0 && errno == EINTR) {
    }
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
    if (value > 0x10ffff || (value >= 0xd800 && value <= 0xdfff))
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
        snprintf(error, error_size, "xkb_context_new failed");
        return false;
    }
    self->xkb_keymap = xkb_keymap_new_from_names(self->xkb_context, &names, XKB_KEYMAP_COMPILE_NO_FLAGS);
    if (!self->xkb_keymap) {
        snprintf(error, error_size, "Cannot compile XKB keymap for layout %s", names.layout);
        return false;
    }
    self->xkb_state = xkb_state_new(self->xkb_keymap);
    if (!self->xkb_state) {
        snprintf(error, error_size, "xkb_state_new failed");
        return false;
    }

    char* keymap = xkb_keymap_get_as_string(self->xkb_keymap, XKB_KEYMAP_FORMAT_TEXT_V1);
    if (!keymap) {
        snprintf(error, error_size, "Cannot serialize XKB keymap");
        return false;
    }
    size_t size = strlen(keymap) + 1;
    int fd = create_keymap_fd(keymap, size);
    free(keymap);
    if (fd < 0) {
        snprintf(error, error_size, "Cannot allocate keymap memfd: %s", strerror(errno));
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

static void registry_global(
    void* data,
    struct wl_registry* registry,
    uint32_t id,
    const char* interface,
    uint32_t version) {
    struct input_state* self = data;
    if (strcmp(interface, zwp_virtual_keyboard_manager_v1_interface.name) == 0 && !self->keyboard_manager) {
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
        snprintf(error, error_size, "An AgentSeat input scope already exists");
        return false;
    }
    if (!self->keyboard_manager || !self->pointer_manager || !self->target_global) {
        snprintf(
            error,
            error_size,
            "Required globals missing: seat=%d keyboard=%d pointer=%d",
            self->target_global != 0,
            self->keyboard_manager != NULL,
            self->pointer_manager != NULL);
        return false;
    }

    snprintf(self->seat_id, sizeof(self->seat_id), "%s", seat_id);
    snprintf(self->seat_name, sizeof(self->seat_name), "%s", name);
    bind_target_seat(self);
    if (!self->seat) {
        snprintf(error, error_size, "Cannot bind AgentSeat micro-host seat global %u", self->target_global);
        destroy_seat(self);
        return false;
    }

    self->keyboard = zwp_virtual_keyboard_manager_v1_create_virtual_keyboard(self->keyboard_manager, self->seat);
    self->pointer = self->output && self->pointer_manager_version >= 2
        ? zwlr_virtual_pointer_manager_v1_create_virtual_pointer_with_output(self->pointer_manager, self->seat, self->output)
        : zwlr_virtual_pointer_manager_v1_create_virtual_pointer(self->pointer_manager, self->seat);
    if (!self->keyboard || !self->pointer) {
        snprintf(error, error_size, "Cannot create virtual keyboard or pointer");
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
        snprintf(error, error_size, "Cannot connect to WAYLAND_DISPLAY");
        return false;
    }
    self->registry = wl_display_get_registry(self->display);
    if (!self->registry) {
        snprintf(error, error_size, "wl_display_get_registry failed");
        return false;
    }
    wl_registry_add_listener(self->registry, &registry_listener, self);
    if (wl_display_roundtrip(self->display) < 0 || wl_display_roundtrip(self->display) < 0) {
        snprintf(error, error_size, "Wayland registry roundtrip failed");
        return false;
    }
    return true;
}

static void input_state_destroy(struct input_state* self) {
    destroy_seat(self);
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
        snprintf(error, error_size, "AgentSeat input scope is not created");
        return false;
    }
    if (self->paused) {
        snprintf(error, error_size, "AgentSeat input scope is paused");
        return false;
    }
    return true;
}

struct agentseat_input* agentseat_input_open(char* error, size_t error_size) {
    struct input_state* self = calloc(1, sizeof(*self));
    if (!self) {
        snprintf(error, error_size, "Cannot allocate AgentSeat input state");
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
        snprintf(error, error_size, "AgentSeat Wayland input connection was lost");
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
        snprintf(error, error_size, "AgentSeat input scope is not created");
        return false;
    }
    release_all(self);
    self->paused = true;
    return true;
}

bool agentseat_input_resume(struct agentseat_input* input, char* error, size_t error_size) {
    struct input_state* self = input_state(input);
    if (!self || !self->created) {
        snprintf(error, error_size, "AgentSeat input scope is not created");
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
    status->created = self->created;
    status->paused = self->paused;
    status->seat_global = self->target_global;
    status->pointer_x = self->pointer_x;
    status->pointer_y = self->pointer_y;
}

static bool flush_input(struct input_state* self, char* error, size_t error_size) {
    if (wl_display_flush(self->display) >= 0)
        return true;
    snprintf(error, error_size, "Cannot flush AgentSeat input events");
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
        snprintf(error, error_size, "Pointer coordinates must be normalized from 0 to 1");
        return false;
    }
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
        snprintf(error, error_size, "Button code is outside the supported range");
        return false;
    }
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
        snprintf(error, error_size, "Keycode is outside the supported range");
        return false;
    }
    if (self->pressed_keys[keycode] != pressed) {
        send_raw_key(self, keycode, pressed);
        self->pressed_keys[keycode] = pressed;
    }
    return flush_input(self, error, error_size);
}

bool agentseat_input_type(
    struct agentseat_input* input,
    const char* text,
    int interval_ms,
    size_t* typed,
    bool* unsupported,
    char* error,
    size_t error_size) {
    struct input_state* self = input_state(input);
    *typed = 0;
    *unsupported = false;
    if (!require_input_active(self, error, error_size))
        return false;
    const unsigned char* bytes = (const unsigned char*)text;
    const size_t byte_length = strlen(text);
    uint32_t* codepoints = calloc(byte_length + 1, sizeof(*codepoints));
    if (!codepoints) {
        snprintf(error, error_size, "Cannot allocate text input buffer");
        return false;
    }
    size_t offset = 0;
    size_t count = 0;
    while (offset < byte_length) {
        uint32_t codepoint = 0;
        if (!utf8_next(bytes, byte_length, &offset, &codepoint) || !supported_codepoint(self, codepoint)) {
            *unsupported = true;
            snprintf(error, error_size, "Text contains invalid UTF-8 or a character unavailable in the configured XKB keymap");
            free(codepoints);
            return false;
        }
        codepoints[count++] = codepoint;
    }
    for (size_t i = 0; i < count; ++i) {
        if (!type_codepoint(self, self->keyboard, codepoints[i])) {
            snprintf(error, error_size, "A key became unavailable while typing");
            free(codepoints);
            return false;
        }
        if (interval_ms > 0 && i + 1 < count)
            sleep_ms((unsigned)interval_ms);
    }
    free(codepoints);
    if (wl_display_flush(self->display) < 0) {
        snprintf(error, error_size, "Cannot flush AgentSeat keyboard events");
        return false;
    }
    *typed = count;
    return true;
}
