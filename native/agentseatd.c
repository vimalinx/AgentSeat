#define _GNU_SOURCE

#include <errno.h>
#include <json-c/json.h>
#include <poll.h>
#include <signal.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <sys/stat.h>
#include <sys/types.h>
#include <sys/un.h>
#include <time.h>
#include <unistd.h>

#include "agentseat-input.h"

#define AGENTSEATD_VERSION "0.5.0"
#define MAX_REQUEST (1024U * 1024U)
#define MAX_LEASES 256
#define MAX_TYPE_BYTES 32768U
#define MAX_MESSAGES 64
#define MAX_CHAT_BYTES 2048U
#define MAX_ACTIVITY_SUMMARY 512U

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
    char activity_summary[MAX_ACTIVITY_SUMMARY + 1];
    char activity_detail[MAX_CHAT_BYTES + 1];
    struct collaboration_message messages[MAX_MESSAGES];
    size_t message_start;
    size_t message_count;
    uint64_t next_message_id;

    bool created;
    bool paused;
    bool quit;
    unsigned generation;
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
        return 0;
    const uint64_t remaining = state->human_active_until_msec - now;
    return remaining > INT32_MAX ? INT32_MAX : (int)remaining;
}

static void on_signal(int signal_number) {
    (void)signal_number;
    signal_stop = 1;
}

static void error_set(struct rpc_error* error, const char* code, const char* message) {
    snprintf(error->code, sizeof(error->code), "%s", code ? code : "AGENTSEAT_ERROR");
    snprintf(error->message, sizeof(error->message), "%s", message ? message : "AgentSeat request failed");
}

static bool write_all(int fd, const void* data, size_t length) {
    const char* bytes = data;
    size_t written = 0;
    while (written < length) {
        ssize_t result = write(fd, bytes + written, length - written);
        if (result < 0 && errno == EINTR)
            continue;
        if (result <= 0)
            return false;
        written += (size_t)result;
    }
    return true;
}

static char* read_line_fd(int fd, size_t limit) {
    char* data = calloc(limit + 1, 1);
    if (!data)
        return NULL;
    size_t length = 0;
    while (length < limit) {
        ssize_t result = read(fd, data + length, limit - length);
        if (result < 0 && errno == EINTR)
            continue;
        if (result <= 0)
            break;
        length += (size_t)result;
        char* newline = memchr(data, '\n', length);
        if (newline) {
            *newline = '\0';
            return data;
        }
    }
    if (length == 0 || length == limit) {
        free(data);
        return NULL;
    }
    data[length] = '\0';
    return data;
}

static json_object* delivered_result(void) {
    json_object* result = json_object_new_object();
    json_object_object_add(result, "delivered", json_object_new_boolean(true));
    return result;
}

static void input_failure(struct rpc_error* error, const char* code, const char* message) {
    error_set(error, code, message && message[0] ? message : "Native AgentSeat input failed");
}

static const char* string_param(json_object* params, const char* key, bool allow_empty, struct rpc_error* error) {
    json_object* value = NULL;
    if (!json_object_object_get_ex(params, key, &value) || !json_object_is_type(value, json_type_string)) {
        error_set(error, "INVALID_PARAMS", "Required string parameter is missing");
        return NULL;
    }
    const char* text = json_object_get_string(value);
    if (!allow_empty && !text[0]) {
        error_set(error, "INVALID_PARAMS", "String parameter must not be empty");
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
        error_set(error, "INVALID_PARAMS", "Boolean parameter has the wrong type");
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
        error_set(error, "INVALID_PARAMS", "Optional string parameter has the wrong type");
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
        error_set(error, "SEAT_NOT_CREATED", "Call seat.create before using AgentSeat");
        return false;
    }
    return true;
}

static bool require_active(struct daemon_state* state, struct rpc_error* error) {
    if (!require_created(state, error))
        return false;
    if (state->paused) {
        error_set(error, "SEAT_PAUSED", "Agent seat is paused");
        return false;
    }
    const int remaining = human_remaining_ms(state);
    if (remaining > 0) {
        char message[160] = {0};
        snprintf(message, sizeof(message), "Human input has priority; retry after %d ms of quiet", remaining);
        error_set(error, "HUMAN_ACTIVE", message);
        return false;
    }
    return true;
}

static bool require_focus_lease(struct daemon_state* state, struct rpc_error* error) {
    if (!require_active(state, error))
        return false;
    if (!state->focused_window[0] || !lease_contains(state, state->focused_window)) {
        error_set(error, "NO_FOCUS", "Focus a leased window before sending input");
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

static json_object* windows_json(const struct daemon_state* state) {
    json_object* windows = json_object_new_array();
    json_object* window = json_object_new_object();
    json_object_object_add(window, "id", json_object_new_string("agentseat:root"));
    json_object_object_add(window, "title", json_object_new_string(state->window_title));
    json_object_object_add(window, "app_id", json_object_new_string(state->app_id));
    json_object_object_add(window, "class", json_object_new_string(state->app_id));
    json_object_object_add(window, "pid", NULL);
    json_object_object_add(window, "workspace", json_object_new_int(state->workspace));
    json_object_object_add(window, "x", json_object_new_int(0));
    json_object_object_add(window, "y", json_object_new_int(0));
    json_object_object_add(window, "width", json_object_new_int(state->output_width));
    json_object_object_add(window, "height", json_object_new_int(state->output_height));
    json_object_object_add(window, "mapped", json_object_new_boolean(true));
    json_object_object_add(window, "hidden", json_object_new_boolean(false));
    json_object_object_add(window, "xwayland", json_object_new_boolean(state->xwayland));
    json_object_object_add(window, "leased", json_object_new_boolean(lease_contains(state, "agentseat:root")));
    json_object_object_add(
        window,
        "focused",
        json_object_new_boolean(strcmp(state->focused_window, "agentseat:root") == 0));
    json_object_array_add(windows, window);
    return windows;
}

static void seat_destroy(struct daemon_state* state) {
    if (state->created)
        agentseat_input_destroy(state->input);
    leases_clear(state);
    state->created = false;
    state->paused = true;
}

static json_object* dispatch(struct daemon_state* state, struct rpc_error* error, const char* method, json_object* params) {
    if (strcmp(method, "version") == 0) {
        json_object* result = json_object_new_object();
        json_object_object_add(result, "name", json_object_new_string("agentseatd"));
        json_object_object_add(result, "version", json_object_new_string(AGENTSEATD_VERSION));
        json_object_object_add(result, "api_version", json_object_new_string("1"));
        json_object_object_add(result, "implementation", json_object_new_string("native-c-event-loop"));
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
            error_set(error, "TEXT_TOO_LARGE", "Activity text exceeds the AgentSeat collaboration limit");
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
            error_set(error, "TEXT_TOO_LARGE", "Chat text exceeds the AgentSeat collaboration limit");
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
                error_set(error, "INVALID_PARAMS", "after_id must be a non-negative integer");
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
        }
        json_object* result = json_object_new_object();
        json_object_object_add(result, "changed", json_object_new_boolean(true));
        json_object_object_add(result, "seat", seat_json(state));
        return result;
    }
    if (strcmp(method, "seat.destroy") == 0) {
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
    if (strcmp(method, "window.lease") == 0 || strcmp(method, "window.release") == 0 || strcmp(method, "window.focus") == 0 ||
        strcmp(method, "window.move") == 0 || strcmp(method, "window.resize") == 0 || strcmp(method, "capture.window") == 0) {
        if (!require_created(state, error))
            return NULL;
        const char* window_id = string_param(params, "window_id", false, error);
        if (!window_id)
            return NULL;
        if (strcmp(method, "window.lease") != 0 && !lease_contains(state, window_id)) {
            error_set(error, "LEASE_REQUIRED", "Window is not leased to AgentSeat");
            return NULL;
        }
        if (strcmp(window_id, "agentseat:root") != 0) {
            error_set(error, "WINDOW_NOT_FOUND", "The single-app micro-host has no such window");
            return NULL;
        }
        if (strcmp(method, "capture.window") == 0) {
            error_set(error, "CAPTURE_VIA_SCREENCOPY", "Capture the micro-host output through wlr-screencopy");
            return NULL;
        }
        json_object* result = json_object_new_object();
        json_object_object_add(result, "window_id", json_object_new_string(window_id));
        json_object_object_add(result, "backend", json_object_new_string("single-app-microhost"));
        if (strcmp(method, "window.lease") == 0 && !lease_add(state, window_id)) {
            json_object_put(result);
            error_set(error, "LEASE_LIMIT", "Too many windows are mapped in the wrapped application");
            return NULL;
        }
        if (strcmp(method, "window.release") == 0)
            lease_remove(state, window_id);
        if (strcmp(method, "window.focus") == 0)
            snprintf(state->focused_window, sizeof(state->focused_window), "%s", window_id);
        return result;
    }
    if (strcmp(method, "pointer.move_absolute") == 0) {
        if (!require_active(state, error))
            return NULL;
        json_object* x_value = NULL;
        json_object* y_value = NULL;
        if (!json_object_object_get_ex(params, "x", &x_value) || !json_object_object_get_ex(params, "y", &y_value)) {
            error_set(error, "INVALID_PARAMS", "pointer.move_absolute requires x and y");
            return NULL;
        }
        const double x = json_object_get_double(x_value);
        const double y = json_object_get_double(y_value);
        if (x < 0 || x > 1 || y < 0 || y > 1) {
            error_set(error, "INVALID_PARAMS", "x and y must be normalized from 0 to 1");
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
    if (strcmp(method, "pointer.button") == 0) {
        if (!require_focus_lease(state, error))
            return NULL;
        json_object* button_value = NULL;
        bool pressed = false;
        if (!json_object_object_get_ex(params, "button", &button_value) || !bool_param(params, "pressed", false, &pressed, error)) {
            if (!error->code[0])
                error_set(error, "INVALID_PARAMS", "pointer.button requires button and pressed");
            return NULL;
        }
        const int button = json_object_get_int(button_value);
        if (button < 1 || button > 1023) {
            error_set(error, "INVALID_PARAMS", "button must be an integer from 1 to 1023");
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
                error_set(error, "INVALID_PARAMS", "pointer.scroll requires axis and value120");
            return NULL;
        }
        if (strcmp(axis, "vertical") != 0 && strcmp(axis, "horizontal") != 0) {
            error_set(error, "INVALID_PARAMS", "axis must be vertical or horizontal");
            return NULL;
        }
        const int value120 = json_object_get_int(value);
        if (value120 < -12000 || value120 > 12000) {
            error_set(error, "INVALID_PARAMS", "value120 must be from -12000 to 12000");
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
    if (strcmp(method, "keyboard.type") == 0) {
        if (!require_focus_lease(state, error))
            return NULL;
        const char* text = string_param(params, "text", true, error);
        if (!text)
            return NULL;
        if (strlen(text) > MAX_TYPE_BYTES) {
            error_set(error, "TEXT_TOO_LARGE", "text exceeds the native AgentSeat input limit");
            return NULL;
        }
        json_object* interval_value = NULL;
        const int interval = json_object_object_get_ex(params, "interval_ms", &interval_value) ? json_object_get_int(interval_value) : 4;
        if (interval < 0 || interval > 10000) {
            error_set(error, "INVALID_PARAMS", "interval_ms must be from 0 to 10000");
            return NULL;
        }
        size_t typed = 0;
        bool unsupported = false;
        char message[512] = {0};
        if (!agentseat_input_type(
                state->input,
                text,
                interval,
                &typed,
                &unsupported,
                message,
                sizeof(message))) {
            input_failure(error, unsupported ? "UNSUPPORTED_CHARACTER" : "KEYBOARD_TYPE_FAILED", message);
            return NULL;
        }
        json_object* result = json_object_new_object();
        json_object_object_add(result, "typed", json_object_new_int64((int64_t)typed));
        return result;
    }
    if (strcmp(method, "keyboard.key") == 0) {
        if (!require_focus_lease(state, error))
            return NULL;
        json_object* key_value = NULL;
        bool pressed = false;
        if (!json_object_object_get_ex(params, "keycode", &key_value) ||
            !bool_param(params, "pressed", false, &pressed, error)) {
            if (!error->code[0])
                error_set(error, "INVALID_PARAMS", "keyboard.key requires keycode and pressed");
            return NULL;
        }
        const int keycode = json_object_get_int(key_value);
        if (keycode < 0 || keycode >= 768) {
            error_set(error, "INVALID_PARAMS", "keycode must be an evdev code from 0 to 767");
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
    error_set(error, "METHOD_NOT_FOUND", "Unknown native AgentSeat method");
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
    json_object* params = NULL;
    bool owns_params = false;
    if (!json_object_is_type(request, json_type_object) || !json_object_object_get_ex(request, "method", &method_value) ||
        !json_object_is_type(method_value, json_type_string)) {
        error_set(&error, "INVALID_REQUEST", "method must be a non-empty string");
    } else if (!json_object_object_get_ex(request, "params", &params)) {
        params = json_object_new_object();
        owns_params = true;
    } else if (!json_object_is_type(params, json_type_object)) {
        error_set(&error, "INVALID_PARAMS", "params must be an object");
    }

    json_object* result = NULL;
    if (!error.code[0])
        result = dispatch(state, &error, json_object_get_string(method_value), params);
    if (owns_params)
        json_object_put(params);

    if (result)
        json_object_object_add(response, "result", result);
    else {
        json_object* failure = json_object_new_object();
        json_object_object_add(failure, "code", json_object_new_string(error.code[0] ? error.code : "INTERNAL_ERROR"));
        json_object_object_add(failure, "message", json_object_new_string(error.message[0] ? error.message : "Native AgentSeat request failed"));
        json_object_object_add(response, "error", failure);
    }
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
        error_set(error, "SOCKET_FAILED", "Cannot create AgentSeat runtime directory");
        return false;
    }
    unlink(state->socket_path);
    state->listen_fd = socket(AF_UNIX, SOCK_STREAM | SOCK_CLOEXEC, 0);
    if (state->listen_fd < 0) {
        error_set(error, "SOCKET_FAILED", "Cannot create AgentSeat Unix socket");
        return false;
    }
    struct sockaddr_un address = {.sun_family = AF_UNIX};
    if (strlen(state->socket_path) >= sizeof(address.sun_path)) {
        error_set(error, "SOCKET_FAILED", "AgentSeat socket path is too long");
        return false;
    }
    strcpy(address.sun_path, state->socket_path);
    if (bind(state->listen_fd, (struct sockaddr*)&address, sizeof(address)) != 0 || listen(state->listen_fd, 16) != 0) {
        error_set(error, "SOCKET_FAILED", "Cannot bind AgentSeat Unix socket");
        return false;
    }
    chmod(state->socket_path, 0600);

    if (!ensure_parent(state->activity_socket_path)) {
        error_set(error, "SOCKET_FAILED", "Cannot create AgentSeat activity runtime directory");
        return false;
    }
    unlink(state->activity_socket_path);
    state->activity_fd = socket(AF_UNIX, SOCK_DGRAM | SOCK_CLOEXEC | SOCK_NONBLOCK, 0);
    if (state->activity_fd < 0) {
        error_set(error, "SOCKET_FAILED", "Cannot create AgentSeat human activity socket");
        return false;
    }
    struct sockaddr_un activity_address = {.sun_family = AF_UNIX};
    if (strlen(state->activity_socket_path) >= sizeof(activity_address.sun_path)) {
        error_set(error, "SOCKET_FAILED", "AgentSeat activity socket path is too long");
        return false;
    }
    strcpy(activity_address.sun_path, state->activity_socket_path);
    if (bind(state->activity_fd, (struct sockaddr*)&activity_address, sizeof(activity_address)) != 0) {
        error_set(error, "SOCKET_FAILED", "Cannot bind AgentSeat human activity socket");
        return false;
    }
    chmod(state->activity_socket_path, 0600);
    return true;
}

static void handle_client(struct daemon_state* state, int client) {
    struct ucred credentials = {0};
    socklen_t credentials_size = sizeof(credentials);
    if (getsockopt(client, SOL_SOCKET, SO_PEERCRED, &credentials, &credentials_size) != 0 || credentials.uid != getuid())
        return;
    char* line = read_line_fd(client, MAX_REQUEST);
    json_object* request = line ? json_tokener_parse(line) : NULL;
    free(line);
    if (!request)
        request = json_object_new_object();
    json_object* response = response_for(state, request);
    json_object_put(request);
    const char* encoded = json_object_to_json_string_ext(response, JSON_C_TO_STRING_PLAIN);
    write_all(client, encoded, strlen(encoded));
    write_all(client, "\n", 1);
    json_object_put(response);
}

static int run_server(struct daemon_state* state) {
    const int input_fd = agentseat_input_fd(state->input);
    while (!state->quit && !signal_stop) {
        struct pollfd poll_fds[3] = {
            {.fd = state->listen_fd, .events = POLLIN},
            {.fd = input_fd, .events = POLLIN},
            {.fd = state->activity_fd, .events = POLLIN},
        };
        int result = poll(poll_fds, 3, 500);
        if (result < 0 && errno == EINTR)
            continue;
        if (result < 0)
            return 1;
        if (result == 0)
            continue;
        if (poll_fds[1].revents & (POLLERR | POLLHUP | POLLNVAL))
            return 2;
        if (poll_fds[1].revents & POLLIN) {
            char message[512] = {0};
            if (!agentseat_input_dispatch(state->input, message, sizeof(message))) {
                fprintf(stderr, "agentseatd: input dispatch failed: %s\n", message);
                return 2;
            }
        }
        if (poll_fds[2].revents & POLLIN) {
            char buffer[64];
            while (recv(state->activity_fd, buffer, sizeof(buffer), MSG_DONTWAIT) > 0) {
                state->human_active_until_msec = monotonic_msec() + (uint64_t)state->human_grace_ms;
            }
        }
        if (poll_fds[0].revents & POLLIN) {
            int client = accept4(state->listen_fd, NULL, NULL, SOCK_CLOEXEC);
            if (client >= 0) {
                handle_client(state, client);
                close(client);
            }
        }
    }
    return 0;
}

static void usage(const char* program) {
    fprintf(stderr, "usage: %s --socket PATH --activity-socket PATH [--human-grace-ms N] [--workspace N] [--seat-id ID] [--seat-name NAME] [--app-id ID] [--window-title TITLE] [--xwayland] [--width N] [--height N]\n", program);
}

int main(int argc, char** argv) {
    struct daemon_state state = {
        .workspace = 1,
        .listen_fd = -1,
        .activity_fd = -1,
        .human_grace_ms = 1500,
        .paused = true,
        .pointer_x = 0.5,
        .pointer_y = 0.5,
        .seat_id = "agent:agentseat",
        .seat_name = "AgentSeat Agent",
        .app_id = "agentseat.application",
        .window_title = "AgentSeat Application",
        .output_width = 1280,
        .output_height = 720,
        .activity_summary = "Waiting for the Agent",
    };
    for (int i = 1; i < argc; ++i) {
        if (strcmp(argv[i], "--socket") == 0 && i + 1 < argc)
            state.socket_path = argv[++i];
        else if (strcmp(argv[i], "--activity-socket") == 0 && i + 1 < argc)
            state.activity_socket_path = argv[++i];
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
    if (!state.socket_path || !state.activity_socket_path || state.workspace <= 0 || state.human_grace_ms < 0 || state.human_grace_ms > 60000 ||
        state.output_width <= 0 || state.output_height <= 0) {
        usage(argv[0]);
        return 2;
    }

    signal(SIGINT, on_signal);
    signal(SIGTERM, on_signal);
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
