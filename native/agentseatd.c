#define _GNU_SOURCE

#include <errno.h>
#include <json-c/json.h>
#include <math.h>
#include <poll.h>
#include <signal.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <sys/stat.h>
#include <sys/time.h>
#include <sys/types.h>
#include <sys/un.h>
#include <time.h>
#include <unistd.h>

#include "agentseat-i18n.h"
#include "agentseat-input.h"
#include "agentseat-capture.h"
#include "request-ledger.h"
#include "agentseat-semantic.h"

#define AGENTSEATD_VERSION "0.6.0"
#define MAX_REQUEST (1024U * 1024U)
#define MAX_LEASES 256
#define MAX_TYPE_BYTES 32768U
#define MAX_MESSAGES 64
#define MAX_CHAT_BYTES 2048U
#define MAX_ACTIVITY_SUMMARY 512U
#define MAX_SESSION_ID 64U
#define MAX_CLIENTS 32
#define CLIENT_IO_BUDGET 16384U
#define CLIENT_TIMEOUT_MS 1000U
#define MAX_OPERATIONS 16
#define MAX_TYPE_DURATION_MS 180000U

struct type_operation {
    char id[96];
    bool clipboard, paste_keys_sent;
    uint32_t clipboard_transfers, clipboard_bytes;
    bool semantic, semantic_sent, semantic_replied, semantic_succeeded, cancel_requested;
    char semantic_kind[40], semantic_stage[40], semantic_error[128];
    uint32_t semantic_geometry;
    uint32_t window_action, window_id, action_token;
    int action_first, action_second;
    uint32_t* codepoints;
    size_t count;
    size_t submitted;
    uint64_t next_msec;
    uint64_t deadline_msec;
    uint64_t epoch;
    unsigned interval_ms;
    bool active;
    const char* outcome;
};

struct rpc_client {
    int fd;
    char* request;
    size_t length;
    size_t capacity;
    char* response;
    size_t response_length;
    size_t written;
    uint64_t deadline_msec;
};

struct collaboration_message {
    uint64_t id;
    uint64_t created_msec;
    char role[16];
    char text[MAX_CHAT_BYTES + 1];
};

struct rpc_error {
    char code[96];
    char message[768];
};

struct daemon_state {
    const char* socket_path;
    const char* activity_socket_path;
    const char* session_id;
    const char* seat_id;
    const char* seat_name;
    int workspace;
    bool xwayland;
    int output_width;
    int output_height;
    const char* app_id;
    const char* window_title;

    int listen_fd;
    int activity_fd;
    struct agentseat_input* input;

    int human_grace_ms;
    uint64_t human_active_until_msec;
    bool human_held;
    bool gate_allowed;
    uint32_t gate_epoch;
    char activity_summary[MAX_ACTIVITY_SUMMARY + 1];
    char activity_detail[MAX_CHAT_BYTES + 1];
    struct collaboration_message messages[MAX_MESSAGES];
    size_t message_start;
    size_t message_count;
    uint64_t next_message_id;

    bool created;
    bool paused;
    bool quit;
    uint64_t started_msec;
    uint64_t request_count;
    unsigned generation;
    uint64_t control_epoch;
    uint64_t next_operation;
    struct type_operation operations[MAX_OPERATIONS];
    struct type_operation* typing;
    struct as_semantic* semantic;
    char semantic_operation_id[96];
    struct as_request_ledger requests;
    char writer_id[129];
    char writer_lease[96];
    uint64_t writer_deadline;
    uint64_t next_lease;
    uint32_t scene_version;
    uint32_t denied_windows[MAX_LEASES];
    size_t denied_count;
    double pointer_x;
    double pointer_y;
    char focused_window[64];
    char* leases[MAX_LEASES];
    size_t lease_count;
};

static volatile sig_atomic_t signal_stop = 0;

static uint64_t monotonic_msec(void) {
    struct timespec now = {0};
    clock_gettime(CLOCK_MONOTONIC, &now);
    return (uint64_t)now.tv_sec * 1000U + (uint64_t)now.tv_nsec / 1000000U;
}

static int human_remaining_ms(const struct daemon_state* state) {
    const uint64_t now = monotonic_msec();
    if (state->human_active_until_msec <= now)
        return state->human_held ? 1 : 0;
    const uint64_t remaining = state->human_active_until_msec - now;
    return remaining > INT32_MAX ? INT32_MAX : (int)remaining;
}

static void on_signal(int signal_number) {
    (void)signal_number;
    signal_stop = 1;
}

static void error_set(struct rpc_error* error, const char* code, const char* message) {
    snprintf(error->code, sizeof(error->code), "%s", code ? code : "AGENTSEAT_ERROR");
    snprintf(
        error->message,
        sizeof(error->message),
        "%s",
        message ? message : AS_TR("AgentSeat request failed"));
}

static json_object* delivered_result(void) {
    json_object* result = json_object_new_object();
    json_object_object_add(result, "submitted", json_object_new_boolean(true));
    json_object_object_add(result, "verification", json_object_new_string("not_requested"));
    return result;
}

static void input_failure(struct rpc_error* error, const char* code, const char* message) {
    error_set(error, code, message && message[0] ? message : AS_TR("Native AgentSeat input failed"));
}

static const char* string_param(json_object* params, const char* key, bool allow_empty, struct rpc_error* error) {
    json_object* value = NULL;
    if (!json_object_object_get_ex(params, key, &value) || !json_object_is_type(value, json_type_string)) {
        error_set(error, "INVALID_PARAMS", AS_TR("Required string parameter is missing"));
        return NULL;
    }
    const char* text = json_object_get_string(value);
    if (!allow_empty && !text[0]) {
        error_set(error, "INVALID_PARAMS", AS_TR("String parameter must not be empty"));
        return NULL;
    }
    return text;
}

static bool bool_param(json_object* params, const char* key, bool fallback, bool* output, struct rpc_error* error) {
    json_object* value = NULL;
    if (!json_object_object_get_ex(params, key, &value)) {
        *output = fallback;
        return true;
    }
    if (!json_object_is_type(value, json_type_boolean)) {
        error_set(error, "INVALID_PARAMS", AS_TR("Boolean parameter has the wrong type"));
        return false;
    }
    *output = json_object_get_boolean(value);
    return true;
}

static const char* optional_string_param(json_object* params, const char* key, struct rpc_error* error) {
    json_object* value = NULL;
    if (!json_object_object_get_ex(params, key, &value))
        return "";
    if (!json_object_is_type(value, json_type_string)) {
        error_set(error, "INVALID_PARAMS", AS_TR("Optional string parameter has the wrong type"));
        return NULL;
    }
    return json_object_get_string(value);
}

static struct collaboration_message* message_append(struct daemon_state* state, const char* role, const char* text) {
    size_t index;
    if (state->message_count < MAX_MESSAGES) {
        index = (state->message_start + state->message_count) % MAX_MESSAGES;
        state->message_count++;
    } else {
        index = state->message_start;
        state->message_start = (state->message_start + 1) % MAX_MESSAGES;
    }
    struct collaboration_message* message = &state->messages[index];
    memset(message, 0, sizeof(*message));
    message->id = ++state->next_message_id;
    message->created_msec = monotonic_msec();
    snprintf(message->role, sizeof(message->role), "%s", role);
    snprintf(message->text, sizeof(message->text), "%s", text);
    return message;
}

static json_object* message_json(const struct collaboration_message* message) {
    json_object* value = json_object_new_object();
    json_object_object_add(value, "id", json_object_new_int64((int64_t)message->id));
    json_object_object_add(value, "created_msec", json_object_new_int64((int64_t)message->created_msec));
    json_object_object_add(value, "role", json_object_new_string(message->role));
    json_object_object_add(value, "text", json_object_new_string(message->text));
    return value;
}

static json_object* collaboration_json(const struct daemon_state* state) {
    const int remaining = human_remaining_ms(state);
    json_object* result = json_object_new_object();
    json_object_object_add(result, "human_active", json_object_new_boolean(remaining > 0));
    json_object_object_add(result, "human_quiet_in_ms", json_object_new_int(remaining));
    json_object_object_add(result, "human_priority_grace_ms", json_object_new_int(state->human_grace_ms));
    json_object_object_add(result, "agent_allowed", json_object_new_boolean(state->created && !state->paused && remaining == 0));
    json_object_object_add(result, "seat_paused", json_object_new_boolean(state->paused));
    json_object_object_add(result, "activity_summary", json_object_new_string(state->activity_summary));
    json_object_object_add(result, "activity_detail", json_object_new_string(state->activity_detail));
    json_object_object_add(result, "unread_messages", json_object_new_int64((int64_t)state->message_count));
    json_object_object_add(result, "last_message_id", json_object_new_int64((int64_t)state->next_message_id));
    return result;
}

static bool lease_contains(const struct daemon_state* state, const char* window_id) {
    for (size_t i = 0; i < state->lease_count; ++i) {
        if (strcmp(state->leases[i], window_id) == 0)
            return true;
    }
    return false;
}

static bool lease_add(struct daemon_state* state, const char* window_id) {
    if (lease_contains(state, window_id))
        return true;
    if (state->lease_count >= MAX_LEASES)
        return false;
    state->leases[state->lease_count] = strdup(window_id);
    if (!state->leases[state->lease_count])
        return false;
    state->lease_count++;
    return true;
}

static void lease_remove(struct daemon_state* state, const char* window_id) {
    for (size_t i = 0; i < state->lease_count; ++i) {
        if (strcmp(state->leases[i], window_id) != 0)
            continue;
        free(state->leases[i]);
        memmove(&state->leases[i], &state->leases[i + 1], (state->lease_count - i - 1) * sizeof(state->leases[0]));
        state->lease_count--;
        return;
    }
}

static void leases_clear(struct daemon_state* state) {
    for (size_t i = 0; i < state->lease_count; ++i)
        free(state->leases[i]);
    state->lease_count = 0;
    state->focused_window[0] = '\0';
}

static bool require_created(struct daemon_state* state, struct rpc_error* error) {
    if (!state->created) {
        error_set(error, "SEAT_NOT_CREATED", AS_TR("Call seat.create before using AgentSeat"));
        return false;
    }
    return true;
}

static bool require_active(struct daemon_state* state, struct rpc_error* error) {
    if (!require_created(state, error))
        return false;
    if (state->paused) {
        error_set(error, "SEAT_PAUSED", AS_TR("Agent seat is paused"));
        return false;
    }
    const int remaining = human_remaining_ms(state);
    if (remaining > 0) {
        char message[160] = {0};
        snprintf(
            message,
            sizeof(message),
            AS_TR("Human input has priority; retry after %d ms of quiet"),
            remaining);
        error_set(error, "HUMAN_ACTIVE", message);
        return false;
    }
    return true;
}

static bool require_focus_lease(struct daemon_state* state, struct rpc_error* error) {
    if (!require_active(state, error))
        return false;
    if (!state->focused_window[0] || !lease_contains(state, state->focused_window)) {
        error_set(error, "NO_FOCUS", AS_TR("Focus a leased window before sending input"));
        return false;
    }
    return true;
}

static json_object* seat_json(const struct daemon_state* state) {
    json_object* seat = json_object_new_object();
    json_object_object_add(seat, "seat_id", json_object_new_string(state->seat_id));
    json_object_object_add(seat, "name", json_object_new_string(state->seat_name));
    json_object_object_add(seat, "created", json_object_new_boolean(state->created));
    json_object_object_add(seat, "paused", json_object_new_boolean(state->paused));
    json_object_object_add(seat, "workspace", json_object_new_int(state->workspace));
    if (state->focused_window[0])
        json_object_object_add(seat, "focused_window", json_object_new_string(state->focused_window));
    else
        json_object_object_add(seat, "focused_window", NULL);
    json_object_object_add(seat, "pointer_x", json_object_new_double(state->pointer_x));
    json_object_object_add(seat, "pointer_y", json_object_new_double(state->pointer_y));
    json_object_object_add(seat, "pressed_keys", json_object_new_array());
    json_object_object_add(seat, "pressed_buttons", json_object_new_array());
    json_object_object_add(seat, "generation", json_object_new_int64(state->generation));
    json_object_object_add(seat, "creator_pid", json_object_new_int64(getpid()));
    json_object* policy = json_object_new_object();
    json_object_object_add(policy, "native_wayland_only", json_object_new_boolean(false));
    json_object_object_add(policy, "allow_xwayland", json_object_new_boolean(true));
    json_object_object_add(policy, "allow_layer_shell", json_object_new_boolean(false));
    json_object_object_add(policy, "allow_host_clipboard", json_object_new_boolean(false));
    json_object_object_add(policy, "allow_private_clipboard_paste", json_object_new_boolean(true));
    json_object_object_add(policy, "allow_drag_and_drop", json_object_new_boolean(false));
    json_object_object_add(policy, "capture_scope", json_object_new_string("leased-windows"));
    json_object_object_add(seat, "policy", policy);
    return seat;
}

static json_object* leases_json(const struct daemon_state* state) {
    json_object* leases = json_object_new_array();
    for (size_t i = 0; i < state->lease_count; ++i) {
        json_object* lease = json_object_new_object();
        json_object_object_add(lease, "window_id", json_object_new_string(state->leases[i]));
        json_object_object_add(lease, "seat_id", json_object_new_string(state->seat_id));
        json_object_object_add(lease, "workspace", json_object_new_int(state->workspace));
        json_object_object_add(lease, "owner_pid", NULL);
        json_object_object_add(lease, "allow_keyboard", json_object_new_boolean(true));
        json_object_object_add(lease, "allow_pointer", json_object_new_boolean(true));
        json_object_object_add(lease, "allow_capture", json_object_new_boolean(true));
        json_object_array_add(leases, lease);
    }
    return leases;
}

static const struct agentseat_window* find_window(const struct daemon_state* state, const char* id) {
    const struct agentseat_scene* scene = agentseat_input_scene(state->input);
    if (!scene)
        return NULL;
    for (size_t i = 0; i < scene->count; ++i) {
        char key[64];
        snprintf(key, sizeof(key), "w-%u", scene->windows[i].id);
        if (strcmp(id, key) == 0 || (strcmp(id, "agentseat:root") == 0 && (scene->windows[i].flags & 1)))
            return &scene->windows[i];
    }
    return NULL;
}

static json_object* output_json(const struct daemon_state* state) {
    const struct agentseat_scene* scene = agentseat_input_scene(state->input);
    json_object* result = json_object_new_object();
    json_object_object_add(result, "width", json_object_new_int(scene->width));
    json_object_object_add(result, "height", json_object_new_int(scene->height));
    json_object_object_add(result, "pixel_width", json_object_new_int(scene->pixel_width));
    json_object_object_add(result, "pixel_height", json_object_new_int(scene->pixel_height));
    json_object_object_add(result, "scale", json_object_new_double(scene->scale_120 / 120.0));
    json_object_object_add(result, "geometry_version", json_object_new_int64(scene->version));
    json_object_object_add(result, "truncated", json_object_new_boolean(scene->truncated));
    struct agentseat_input_status status = {0};
    agentseat_input_status(state->input, &status);
    /* Content serial advances on damaged commits only (protocol v4). */
    json_object_object_add(result, "content_serial", status.control_protocol_version >= 4 ? json_object_new_int64(status.content_serial) : NULL);
    json_object_object_add(result, "content_age_ms", status.control_protocol_version >= 4 ? json_object_new_int64(status.content_age_ms) : NULL);
    return result;
}

static json_object* windows_json(const struct daemon_state* state) {
    const struct agentseat_scene* scene = agentseat_input_scene(state->input);
    json_object* windows = json_object_new_array();
    for (size_t i = 0; scene && i < scene->count; ++i) {
        const struct agentseat_window* item = &scene->windows[i];
        char id[64], parent[64];
        snprintf(id, sizeof(id), "w-%u", item->id);
        snprintf(parent, sizeof(parent), "w-%u", item->parent_id);
        json_object* window = json_object_new_object();
        json_object_object_add(window, "id", json_object_new_string(id));
        json_object_object_add(window, "parent_id", item->parent_id ? json_object_new_string(parent) : NULL);
        json_object_object_add(window, "title", json_object_new_string(item->title));
        json_object_object_add(window, "app_id", json_object_new_string(item->app_id));
        json_object_object_add(window, "class", json_object_new_string(item->app_id));
        json_object_object_add(window, "pid", item->pid ? json_object_new_int64(item->pid) : NULL);
        json_object_object_add(window, "pid_verified", json_object_new_boolean(item->flags & 4));
        json_object_object_add(window, "popup", json_object_new_boolean(item->flags & 8));
        json_object_object_add(window, "primary", json_object_new_boolean(item->flags & 1));
        json_object_object_add(window, "workspace", json_object_new_int(state->workspace));
        json_object_object_add(window, "x", json_object_new_int(item->x));
        json_object_object_add(window, "y", json_object_new_int(item->y));
        json_object_object_add(window, "width", json_object_new_int(item->width));
        json_object_object_add(window, "height", json_object_new_int(item->height));
        json_object_object_add(window, "geometry_version", json_object_new_int64(scene->version));
        json_object_object_add(window, "coordinate_space", json_object_new_string("output-logical"));
        json_object_object_add(window, "mapped", json_object_new_boolean(true));
        json_object_object_add(window, "hidden", json_object_new_boolean(false));
        json_object_object_add(window, "xwayland", json_object_new_boolean(item->flags & 2));
        json_object_object_add(window, "leased", json_object_new_boolean(lease_contains(state, id)));
        json_object_object_add(window, "focused", json_object_new_boolean(item->id == scene->focused_id));
        json_object_array_add(windows, window);
    }
    return windows;
}

static void seat_destroy(struct daemon_state* state) {
    if (state->created)
        agentseat_input_destroy(state->input);
    leases_clear(state);
    state->created = false;
    state->paused = true;
}

static bool method_is_write(const char* method) {
    const char* writes[] = {
        "seat.create", "seat.destroy", "seat.pause", "seat.resume", "shutdown",
        "window.geometry", "window.lease", "window.release", "window.focus",
        "window.move", "window.resize", "window.close", "pointer.move_absolute", "pointer.button",
        "pointer.click", "pointer.scroll", "keyboard.key", "keyboard.type", "keyboard.paste",
        "operation.cancel", "activity.set", "chat.send", "chat.reply",
        "control.acquire", "control.renew", "control.release",
        "a11y.perform", "a11y.set_text", "a11y.set_value", "a11y.focus", "a11y.select", "a11y.scroll_to",
    };
    for (size_t i = 0; i < sizeof(writes) / sizeof(writes[0]); ++i)
        if (strcmp(method, writes[i]) == 0)
            return true;
    return false;
}

static bool method_is_semantic(const char* method) {
    return !strcmp(method, "a11y.perform") || !strcmp(method, "a11y.set_text") || !strcmp(method, "a11y.set_value") ||
        !strcmp(method, "a11y.focus") || !strcmp(method, "a11y.select") || !strcmp(method, "a11y.scroll_to");
}

static bool method_sends_input(const char* method) {
    return strncmp(method, "pointer.", 8) == 0 || strncmp(method, "keyboard.", 9) == 0;
}

static json_object* operation_json(const struct type_operation* operation) {
    json_object* result = json_object_new_object();
    json_object_object_add(result, "operation_id", json_object_new_string(operation->id));
    json_object_object_add(result, "execution", json_object_new_string(operation->active ? "running" : operation->outcome));
    json_object_object_add(result, "submitted", json_object_new_int64((int64_t)operation->submitted));
    json_object_object_add(result, "total", json_object_new_int64((int64_t)operation->count));
    json_object_object_add(result, "control_epoch", json_object_new_int64((int64_t)operation->epoch));
    json_object_object_add(result, "verification", json_object_new_string(operation->window_action && !operation->active && strcmp(operation->outcome, "finished") == 0 ? "compositor_confirmed" : "not_requested"));
    json_object_object_add(result, "kind", json_object_new_string(operation->window_action ? "window.action" : "keyboard.type"));
    if (operation->window_action == 4 && !operation->active && strcmp(operation->outcome, "finished") == 0)
        json_object_object_add(result, "verification", json_object_new_string("close_request_delivered"));
    json_object_object_add(result, "replay_safe", json_object_new_boolean(false));
    if (operation->clipboard) {
        json_object_object_add(result, "kind", json_object_new_string("keyboard.paste"));
        json_object_object_add(result, "clipboard_transfers", json_object_new_int64(operation->clipboard_transfers));
        json_object_object_add(result, "clipboard_bytes", json_object_new_int64(operation->clipboard_bytes));
        json_object_object_add(result, "paste_keys_submitted", json_object_new_boolean(operation->paste_keys_sent));
        json_object_object_add(result, "verification", json_object_new_string(operation->clipboard_transfers ? "clipboard_transport_only" : "not_requested"));
        json_object_object_add(result, "host_clipboard", json_object_new_string("untouched"));
    }
    if (operation->semantic) {
        json_object_object_add(result, "kind", json_object_new_string(operation->semantic_kind));
        json_object_object_add(result, "stage", json_object_new_string(operation->semantic_stage));
        json_object_object_add(result, "submitted", json_object_new_int(operation->semantic_sent ? 1 : 0));
        json_object_object_add(result, "application_replied", json_object_new_boolean(operation->semantic_replied));
        json_object_object_add(result, "method_succeeded", operation->semantic_replied ? json_object_new_boolean(operation->semantic_succeeded) : NULL);
        json_object_object_add(result, "cancel_requested", json_object_new_boolean(operation->cancel_requested));
        json_object_object_add(result, "may_have_side_effects", json_object_new_boolean(operation->semantic_sent));
        json_object_object_add(result, "verification", json_object_new_string("not_requested"));
        json_object_object_add(result, "error_kind", operation->semantic_error[0] ? json_object_new_string(operation->semantic_error) : NULL);
    }
    return result;
}

static void finish_typing(struct daemon_state* state, const char* outcome) {
    if (!state->typing)
        return;
    if (state->typing->semantic) {
        struct as_semantic_status semantic = {0};
        as_semantic_status(state->semantic, &semantic);
        if (semantic.busy) {
            as_semantic_cancel(state->semantic);
            state->typing->cancel_requested = true;
        }
        if (semantic.sent && strcmp(outcome, "finished") && strcmp(outcome, "rejected")) outcome = "outcome_unknown";
    }
    state->typing->active = false;
    state->typing->outcome = (state->typing->window_action || state->typing->clipboard) && state->typing->submitted && strcmp(outcome, "finished") && strcmp(outcome, "rejected") ? "outcome_unknown" : outcome;
    free(state->typing->codepoints);
    state->typing->codepoints = NULL;
    state->typing = NULL;
}

static void revoke_input(struct daemon_state* state) {
    state->control_epoch++;
    state->writer_id[0] = state->writer_lease[0] = '\0';
    state->writer_deadline = 0;
    finish_typing(state, "cancelled");
    agentseat_input_revoke(state->input);
}

static void sync_control_state(struct daemon_state* state) {
    struct agentseat_input_status status = {0};
    agentseat_input_status(state->input, &status);
    if (status.control_epoch > state->control_epoch) {
        state->control_epoch = status.control_epoch;
        state->writer_id[0] = state->writer_lease[0] = '\0';
        state->writer_deadline = 0;
        finish_typing(state, "cancelled");
        agentseat_input_release_all(state->input);
    }
    state->human_active_until_msec = monotonic_msec() + status.human_quiet_ms;
    state->human_held = status.human_held;
    state->gate_allowed = status.gate_allowed;
    state->gate_epoch = status.control_epoch;
    const struct agentseat_scene* scene = agentseat_input_scene(state->input);
    if (scene && scene->version != state->scene_version) {
        state->scene_version = scene->version;
        state->output_width = scene->width;
        state->output_height = scene->height;
        leases_clear(state);
        for (size_t i = 0; state->created && i < scene->count; ++i) {
            bool denied = false;
            for (size_t j = 0; j < state->denied_count; ++j)
                denied |= state->denied_windows[j] == scene->windows[i].id;
            char id[64];
            snprintf(id, sizeof(id), "w-%u", scene->windows[i].id);
            if (!denied)
                lease_add(state, id);
        }
        if (scene->focused_id)
            snprintf(state->focused_window, sizeof(state->focused_window), "w-%u", scene->focused_id);
        else
            state->focused_window[0] = '\0';
    }
    struct type_operation* operation = state->typing;
    if (operation && operation->window_action && status.window_action_token == operation->action_token) {
        if (status.window_action_status) {
            finish_typing(state, "rejected");
        } else {
            char id[64];
            snprintf(id, sizeof(id), "w-%u", operation->window_id);
            const struct agentseat_window* window = find_window(state, id);
            bool confirmed = operation->window_action == 4 || (window && (
                (operation->window_action == 1 && scene->focused_id == operation->window_id) ||
                (operation->window_action == 2 && window->x == operation->action_first && window->y == operation->action_second) ||
                (operation->window_action == 3 && window->width == operation->action_first && window->height == operation->action_second)));
            if (confirmed)
                finish_typing(state, "finished");
        }
    }
}

static void advance_typing(struct daemon_state* state) {
    struct type_operation* operation = state->typing;
    if (!operation)
        return;
    uint64_t now = monotonic_msec();
    if (state->paused || human_remaining_ms(state) > 0 || operation->epoch != state->control_epoch) {
        finish_typing(state, "cancelled");
        return;
    }
    if (now >= operation->deadline_msec) {
        finish_typing(state, "deadline_exceeded");
        return;
    }
    if (operation->clipboard) {
        struct agentseat_input_status status = {0};
        agentseat_input_status(state->input, &status);
        if (status.clipboard_token != operation->action_token) return;
        if (status.clipboard_status) { finish_typing(state, "rejected"); return; }
        if (!operation->paste_keys_sent) {
            char error[256] = {0};
            operation->paste_keys_sent = true;
            agentseat_input_arm(state->input, (uint32_t)operation->epoch);
            if (!agentseat_input_key(state->input, 29, true, error, sizeof(error)) ||
                !agentseat_input_key(state->input, 47, true, error, sizeof(error)) ||
                !agentseat_input_key(state->input, 47, false, error, sizeof(error)) ||
                !agentseat_input_key(state->input, 29, false, error, sizeof(error))) {
                revoke_input(state); return;
            }
        }
        operation->clipboard_transfers = status.clipboard_transfers;
        operation->clipboard_bytes = status.clipboard_bytes;
        /* Keep the source alive after this transport receipt. Callers verify
         * the application before releasing control; later reads stay served. */
        if (status.clipboard_transfers) finish_typing(state, "finished");
        return;
    }
    if (operation->semantic || operation->window_action || now < operation->next_msec)
        return;
    char message[512] = {0};
    /* One codepoint per turn, including interval=0. Socket and intervention
     * processing always gets a turn before more text is submitted. */
    agentseat_input_arm(state->input, (uint32_t)operation->epoch);
    if (!agentseat_input_type_codepoint(state->input, operation->codepoints[operation->submitted], message, sizeof(message))) {
        finish_typing(state, "outcome_unknown");
        agentseat_input_release_all(state->input);
        return;
    }
    operation->submitted++;
    if (operation->submitted == operation->count)
        finish_typing(state, "finished");
    else
        operation->next_msec = monotonic_msec() + operation->interval_ms;
}

static json_object* writer_json(struct daemon_state* state) {
    json_object* result = json_object_new_object();
    json_object_object_add(result, "controller_id", json_object_new_string(state->writer_id));
    json_object_object_add(result, "lease_id", json_object_new_string(state->writer_lease));
    json_object_object_add(result, "expires_msec", json_object_new_int64((int64_t)state->writer_deadline));
    json_object_object_add(result, "control_epoch", json_object_new_int64((int64_t)state->control_epoch));
    return result;
}

static bool require_writer(struct daemon_state* state, json_object* params, struct rpc_error* error) {
    const char* controller = string_param(params, "controller_id", false, error);
    const char* lease = string_param(params, "lease_id", false, error);
    if (!controller || !lease)
        return false;
    if (!state->writer_id[0] || monotonic_msec() >= state->writer_deadline ||
        strcmp(controller, state->writer_id) != 0 || strcmp(lease, state->writer_lease) != 0) {
        error_set(error, "NO_WRITE_LEASE", "Acquire a current write lease before changing the application");
        return false;
    }
    return true;
}

static bool method_needs_writer(const char* method) {
    return method_is_semantic(method) || method_sends_input(method) || strcmp(method, "window.focus") == 0 ||
        strcmp(method, "window.move") == 0 || strcmp(method, "window.resize") == 0 || strcmp(method, "window.close") == 0;
}

static bool semantic_scope_complete(struct daemon_state* state) {
    const struct agentseat_scene* scene = agentseat_input_scene(state->input);
    if (!scene || scene->truncated || !scene->count) return false;
    for (size_t i = 0; i < scene->count; ++i) {
        char id[64]; snprintf(id, sizeof(id), "w-%u", scene->windows[i].id);
        if (!lease_contains(state, id)) return false;
    }
    return true;
}

static void advance_semantic(struct daemon_state* state) {
    if (!state->semantic || !state->semantic_operation_id[0]) return;
    struct type_operation* operation = NULL;
    for (size_t i = 0; i < MAX_OPERATIONS; ++i)
        if (!strcmp(state->operations[i].id, state->semantic_operation_id)) operation = &state->operations[i];
    if (!operation) return; /* A bounded receipt can be evicted while a late reply remains. */
    struct as_semantic_status status;
    as_semantic_status(state->semantic, &status);
    operation->semantic_sent = status.sent;
    operation->submitted = status.sent ? 1 : 0;
    operation->semantic_replied = status.replied;
    operation->semantic_succeeded = status.succeeded;
    operation->cancel_requested |= status.cancelled;
    snprintf(operation->semantic_stage, sizeof(operation->semantic_stage), "%s", status.stage);
    snprintf(operation->semantic_error, sizeof(operation->semantic_error), "%s", status.error);
    if (!operation->active) return;
    if (!status.busy) {
        finish_typing(state, status.stage);
        return;
    }
    if (state->paused || human_remaining_ms(state) > 0 || operation->epoch != state->control_epoch ||
        !state->writer_id[0] || monotonic_msec() >= state->writer_deadline || !semantic_scope_complete(state)) {
        finish_typing(state, "cancelled");
        return;
    }
    if (!status.ready) return;
    if (operation->semantic_geometry && operation->semantic_geometry != agentseat_input_scene(state->input)->version) {
        snprintf(operation->semantic_error, sizeof(operation->semantic_error), "STALE_GEOMETRY");
        finish_typing(state, "rejected"); return;
    }
    if (!operation->action_token) {
        operation->action_token = (uint32_t)state->next_operation;
        if (!operation->action_token) operation->action_token = 1;
        agentseat_input_arm(state->input, (uint32_t)operation->epoch);
        agentseat_input_geometry(state->input, operation->semantic_geometry);
        agentseat_input_check_authority(state->input, operation->action_token);
        snprintf(operation->semantic_stage, sizeof(operation->semantic_stage), "awaiting_compositor");
        return;
    }
    struct agentseat_input_status input;
    agentseat_input_status(state->input, &input);
    if (input.authorization_token != operation->action_token) return;
    if (!input.authorization_allowed || input.authorization_epoch != operation->epoch ||
        !input.gate_allowed || input.human_held || input.control_epoch != operation->epoch) {
        finish_typing(state, "cancelled"); return;
    }
    /* This is the single commit point. A later pause cannot recall a method
     * already queued to the application; its receipt stays explicit about it. */
    as_semantic_submit(state->semantic);
    as_semantic_status(state->semantic, &status);
    operation->semantic_sent = status.sent;
    operation->submitted = status.sent ? 1 : 0;
    snprintf(operation->semantic_stage, sizeof(operation->semantic_stage), "%s", status.stage);
}

static json_object* dispatch(struct daemon_state* state, struct rpc_error* error, const char* method, json_object* params) {
    if (method_is_semantic(method)) {
        if (!require_active(state, error)) return NULL;
        if (!semantic_scope_complete(state)) {
            error_set(error, "SEMANTIC_SCOPE_INCOMPLETE", "All windows in the private application must be included for semantic writes"); return NULL;
        }
        const char* ref = string_param(params, "node_ref", false, error);
        if (!ref || strlen(ref) > 200) {
            if (ref) error_set(error, "INVALID_PARAMS", "node_ref is too long");
            return NULL;
        }
        json_object* timeout = NULL;
        if (json_object_object_get_ex(params, "timeout_ms", &timeout) &&
            (!json_object_is_type(timeout, json_type_int) || json_object_get_int64(timeout) < 100 || json_object_get_int64(timeout) > 10000)) {
            error_set(error, "INVALID_PARAMS", "timeout_ms must be 100 to 10000"); return NULL;
        }
        if (!state->semantic) {
            struct ucred peer; socklen_t size = sizeof(peer);
            if (getsockopt(agentseat_input_fd(state->input), SOL_SOCKET, SO_PEERCRED, &peer, &size) || peer.uid != getuid()) {
                error_set(error, "COMPOSITOR_IDENTITY_UNAVAILABLE", "Cannot identify the owned compositor"); return NULL;
            }
            state->semantic = as_semantic_new(state->socket_path, state->session_id, peer.pid);
        }
        if (!as_semantic_start(state->semantic, method, params)) {
            error_set(error, "SEMANTIC_BUSY", "A previous semantic call still has a pending reply"); return NULL;
        }
        uint64_t sequence = ++state->next_operation;
        struct type_operation* operation = &state->operations[sequence % MAX_OPERATIONS];
        *operation = (struct type_operation){.semantic = true, .count = 1, .active = true,
            .outcome = "running", .epoch = state->control_epoch,
            .next_msec = monotonic_msec() + 15000, .deadline_msec = monotonic_msec() + 15000};
        json_object* geometry = NULL;
        if (json_object_object_get_ex(params, "geometry_version", &geometry)) operation->semantic_geometry = json_object_get_int64(geometry);
        snprintf(operation->id, sizeof(operation->id), "%s:%llu", state->session_id, (unsigned long long)sequence);
        snprintf(operation->semantic_kind, sizeof(operation->semantic_kind), "%s", method);
        snprintf(operation->semantic_stage, sizeof(operation->semantic_stage), "preparing");
        snprintf(state->semantic_operation_id, sizeof(state->semantic_operation_id), "%s", operation->id);
        state->typing = operation;
        return operation_json(operation);
    }
    if (strcmp(method, "capture.submit") == 0) {
        if (!require_created(state, error)) return NULL;
        if (!state->lease_count) {
            error_set(error, "NO_CAPTURE_SCOPE", "No application windows are leased"); return NULL;
        }
        const struct agentseat_scene* scene = agentseat_input_scene(state->input);
        if (scene->truncated) {
            error_set(error, "CAPTURE_SCOPE_INCOMPLETE", "Window registry exceeds the scope budget"); return NULL;
        }
        for (size_t i = 0; i < scene->count; ++i) {
            char id[64]; snprintf(id, sizeof(id), "w-%u", scene->windows[i].id);
            if (!lease_contains(state, id)) {
                error_set(error, "CAPTURE_SCOPE_MISMATCH", "Output includes an unleased window"); return NULL;
            }
        }
        json_object* result = agentseat_input_capture_start(state->input, state->socket_path);
        if (!result) error_set(error, "CAPTURE_BUSY", "Frame capture is busy or unavailable");
        else json_object_object_add(result, "session_id", json_object_new_string(state->session_id));
        return result;
    }
    if (strcmp(method, "capture.get") == 0) {
        const char* id = string_param(params, "frame_id", false, error);
        if (!id) return NULL;
        json_object* result = as_capture_get(agentseat_input_capture(state->input), id);
        if (!result) error_set(error, "FRAME_NOT_FOUND", "Frame receipt is absent or expired");
        else json_object_object_add(result, "session_id", json_object_new_string(state->session_id));
        return result;
    }
    if (strcmp(method, "content.changes") == 0) {
        json_object* after = NULL;
        if (!json_object_object_get_ex(params, "after_serial", &after) || !json_object_is_type(after, json_type_int) ||
            json_object_get_int64(after) < 0 || json_object_get_int64(after) > UINT32_MAX) {
            error_set(error, "INVALID_PARAMS", "after_serial must be an integer from 0 to 4294967295"); return NULL;
        }
        struct agentseat_input_status status = {0};
        agentseat_input_status(state->input, &status);
        if (status.control_protocol_version < 4) {
            error_set(error, "UNSUPPORTED", "The microhost does not report content changes"); return NULL;
        }
        struct agentseat_content_change changes[AGENTSEAT_CONTENT_HISTORY];
        uint32_t latest = 0; bool gap = false;
        size_t count = agentseat_input_content_changes(state->input, (uint32_t)json_object_get_int64(after),
            changes, AGENTSEAT_CONTENT_HISTORY, &latest, &gap);
        uint64_t now = monotonic_msec();
        json_object* result = json_object_new_object();
        json_object* list = json_object_new_array();
        for (size_t i = 0; i < count; ++i) {
            json_object* item = json_object_new_object();
            json_object_object_add(item, "serial", json_object_new_int64(changes[i].serial));
            json_object_object_add(item, "x", json_object_new_int(changes[i].x));
            json_object_object_add(item, "y", json_object_new_int(changes[i].y));
            json_object_object_add(item, "width", json_object_new_int(changes[i].width));
            json_object_object_add(item, "height", json_object_new_int(changes[i].height));
            json_object_object_add(item, "area", json_object_new_int64(changes[i].area));
            json_object_object_add(item, "age_ms", json_object_new_int64((int64_t)((uint32_t)now - changes[i].msec)));
            json_object_array_add(list, item);
        }
        json_object_object_add(result, "content_serial", json_object_new_int64(latest));
        json_object_object_add(result, "gap", json_object_new_boolean(gap));
        json_object_object_add(result, "changes", list);
        json_object_object_add(result, "output", output_json(state));
        json_object_object_add(result, "session_id", json_object_new_string(state->session_id));
        return result;
    }
    if (strcmp(method, "request.get") == 0) {
        const char* owner = string_param(params, "controller_id", false, error);
        json_object* sequence = NULL;
        if (!owner)
            return NULL;
        if (!json_object_object_get_ex(params, "request_id", &sequence) ||
            !json_object_is_type(sequence, json_type_int) || json_object_get_int64(sequence) <= 0) {
            error_set(error, "INVALID_PARAMS", "request_id must be a positive monotonically increasing integer");
            return NULL;
        }
        json_object* receipt = as_request_get(&state->requests, owner, (uint64_t)json_object_get_int64(sequence));
        if (!receipt) {
            error_set(error, "REQUEST_NOT_FOUND", "Receipt is absent or expired; this is not proof of no effect");
            return NULL;
        }
        return json_object_get(receipt);
    }
    if (strcmp(method, "control.status") == 0)
        return writer_json(state);
    if (strcmp(method, "control.acquire") == 0 || strcmp(method, "control.renew") == 0) {
        if (!require_active(state, error))
            return NULL;
        const char* owner = string_param(params, "controller_id", false, error);
        if (!owner)
            return NULL;
        json_object* ttl_value = NULL;
        bool has_ttl = json_object_object_get_ex(params, "ttl_ms", &ttl_value);
        int64_t ttl = has_ttl ? json_object_get_int64(ttl_value) : 10000;
        if ((has_ttl && !json_object_is_type(ttl_value, json_type_int)) || ttl < 100 || ttl > 30000) {
            error_set(error, "INVALID_PARAMS", "ttl_ms must be an integer from 100 to 30000");
            return NULL;
        }
        if (strcmp(method, "control.renew") == 0 && !require_writer(state, params, error))
            return NULL;
        if (state->writer_id[0] && strcmp(state->writer_id, owner) != 0) {
            error_set(error, "WRITE_LEASE_BUSY", "Another controller owns the application write lease");
            return NULL;
        }
        if (!state->writer_id[0]) {
            snprintf(state->writer_id, sizeof(state->writer_id), "%s", owner);
            snprintf(state->writer_lease, sizeof(state->writer_lease), "%s:l%llu", state->session_id,
                (unsigned long long)++state->next_lease);
        }
        state->writer_deadline = monotonic_msec() + (uint64_t)ttl;
        return writer_json(state);
    }
    if (strcmp(method, "control.release") == 0) {
        if (!require_writer(state, params, error))
            return NULL;
        revoke_input(state);
        return writer_json(state);
    }
    if (strcmp(method, "version") == 0) {
        json_object* result = json_object_new_object();
        json_object_object_add(result, "name", json_object_new_string("agentseatd"));
        json_object_object_add(result, "version", json_object_new_string(AGENTSEATD_VERSION));
        json_object_object_add(result, "api_version", json_object_new_string("2"));
        json_object_object_add(result, "implementation", json_object_new_string("native-c-event-loop"));
        struct agentseat_input_status input = {0};
        agentseat_input_status(state->input, &input);
        json_object* clipboard = json_object_new_object();
        json_object_object_add(clipboard, "available", json_object_new_boolean(input.control_protocol_version >= 3));
        json_object_object_add(clipboard, "method", json_object_new_string("keyboard.paste"));
        json_object_object_add(clipboard, "max_bytes", json_object_new_int(32768));
        json_object_object_add(clipboard, "lifetime_ms", json_object_new_int(10000));
        json_object_object_add(clipboard, "max_reads", json_object_new_int(64));
        json_object_object_add(clipboard, "max_active_readers", json_object_new_int(16));
        json_object_object_add(clipboard, "verification", json_object_new_string("clipboard_transport_only"));
        json_object_object_add(result, "private_clipboard", clipboard);
        return result;
    }
    if (strcmp(method, "session.heartbeat") == 0) {
        const uint64_t now = monotonic_msec();
        json_object* result = json_object_new_object();
        json_object_object_add(result, "session_id", json_object_new_string(state->session_id));
        json_object_object_add(result, "control_epoch", json_object_new_int64((int64_t)state->control_epoch));
        json_object_object_add(result, "gate_allowed", json_object_new_boolean(state->gate_allowed));
        json_object_object_add(result, "gate_epoch", json_object_new_int64(state->gate_epoch));
        json_object_object_add(result, "human_held", json_object_new_boolean(state->human_held));
        json_object_object_add(result, "daemon_pid", json_object_new_int64(getpid()));
        json_object_object_add(result, "started_msec", json_object_new_int64((int64_t)state->started_msec));
        json_object_object_add(result, "now_msec", json_object_new_int64((int64_t)now));
        json_object_object_add(result, "uptime_ms", json_object_new_int64((int64_t)(now - state->started_msec)));
        json_object_object_add(result, "request_count", json_object_new_int64((int64_t)state->request_count));
        json_object_object_add(result, "seat_generation", json_object_new_int64(state->generation));
        json_object_object_add(result, "seat_created", json_object_new_boolean(state->created));
        json_object_object_add(result, "seat_paused", json_object_new_boolean(state->paused));
        json_object_object_add(result, "leased_windows", json_object_new_int64((int64_t)state->lease_count));
        json_object_object_add(result, "collaboration", collaboration_json(state));
        json_object_object_add(result, "write_control", writer_json(state));
        json_object_object_add(result, "output", output_json(state));
        return result;
    }
    if (strcmp(method, "collaboration.status") == 0) {
        return collaboration_json(state);
    }
    if (strcmp(method, "activity.set") == 0) {
        const char* summary = string_param(params, "summary", true, error);
        const char* detail = optional_string_param(params, "detail", error);
        if (!summary || !detail)
            return NULL;
        if (strlen(summary) > MAX_ACTIVITY_SUMMARY || strlen(detail) > MAX_CHAT_BYTES) {
            error_set(
                error,
                "TEXT_TOO_LARGE",
                AS_TR("Activity text exceeds the AgentSeat collaboration limit"));
            return NULL;
        }
        snprintf(state->activity_summary, sizeof(state->activity_summary), "%s", summary);
        snprintf(state->activity_detail, sizeof(state->activity_detail), "%s", detail);
        return collaboration_json(state);
    }
    if (strcmp(method, "chat.send") == 0 || strcmp(method, "chat.reply") == 0) {
        const char* text = string_param(params, "text", false, error);
        if (!text)
            return NULL;
        if (strlen(text) > MAX_CHAT_BYTES) {
            error_set(error, "TEXT_TOO_LARGE", AS_TR("Chat text exceeds the AgentSeat collaboration limit"));
            return NULL;
        }
        const char* role = strcmp(method, "chat.send") == 0 ? "human" : "agent";
        return message_json(message_append(state, role, text));
    }
    if (strcmp(method, "chat.list") == 0) {
        uint64_t after_id = 0;
        json_object* after_value = NULL;
        if (json_object_object_get_ex(params, "after_id", &after_value)) {
            if (!json_object_is_type(after_value, json_type_int) || json_object_get_int64(after_value) < 0) {
                error_set(error, "INVALID_PARAMS", AS_TR("after_id must be a non-negative integer"));
                return NULL;
            }
            after_id = (uint64_t)json_object_get_int64(after_value);
        }
        json_object* result = json_object_new_object();
        json_object* messages = json_object_new_array();
        for (size_t i = 0; i < state->message_count; ++i) {
            const size_t index = (state->message_start + i) % MAX_MESSAGES;
            if (state->messages[index].id > after_id)
                json_object_array_add(messages, message_json(&state->messages[index]));
        }
        json_object_object_add(result, "messages", messages);
        json_object_object_add(result, "last_message_id", json_object_new_int64((int64_t)state->next_message_id));
        return result;
    }
    if (strcmp(method, "seat.create") == 0) {
        if (!state->created) {
            char message[512] = {0};
            if (!agentseat_input_create(state->input, state->seat_id, state->seat_name, message, sizeof(message))) {
                input_failure(error, "INPUT_CREATE_FAILED", message);
                return NULL;
            }
            state->created = true;
            state->paused = false;
            state->generation++;
            state->scene_version = 0;
            sync_control_state(state);
        }
        json_object* result = json_object_new_object();
        json_object_object_add(result, "changed", json_object_new_boolean(true));
        json_object_object_add(result, "seat", seat_json(state));
        return result;
    }
    if (strcmp(method, "seat.destroy") == 0) {
        revoke_input(state);
        seat_destroy(state);
        json_object* result = json_object_new_object();
        json_object_object_add(result, "changed", json_object_new_boolean(true));
        json_object_object_add(result, "seat", seat_json(state));
        return result;
    }
    if (strcmp(method, "seat.pause") == 0 || strcmp(method, "seat.resume") == 0) {
        if (!require_created(state, error))
            return NULL;
        const bool pause = strcmp(method, "seat.pause") == 0;
        if (pause)
            revoke_input(state);
        char message[512] = {0};
        const bool changed = pause
            ? agentseat_input_pause(state->input, message, sizeof(message))
            : agentseat_input_resume(state->input, message, sizeof(message));
        if (!changed) {
            input_failure(error, "INPUT_STATE_FAILED", message);
            return NULL;
        }
        state->paused = pause;
        json_object* result = json_object_new_object();
        json_object_object_add(result, "changed", json_object_new_boolean(true));
        json_object_object_add(result, "seat", seat_json(state));
        return result;
    }
    if (strcmp(method, "seat.status") == 0) {
        json_object* result = json_object_new_object();
        json_object_object_add(result, "seat", seat_json(state));
        json_object_object_add(result, "leases", leases_json(state));
        struct agentseat_input_status status = {0};
        agentseat_input_status(state->input, &status);
        json_object* backend = json_object_new_object();
        json_object_object_add(backend, "protocol", json_object_new_string("direct-v1"));
        json_object_object_add(backend, "created", json_object_new_boolean(status.created));
        json_object_object_add(backend, "paused", json_object_new_boolean(status.paused));
        json_object_object_add(backend, "global", json_object_new_int64(status.seat_global));
        json_object_object_add(backend, "scope", json_object_new_string("microhost-primary"));
        json_object_object_add(backend, "pointer_x", json_object_new_double(status.pointer_x));
        json_object_object_add(backend, "pointer_y", json_object_new_double(status.pointer_y));
        json_object_object_add(backend, "backend", json_object_new_string("native-wayland-inprocess"));
        json_object_object_add(result, "seat_backend", backend);
        json_object* compositor = json_object_new_object();
        json_object_object_add(compositor, "backend", json_object_new_string("single-app-microhost"));
        json_object_object_add(result, "compositor", compositor);
        json_object_object_add(result, "collaboration", collaboration_json(state));
        return result;
    }
    if (strcmp(method, "window.list") == 0) {
        if (!require_created(state, error))
            return NULL;
        return windows_json(state);
    }
    if (strcmp(method, "window.geometry") == 0) {
        error_set(error, "UNSUPPORTED", "Window geometry is reported by the compositor and cannot be overwritten");
        return NULL;
    }
    if (strcmp(method, "window.lease") == 0 || strcmp(method, "window.release") == 0 ||
        strcmp(method, "window.focus") == 0 || strcmp(method, "window.move") == 0 ||
        strcmp(method, "window.resize") == 0 || strcmp(method, "window.close") == 0 || strcmp(method, "capture.window") == 0) {
        if (!require_created(state, error))
            return NULL;
        const char* requested = string_param(params, "window_id", false, error);
        if (!requested)
            return NULL;
        const struct agentseat_window* window = find_window(state, requested);
        if (!window) {
            error_set(error, "WINDOW_NOT_FOUND", "The window is not mapped in this application session");
            return NULL;
        }
        char id[64];
        snprintf(id, sizeof(id), "w-%u", window->id);
        if (strcmp(method, "window.lease") != 0 && !lease_contains(state, id)) {
            error_set(error, "LEASE_REQUIRED", "Window is not leased to AgentSeat");
            return NULL;
        }
        if (strcmp(method, "capture.window") == 0) {
            error_set(error, "CAPTURE_VIA_SCREENCOPY", "Capture the application output with its coordinate mapping");
            return NULL;
        }
        if (method_needs_writer(method)) {
            if (window->flags & 8) {
                error_set(error, "UNSUPPORTED", "Popup placement and grabs belong to the application"); return NULL;
            }
            if (!require_active(state, error))
                return NULL;
            if (state->typing) {
                error_set(error, "BUSY", "An operation is active");
                return NULL;
            }
            uint32_t action = strcmp(method, "window.focus") == 0 ? 1 : strcmp(method, "window.move") == 0 ? 2 : strcmp(method, "window.resize") == 0 ? 3 : 4;
            if ((action == 2 || action == 3) && (window->flags & 1)) {
                error_set(error, "UNSUPPORTED", "The primary window is configured by the host-owned viewport policy");
                return NULL;
            }
            int first = 0, second = 0;
            if (action == 2 || action == 3) {
                json_object *a = NULL, *b = NULL;
                if (!json_object_object_get_ex(params, action == 2 ? "x" : "width", &a) ||
                    !json_object_object_get_ex(params, action == 2 ? "y" : "height", &b) ||
                    !json_object_is_type(a, json_type_int) || !json_object_is_type(b, json_type_int) ||
                    json_object_get_int64(a) < (action == 3 ? 1 : 0) || json_object_get_int64(a) > 16384 ||
                    json_object_get_int64(b) < (action == 3 ? 1 : 0) || json_object_get_int64(b) > 16384) {
                    error_set(error, "INVALID_PARAMS", "Window coordinates and dimensions must be bounded integers");
                    return NULL;
                }
                first = json_object_get_int(a);
                second = json_object_get_int(b);
            }
            uint64_t sequence = ++state->next_operation;
            struct type_operation* operation = &state->operations[sequence % MAX_OPERATIONS];
            *operation = (struct type_operation){.count = 1, .submitted = 1, .active = true,
                .outcome = "running", .epoch = state->control_epoch, .window_action = action,
                .window_id = window->id, .action_token = (uint32_t)sequence,
                .action_first = first, .action_second = second,
                .next_msec = monotonic_msec() + 3000, .deadline_msec = monotonic_msec() + 3000};
            snprintf(operation->id, sizeof(operation->id), "%s:%llu", state->session_id, (unsigned long long)sequence);
            state->typing = operation;
            agentseat_input_arm(state->input, (uint32_t)state->control_epoch);
            agentseat_input_window_action(state->input, operation->action_token, window->id, action, first, second);
            return operation_json(operation);
        }
        if (strcmp(method, "window.release") == 0) {
            bool denied = false;
            for (size_t i = 0; i < state->denied_count; ++i) denied |= state->denied_windows[i] == window->id;
            if (!denied && state->denied_count == MAX_LEASES) {
                error_set(error, "RESOURCE_LIMIT", "Window exclusion budget is exhausted"); return NULL;
            }
            if (!denied) state->denied_windows[state->denied_count++] = window->id;
            lease_remove(state, id);
            if (!strcmp(state->focused_window, id) ||
                (state->typing && state->typing->window_id == window->id)) revoke_input(state);
        } else {
            for (size_t i = 0; i < state->denied_count; ) {
                if (state->denied_windows[i] == window->id)
                    state->denied_windows[i] = state->denied_windows[--state->denied_count];
                else
                    i++;
            }
            lease_add(state, id);
        }
        return delivered_result();
    }
    if (strcmp(method, "pointer.move_absolute") == 0) {
        if (!require_active(state, error))
            return NULL;
        json_object* x_value = NULL;
        json_object* y_value = NULL;
        if (!json_object_object_get_ex(params, "x", &x_value) || !json_object_object_get_ex(params, "y", &y_value) ||
            !(json_object_is_type(x_value, json_type_int) || json_object_is_type(x_value, json_type_double)) ||
            !(json_object_is_type(y_value, json_type_int) || json_object_is_type(y_value, json_type_double))) {
            error_set(error, "INVALID_PARAMS", AS_TR("pointer.move_absolute requires x and y"));
            return NULL;
        }
        const double x = json_object_get_double(x_value);
        const double y = json_object_get_double(y_value);
        if (!isfinite(x) || !isfinite(y) || x < 0 || x > 1 || y < 0 || y > 1) {
            error_set(error, "INVALID_PARAMS", AS_TR("x and y must be normalized from 0 to 1"));
            return NULL;
        }
        char message[512] = {0};
        if (!agentseat_input_move_absolute(state->input, x, y, message, sizeof(message))) {
            input_failure(error, "POINTER_MOVE_FAILED", message);
            return NULL;
        }
        state->pointer_x = x;
        state->pointer_y = y;
        return delivered_result();
    }
    if (strcmp(method, "pointer.click") == 0) {
        if (!require_focus_lease(state, error))
            return NULL;
        json_object* button_value = NULL;
        if (!json_object_object_get_ex(params, "button", &button_value) || !json_object_is_type(button_value, json_type_int)) {
            error_set(error, "INVALID_PARAMS", "button must be an integer");
            return NULL;
        }
        int64_t button = json_object_get_int64(button_value);
        if (button < 1 || button > 1023) {
            error_set(error, "INVALID_PARAMS", "button is outside the supported range");
            return NULL;
        }
        char message[512] = {0};
        bool pressed = agentseat_input_button(state->input, (uint32_t)button, true, message, sizeof(message));
        bool released = agentseat_input_button(state->input, (uint32_t)button, false, message, sizeof(message));
        if (!pressed || !released) {
            agentseat_input_release_all(state->input);
            input_failure(error, "POINTER_CLICK_FAILED", message);
            return NULL;
        }
        return delivered_result();
    }
    if (strcmp(method, "pointer.button") == 0) {
        if (!require_focus_lease(state, error))
            return NULL;
        json_object* button_value = NULL;
        bool pressed = false;
        if (!json_object_object_get_ex(params, "button", &button_value) || !bool_param(params, "pressed", false, &pressed, error)) {
            if (!error->code[0])
                error_set(error, "INVALID_PARAMS", AS_TR("pointer.button requires button and pressed"));
            return NULL;
        }
        const int64_t button = json_object_get_int64(button_value);
        if (!json_object_is_type(button_value, json_type_int) || button < 1 || button > 1023) {
            error_set(error, "INVALID_PARAMS", AS_TR("button must be an integer from 1 to 1023"));
            return NULL;
        }
        char message[512] = {0};
        if (!agentseat_input_button(state->input, (uint32_t)button, pressed, message, sizeof(message))) {
            input_failure(error, "POINTER_BUTTON_FAILED", message);
            return NULL;
        }
        return delivered_result();
    }
    if (strcmp(method, "pointer.scroll") == 0) {
        if (!require_focus_lease(state, error))
            return NULL;
        const char* axis = string_param(params, "axis", false, error);
        json_object* value = NULL;
        if (!axis || !json_object_object_get_ex(params, "value120", &value)) {
            if (!error->code[0])
                error_set(error, "INVALID_PARAMS", AS_TR("pointer.scroll requires axis and value120"));
            return NULL;
        }
        if (strcmp(axis, "vertical") != 0 && strcmp(axis, "horizontal") != 0) {
            error_set(error, "INVALID_PARAMS", AS_TR("axis must be vertical or horizontal"));
            return NULL;
        }
        const int64_t value120 = json_object_get_int64(value);
        if (!json_object_is_type(value, json_type_int) || value120 < -12000 || value120 > 12000) {
            error_set(error, "INVALID_PARAMS", AS_TR("value120 must be from -12000 to 12000"));
            return NULL;
        }
        char message[512] = {0};
        if (!agentseat_input_scroll(
                state->input,
                strcmp(axis, "horizontal") == 0,
                value120,
                message,
                sizeof(message))) {
            input_failure(error, "POINTER_SCROLL_FAILED", message);
            return NULL;
        }
        return delivered_result();
    }
    if (strcmp(method, "operation.get") == 0 || strcmp(method, "operation.cancel") == 0) {
        const char* id = string_param(params, "operation_id", false, error);
        if (!id)
            return NULL;
        for (size_t i = 0; i < MAX_OPERATIONS; ++i) {
            struct type_operation* operation = &state->operations[i];
            if (strcmp(operation->id, id) != 0)
                continue;
            if (strcmp(method, "operation.cancel") == 0 && operation->active) {
                revoke_input(state);
            }
            return operation_json(operation);
        }
        error_set(error, "OPERATION_NOT_FOUND", "Operation is unknown or its bounded history has expired");
        return NULL;
    }
    if (strcmp(method, "keyboard.paste") == 0) {
        if (!require_focus_lease(state, error)) return NULL;
        const char* text = string_param(params, "text", false, error);
        if (!text) return NULL;
        json_object* value = NULL;
        json_object_object_get_ex(params, "text", &value);
        if (!strlen(text) || strlen(text) > MAX_TYPE_BYTES || (size_t)json_object_get_string_len(value) != strlen(text)) {
            error_set(error, "INVALID_PARAMS", "Paste requires 1..32768 NUL-free UTF-8 bytes"); return NULL;
        }
        uint64_t sequence = ++state->next_operation;
        uint32_t token = (uint32_t)sequence;
        if (!token) { error_set(error, "RESOURCE_LIMIT", "Clipboard token exhausted"); return NULL; }
        char message[512] = {0};
        if (!agentseat_input_paste_offer(state->input, token, text, message, sizeof(message))) {
            input_failure(error, "KEYBOARD_PASTE_FAILED", message); return NULL;
        }
        struct type_operation* operation = &state->operations[sequence % MAX_OPERATIONS];
        *operation = (struct type_operation){.clipboard = true, .action_token = token,
            .count = 1, .submitted = 1, .active = true, .outcome = "running",
            .epoch = state->control_epoch, .deadline_msec = monotonic_msec() + 5000};
        snprintf(operation->id, sizeof(operation->id), "%s:%llu", state->session_id, (unsigned long long)sequence);
        state->typing = operation;
        return operation_json(operation);
    }
    if (strcmp(method, "keyboard.type") == 0) {
        if (!require_focus_lease(state, error))
            return NULL;
        const char* text = string_param(params, "text", true, error);
        if (!text)
            return NULL;
        if (strlen(text) > MAX_TYPE_BYTES) {
            error_set(error, "TEXT_TOO_LARGE", AS_TR("text exceeds the native AgentSeat input limit"));
            return NULL;
        }
        json_object* text_value = NULL;
        json_object_object_get_ex(params, "text", &text_value);
        if ((size_t)json_object_get_string_len(text_value) != strlen(text)) {
            error_set(error, "INVALID_PARAMS", "Embedded NUL is not a text input character");
            return NULL;
        }
        json_object* interval_value = NULL;
        bool has_interval = json_object_object_get_ex(params, "interval_ms", &interval_value);
        int64_t interval = has_interval ? json_object_get_int64(interval_value) : 4;
        if ((has_interval && !json_object_is_type(interval_value, json_type_int)) || interval < 0 || interval > 10000) {
            error_set(error, "INVALID_PARAMS", AS_TR("interval_ms must be from 0 to 10000"));
            return NULL;
        }
        uint32_t* codepoints = NULL;
        size_t count = 0;
        bool unsupported = false;
        char message[512] = {0};
        if (!agentseat_input_prepare_text(state->input, text, &codepoints, &count, &unsupported, message, sizeof(message))) {
            input_failure(error, unsupported ? "UNSUPPORTED_CHARACTER" : "KEYBOARD_TYPE_FAILED", message);
            return NULL;
        }
        if (count > 0 && (count - 1) * (uint64_t)interval > MAX_TYPE_DURATION_MS - 1000) {
            free(codepoints);
            error_set(error, "RESOURCE_LIMIT", "Text operation exceeds the three-minute execution budget");
            return NULL;
        }
        uint64_t sequence = ++state->next_operation;
        struct type_operation* operation = &state->operations[sequence % MAX_OPERATIONS];
        *operation = (struct type_operation){
            .codepoints = codepoints, .count = count, .next_msec = monotonic_msec(),
            .deadline_msec = monotonic_msec() + MAX_TYPE_DURATION_MS,
            .interval_ms = (unsigned)interval, .active = true,
            .outcome = "running", .epoch = state->control_epoch,
        };
        snprintf(operation->id, sizeof(operation->id), "%s:%llu", state->session_id, (unsigned long long)sequence);
        state->typing = operation;
        if (!count)
            finish_typing(state, "finished");
        return operation_json(operation);
    }
    if (strcmp(method, "keyboard.key") == 0) {
        if (!require_focus_lease(state, error))
            return NULL;
        json_object* key_value = NULL;
        bool pressed = false;
        if (!json_object_object_get_ex(params, "keycode", &key_value) ||
            !bool_param(params, "pressed", false, &pressed, error)) {
            if (!error->code[0])
                error_set(error, "INVALID_PARAMS", AS_TR("keyboard.key requires keycode and pressed"));
            return NULL;
        }
        const int64_t keycode = json_object_get_int64(key_value);
        if (!json_object_is_type(key_value, json_type_int) || keycode < 0 || keycode >= 768) {
            error_set(error, "INVALID_PARAMS", AS_TR("keycode must be an evdev code from 0 to 767"));
            return NULL;
        }
        char message[512] = {0};
        if (!agentseat_input_key(state->input, (uint32_t)keycode, pressed, message, sizeof(message))) {
            input_failure(error, "KEYBOARD_KEY_FAILED", message);
            return NULL;
        }
        return delivered_result();
    }
    if (strcmp(method, "shutdown") == 0) {
        seat_destroy(state);
        state->quit = true;
        json_object* result = json_object_new_object();
        json_object_object_add(result, "shutdown", json_object_new_boolean(true));
        return result;
    }
    error_set(error, "METHOD_NOT_FOUND", AS_TR("Unknown native AgentSeat method"));
    return NULL;
}

static json_object* response_for(struct daemon_state* state, json_object* request) {
    json_object* response = json_object_new_object();
    json_object_object_add(response, "jsonrpc", json_object_new_string("2.0"));
    json_object* id = NULL;
    if (json_object_is_type(request, json_type_object) && json_object_object_get_ex(request, "id", &id))
        json_object_object_add(response, "id", json_object_get(id));
    else
        json_object_object_add(response, "id", NULL);

    struct rpc_error error = {0};
    json_object* method_value = NULL;
    json_object* version_value = NULL;
    json_object* params = NULL;
    bool owns_params = false;
    bool admitted = false;
    size_t request_controller = 0;
    uint64_t request_sequence = 0;
    if (!json_object_is_type(request, json_type_object) ||
        !json_object_object_get_ex(request, "jsonrpc", &version_value) ||
        !json_object_is_type(version_value, json_type_string) ||
        json_object_get_string_len(version_value) != 3 || strcmp(json_object_get_string(version_value), "2.0") != 0 ||
        !json_object_object_get_ex(request, "method", &method_value) ||
        !json_object_is_type(method_value, json_type_string) ||
        json_object_get_string_len(method_value) == 0 ||
        (size_t)json_object_get_string_len(method_value) != strlen(json_object_get_string(method_value))) {
        error_set(&error, "INVALID_REQUEST", "jsonrpc must be 2.0 and method must be a non-empty NUL-free string");
    } else if (!json_object_object_get_ex(request, "params", &params)) {
        params = json_object_new_object();
        owns_params = true;
    } else if (!json_object_is_type(params, json_type_object)) {
        error_set(&error, "INVALID_PARAMS", AS_TR("params must be an object"));
    }

    json_object* result = NULL;
    if (!error.code[0]) {
        state->request_count++;
        const char* method = json_object_get_string(method_value);
        json_object* session = NULL;
        if (method_is_write(method) &&
            (!json_object_object_get_ex(params, "session_id", &session) ||
             !json_object_is_type(session, json_type_string) ||
             (size_t)json_object_get_string_len(session) != strlen(state->session_id) ||
             strcmp(json_object_get_string(session), state->session_id) != 0)) {
            error_set(&error, "SESSION_MISMATCH", "Write request must name the current session");
        }
        if (!error.code[0] && method_is_write(method)) {
            json_object* replay = NULL;
            const char* failure = as_request_begin(&state->requests, method, params, &replay,
                &request_controller, &request_sequence);
            if (failure) {
                error_set(&error, failure, "Write requests require controller_id and increasing request_id; expired IDs cannot be replayed");
            } else if (replay) {
                json_object* copy = NULL;
                json_object_deep_copy(replay, &copy, NULL);
                if (!copy) {
                    error_set(&error, "RESOURCE_LIMIT", "Cannot copy the retained receipt");
                } else {
                    json_object_object_add(copy, "id", id ? json_object_get(id) : NULL);
                    json_object_put(response);
                    if (owns_params)
                        json_object_put(params);
                    return copy;
                }
            } else {
                admitted = true;
            }
        }
        if (!error.code[0] && method_needs_writer(method)) {
            json_object* geometry = NULL;
            bool has_geometry = json_object_object_get_ex(params, "geometry_version", &geometry);
            if (has_geometry && (!json_object_is_type(geometry, json_type_int) ||
                json_object_get_int64(geometry) <= 0 ||
                (uint64_t)json_object_get_int64(geometry) != agentseat_input_scene(state->input)->version)) {
                error_set(&error, "STALE_GEOMETRY", "Scene changed; observe again before using frame coordinates");
            } else {
                agentseat_input_geometry(state->input, has_geometry ? (uint32_t)json_object_get_int64(geometry) : 0);
                require_writer(state, params, &error);
            }
        }
        if (!error.code[0] && method_needs_writer(method)) {
            json_object* epoch = NULL;
            if (!json_object_object_get_ex(params, "control_epoch", &epoch) ||
                !json_object_is_type(epoch, json_type_int) || json_object_get_int64(epoch) < 0 ||
                (uint64_t)json_object_get_int64(epoch) != state->control_epoch) {
                error_set(&error, "STALE_CONTROL", "Control changed; observe again before sending input");
            } else if (state->typing) {
                error_set(&error, "BUSY", "A text operation is active; cancel it before sending other input");
            } else {
                json_object* hold = NULL;
                bool has_hold = json_object_object_get_ex(params, "hold_ms", &hold);
                int64_t hold_ms = has_hold ? json_object_get_int64(hold) : 2000;
                if ((has_hold && !json_object_is_type(hold, json_type_int)) || hold_ms < 100 || hold_ms > 10000) {
                    error_set(&error, "INVALID_PARAMS", "hold_ms must be an integer from 100 to 10000");
                } else {
                    agentseat_input_hold_timeout(state->input, (unsigned)hold_ms);
                    agentseat_input_arm(state->input, (uint32_t)state->control_epoch);
                }
            }
        }
        if (!error.code[0])
            result = dispatch(state, &error, method, params);
    }
    if (result) {
        if (admitted && json_object_is_type(result, json_type_object)) {
            json_object_object_add(result, "request_id", json_object_new_int64((int64_t)request_sequence));
            json_object_object_add(result, "request_controller_id", json_object_new_string(
                state->requests.controllers[request_controller].id));
        }
        json_object_object_add(response, "result", result);
    } else {
        json_object* failure = json_object_new_object();
        int code = strcmp(error.code, "INVALID_REQUEST") == 0 ? -32600 :
            strcmp(error.code, "METHOD_NOT_FOUND") == 0 ? -32601 :
            strcmp(error.code, "INVALID_PARAMS") == 0 ? -32602 : -32000;
        json_object_object_add(failure, "code", json_object_new_int(code));
        json_object* details = json_object_new_object();
        json_object_object_add(details, "kind", json_object_new_string(error.code[0] ? error.code : "INTERNAL_ERROR"));
        json_object_object_add(details, "control_epoch", json_object_new_int64((int64_t)state->control_epoch));
        json_object_object_add(failure, "data", details);
        json_object_object_add(
            failure,
            "message",
            json_object_new_string(
                error.message[0] ? error.message : AS_TR("Native AgentSeat request failed")));
        json_object_object_add(response, "error", failure);
    }
    if (admitted)
        as_request_finish(&state->requests, request_controller, request_sequence,
            json_object_get_string(method_value), params, response);
    if (owns_params)
        json_object_put(params);
    return response;
}

static bool ensure_parent(const char* path) {
    char* copy = strdup(path);
    if (!copy)
        return false;
    char* slash = strrchr(copy, '/');
    if (!slash || slash == copy) {
        free(copy);
        return false;
    }
    *slash = '\0';
    if (mkdir(copy, 0700) != 0 && errno != EEXIST) {
        free(copy);
        return false;
    }
    chmod(copy, 0700);
    free(copy);
    return true;
}

static bool server_start(struct daemon_state* state, struct rpc_error* error) {
    if (!ensure_parent(state->socket_path)) {
        error_set(error, "SOCKET_FAILED", AS_TR("Cannot create AgentSeat runtime directory"));
        return false;
    }
    unlink(state->socket_path);
    state->listen_fd = socket(AF_UNIX, SOCK_STREAM | SOCK_CLOEXEC | SOCK_NONBLOCK, 0);
    if (state->listen_fd < 0) {
        error_set(error, "SOCKET_FAILED", AS_TR("Cannot create AgentSeat Unix socket"));
        return false;
    }
    struct sockaddr_un address = {.sun_family = AF_UNIX};
    if (strlen(state->socket_path) >= sizeof(address.sun_path)) {
        error_set(error, "SOCKET_FAILED", AS_TR("AgentSeat socket path is too long"));
        return false;
    }
    strcpy(address.sun_path, state->socket_path);
    if (bind(state->listen_fd, (struct sockaddr*)&address, sizeof(address)) != 0 || listen(state->listen_fd, MAX_CLIENTS * 2) != 0) {
        error_set(error, "SOCKET_FAILED", AS_TR("Cannot bind AgentSeat Unix socket"));
        return false;
    }
    chmod(state->socket_path, 0600);

    if (!ensure_parent(state->activity_socket_path)) {
        error_set(error, "SOCKET_FAILED", AS_TR("Cannot create AgentSeat activity runtime directory"));
        return false;
    }
    unlink(state->activity_socket_path);
    state->activity_fd = socket(AF_UNIX, SOCK_DGRAM | SOCK_CLOEXEC | SOCK_NONBLOCK, 0);
    if (state->activity_fd < 0) {
        error_set(error, "SOCKET_FAILED", AS_TR("Cannot create AgentSeat human activity socket"));
        return false;
    }
    struct sockaddr_un activity_address = {.sun_family = AF_UNIX};
    if (strlen(state->activity_socket_path) >= sizeof(activity_address.sun_path)) {
        error_set(error, "SOCKET_FAILED", AS_TR("AgentSeat activity socket path is too long"));
        return false;
    }
    strcpy(activity_address.sun_path, state->activity_socket_path);
    if (bind(state->activity_fd, (struct sockaddr*)&activity_address, sizeof(activity_address)) != 0) {
        error_set(error, "SOCKET_FAILED", AS_TR("Cannot bind AgentSeat human activity socket"));
        return false;
    }
    chmod(state->activity_socket_path, 0600);
    return true;
}

static void client_close(struct rpc_client* client) {
    if (client->fd >= 0)
        close(client->fd);
    free(client->request);
    free(client->response);
    *client = (struct rpc_client){.fd = -1};
}

static void client_read(struct daemon_state* state, struct rpc_client* client) {
    char block[CLIENT_IO_BUDGET];
    ssize_t count = recv(client->fd, block, sizeof(block), MSG_DONTWAIT);
    if (count < 0 && (errno == EAGAIN || errno == EWOULDBLOCK || errno == EINTR))
        return;
    if (count <= 0 || client->length + (size_t)count > MAX_REQUEST) {
        client_close(client);
        return;
    }
    size_t needed = client->length + (size_t)count + 1;
    if (needed > client->capacity) {
        size_t capacity = needed * 2;
        if (capacity > MAX_REQUEST + 1)
            capacity = MAX_REQUEST + 1;
        char* buffer = realloc(client->request, capacity);
        if (!buffer) {
            client_close(client);
            return;
        }
        client->request = buffer;
        client->capacity = capacity;
    }
    memcpy(client->request + client->length, block, (size_t)count);
    client->length += (size_t)count;
    client->request[client->length] = '\0';
    char* newline = memchr(client->request, '\n', client->length);
    if (!newline)
        return;
    /* The control transport is exactly one newline-terminated request per
     * connection. Incomplete documents are never executed on EOF/timeout. */
    struct json_tokener* parser = json_tokener_new();
    json_tokener_set_flags(parser, JSON_TOKENER_STRICT | JSON_TOKENER_VALIDATE_UTF8);
    size_t length = (size_t)(newline - client->request);
    json_object* request = json_tokener_parse_ex(parser, client->request, (int)length);
    size_t consumed = json_tokener_get_parse_end(parser);
    bool valid = json_tokener_get_error(parser) == json_tokener_success;
    for (size_t i = consumed; i < length; ++i)
        if (client->request[i] != ' ' && client->request[i] != '\t' && client->request[i] != '\r')
            valid = false;
    json_tokener_free(parser);
    if (!valid) {
        json_object_put(request);
        request = NULL;
    }
    json_object* response = response_for(state, request);
    json_object_put(request);
    const char* encoded = json_object_to_json_string_ext(response, JSON_C_TO_STRING_PLAIN);
    size_t encoded_length = strlen(encoded);
    client->response = malloc(encoded_length + 1);
    if (client->response) {
        memcpy(client->response, encoded, encoded_length);
        client->response[encoded_length] = '\n';
        client->response_length = encoded_length + 1;
        client->deadline_msec = monotonic_msec() + CLIENT_TIMEOUT_MS;
    }
    json_object_put(response);
    free(client->request);
    client->request = NULL;
    client->capacity = client->length = 0;
    if (!client->response)
        client_close(client);
}

static void client_write(struct rpc_client* client) {
    size_t length = client->response_length - client->written;
    if (length > CLIENT_IO_BUDGET)
        length = CLIENT_IO_BUDGET;
    ssize_t count = send(client->fd, client->response + client->written, length, MSG_DONTWAIT | MSG_NOSIGNAL);
    if (count < 0 && (errno == EAGAIN || errno == EWOULDBLOCK || errno == EINTR))
        return;
    if (count <= 0) {
        client_close(client);
        return;
    }
    client->written += (size_t)count;
    if (client->written == client->response_length)
        client_close(client);
}

static int run_server(struct daemon_state* state) {
    const int input_fd = agentseat_input_fd(state->input);
    struct rpc_client clients[MAX_CLIENTS];
    for (size_t i = 0; i < MAX_CLIENTS; ++i)
        clients[i] = (struct rpc_client){.fd = -1};
    int exit_code = 0;
    while (!signal_stop) {
        as_capture_tick(agentseat_input_capture(state->input));
        uint64_t now = monotonic_msec();
        if (state->writer_deadline && now >= state->writer_deadline)
            revoke_input(state);
        uint64_t deadline = state->writer_deadline ? state->writer_deadline : UINT64_MAX;
        uint64_t capture_deadline = as_capture_deadline(agentseat_input_capture(state->input));
        if (capture_deadline && capture_deadline < deadline) deadline = capture_deadline;
        struct pollfd poll_fds[4 + AS_SEMANTIC_POLL_MAX + MAX_CLIENTS] = {
            {.fd = state->quit ? -1 : state->listen_fd, .events = POLLIN},
            {.fd = input_fd, .events = POLLIN},
            {.fd = state->activity_fd, .events = POLLIN},
            {.fd = as_capture_fd(agentseat_input_capture(state->input)), .events = POLLIN},
        };
        for (size_t i = 0; i < AS_SEMANTIC_POLL_MAX; ++i) poll_fds[4 + i].fd = -1;
        size_t active_clients = 0;
        for (size_t i = 0; i < MAX_CLIENTS; ++i) {
            struct rpc_client* client = &clients[i];
            if (client->fd >= 0 && (now >= client->deadline_msec || (state->quit && !client->response)))
                client_close(client);
            poll_fds[4 + AS_SEMANTIC_POLL_MAX + i] = (struct pollfd){.fd = client->fd, .events = client->response ? POLLOUT : POLLIN};
            if (client->fd >= 0) {
                active_clients++;
                if (client->deadline_msec < deadline)
                    deadline = client->deadline_msec;
            }
        }
        if (state->quit && !active_clients)
            break;
        if (state->typing && state->typing->next_msec < deadline)
            deadline = state->typing->next_msec;
        int timeout = deadline == UINT64_MAX ? -1 : deadline <= now ? 0 :
            deadline - now > INT32_MAX ? INT32_MAX : (int)(deadline - now);
        struct as_semantic* polled_semantic = state->semantic;
        as_semantic_prepare_poll(polled_semantic, &poll_fds[4], &timeout);
        int result = poll(poll_fds, 4 + AS_SEMANTIC_POLL_MAX + MAX_CLIENTS, timeout);
        if (result < 0) as_semantic_dispatch(polled_semantic, &poll_fds[4]);
        if (result < 0 && errno == EINTR)
            continue;
        if (result < 0) {
            exit_code = 1;
            break;
        }
        if (poll_fds[1].revents & (POLLERR | POLLHUP | POLLNVAL)) {
            exit_code = 2;
            break;
        }
        if (poll_fds[1].revents & POLLIN) {
            char message[512] = {0};
            if (!agentseat_input_dispatch(state->input, message, sizeof(message))) {
                fprintf(stderr, "agentseatd: %s: %s\n", AS_TR("input dispatch failed"), message);
                exit_code = 2;
                break;
            }
        }
        if (poll_fds[2].revents & POLLIN) {
            char buffer[64];
            /* Bound work per turn even if a peer floods activity datagrams. */
            for (unsigned i = 0; i < 64 && recv(state->activity_fd, buffer, sizeof(buffer), MSG_DONTWAIT) > 0; ++i) {
                /* Compatibility telemetry only. Gate events on the owned
                 * Wayland connection are the authority for input ordering. */
            }
        }
        as_capture_tick(agentseat_input_capture(state->input));
        sync_control_state(state);
        as_semantic_dispatch(polled_semantic, &poll_fds[4]);
        for (size_t i = 0; i < MAX_CLIENTS; ++i) {
            struct rpc_client* client = &clients[i];
            short events = poll_fds[4 + AS_SEMANTIC_POLL_MAX + i].revents;
            if (client->fd < 0)
                continue;
            if (events & (POLLERR | POLLNVAL)) {
                client_close(client);
                continue;
            }
            if (!client->response && (events & POLLIN))
                client_read(state, client);
            if (client->fd >= 0 && client->response && (events & (POLLOUT | POLLIN)))
                client_write(client);
            if (client->fd >= 0 && (events & POLLHUP))
                client_close(client);
        }
        if (!state->quit && (poll_fds[0].revents & POLLIN)) {
            /* A bounded accept batch drains short reconnect bursts without
             * starving the existing clients or input cancellation. */
            for (unsigned accepted = 0; accepted < 8; ++accepted) {
                int fd = accept4(state->listen_fd, NULL, NULL, SOCK_CLOEXEC | SOCK_NONBLOCK);
                if (fd < 0)
                    break;
                struct ucred credentials = {0};
                socklen_t length = sizeof(credentials);
                bool owned = getsockopt(fd, SOL_SOCKET, SO_PEERCRED, &credentials, &length) == 0 && credentials.uid == getuid();
                size_t slot = 0;
                while (slot < MAX_CLIENTS && clients[slot].fd >= 0)
                    slot++;
                if (!owned || slot == MAX_CLIENTS)
                    close(fd);
                else
                    clients[slot] = (struct rpc_client){.fd = fd, .deadline_msec = monotonic_msec() + CLIENT_TIMEOUT_MS};
            }
        }
        if (state->quit)
            finish_typing(state, "cancelled");
        else {
            advance_typing(state);
            advance_semantic(state);
        }
    }
    finish_typing(state, "cancelled");
    for (size_t i = 0; i < MAX_CLIENTS; ++i)
        client_close(&clients[i]);
    as_request_ledger_finish(&state->requests);
    as_semantic_free(state->semantic);
    return exit_code;
}

static void usage(const char* program) {
    fprintf(
        stderr,
        AS_TR("usage: %s --socket PATH --activity-socket PATH [--session-id ID] [--human-grace-ms N] [--workspace N] [--seat-id ID] [--seat-name NAME] [--app-id ID] [--window-title TITLE] [--xwayland] [--width N] [--height N]\n"),
        program);
}

int main(int argc, char** argv) {
    agentseat_i18n_init();
    struct daemon_state state = {
        .workspace = 1,
        .listen_fd = -1,
        .activity_fd = -1,
        .human_grace_ms = 1500,
        .paused = true,
        .pointer_x = 0.5,
        .pointer_y = 0.5,
        .session_id = "agentseat-session",
        .seat_id = "agent:agentseat",
        .seat_name = "AgentSeat Agent",
        .app_id = "agentseat.application",
        .window_title = "AgentSeat Application",
        .output_width = 1280,
        .output_height = 720,
        .activity_summary = "",
    };
    for (int i = 1; i < argc; ++i) {
        if (strcmp(argv[i], "--socket") == 0 && i + 1 < argc)
            state.socket_path = argv[++i];
        else if (strcmp(argv[i], "--activity-socket") == 0 && i + 1 < argc)
            state.activity_socket_path = argv[++i];
        else if (strcmp(argv[i], "--session-id") == 0 && i + 1 < argc)
            state.session_id = argv[++i];
        else if (strcmp(argv[i], "--human-grace-ms") == 0 && i + 1 < argc)
            state.human_grace_ms = atoi(argv[++i]);
        else if (strcmp(argv[i], "--workspace") == 0 && i + 1 < argc)
            state.workspace = atoi(argv[++i]);
        else if (strcmp(argv[i], "--seat-id") == 0 && i + 1 < argc)
            state.seat_id = argv[++i];
        else if (strcmp(argv[i], "--seat-name") == 0 && i + 1 < argc)
            state.seat_name = argv[++i];
        else if (strcmp(argv[i], "--app-id") == 0 && i + 1 < argc)
            state.app_id = argv[++i];
        else if (strcmp(argv[i], "--window-title") == 0 && i + 1 < argc)
            state.window_title = argv[++i];
        else if (strcmp(argv[i], "--xwayland") == 0)
            state.xwayland = true;
        else if (strcmp(argv[i], "--width") == 0 && i + 1 < argc)
            state.output_width = atoi(argv[++i]);
        else if (strcmp(argv[i], "--height") == 0 && i + 1 < argc)
            state.output_height = atoi(argv[++i]);
        else {
            usage(argv[0]);
            return 2;
        }
    }
    if (!state.socket_path || !state.activity_socket_path || !state.session_id || !state.session_id[0] ||
        strlen(state.session_id) > MAX_SESSION_ID || state.workspace <= 0 || state.human_grace_ms < 0 || state.human_grace_ms > 60000 ||
        state.output_width <= 0 || state.output_height <= 0) {
        usage(argv[0]);
        return 2;
    }

    signal(SIGINT, on_signal);
    signal(SIGTERM, on_signal);
    signal(SIGPIPE, SIG_IGN);
    struct rpc_error error = {0};
    char input_message[512] = {0};
    state.input = agentseat_input_open(input_message, sizeof(input_message));
    if (!state.input)
        error_set(&error, "INPUT_START_FAILED", input_message);
    if (!state.input || !server_start(&state, &error)) {
        fprintf(stderr, "agentseatd: %s: %s\n", error.code, error.message);
        agentseat_input_close(state.input);
        if (state.listen_fd >= 0)
            close(state.listen_fd);
        if (state.activity_fd >= 0)
            close(state.activity_fd);
        unlink(state.socket_path);
        if (state.activity_socket_path)
            unlink(state.activity_socket_path);
        return 1;
    }

    state.started_msec = monotonic_msec();
    sync_control_state(&state);
    const int result = run_server(&state);
    seat_destroy(&state);
    agentseat_input_close(state.input);
    if (state.listen_fd >= 0)
        close(state.listen_fd);
    if (state.activity_fd >= 0)
        close(state.activity_fd);
    unlink(state.socket_path);
    unlink(state.activity_socket_path);
    leases_clear(&state);
    return result;
}
