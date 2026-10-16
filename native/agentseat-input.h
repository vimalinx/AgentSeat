#pragma once

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

struct agentseat_input;
#define AGENTSEAT_MAX_WINDOWS 256
struct agentseat_window {
    uint32_t id, parent_id, pid, flags;
    int x, y, width, height;
    char title[257], app_id[257];
};
struct agentseat_scene {
    uint32_t version, focused_id, scale_120;
    int width, height, pixel_width, pixel_height;
    bool truncated;
    size_t count;
    struct agentseat_window windows[AGENTSEAT_MAX_WINDOWS];
};
const struct agentseat_scene* agentseat_input_scene(const struct agentseat_input* input);
void agentseat_input_window_action(struct agentseat_input* input, uint32_t token,
    uint32_t window_id, uint32_t action, int first, int second);


struct agentseat_input_status {
    bool created;
    bool paused;
    uint32_t control_epoch;
    uint32_t control_protocol_version;
    uint32_t window_action_token, window_action_status;
    uint32_t clipboard_token, clipboard_status, clipboard_transfers, clipboard_bytes;
    uint32_t authorization_token, authorization_epoch;
    bool authorization_allowed;
    bool gate_allowed;
    bool human_held;
    unsigned human_quiet_ms;
    uint32_t seat_global;
    double pointer_x;
    double pointer_y;
};

struct agentseat_input* agentseat_input_open(char* error, size_t error_size);
void agentseat_input_close(struct agentseat_input* input);
int agentseat_input_fd(const struct agentseat_input* input);
bool agentseat_input_dispatch(struct agentseat_input* input, char* error, size_t error_size);

bool agentseat_input_create(
    struct agentseat_input* input,
    const char* seat_id,
    const char* seat_name,
    char* error,
    size_t error_size);
void agentseat_input_destroy(struct agentseat_input* input);
bool agentseat_input_pause(struct agentseat_input* input, char* error, size_t error_size);
bool agentseat_input_resume(struct agentseat_input* input, char* error, size_t error_size);
void agentseat_input_status(const struct agentseat_input* input, struct agentseat_input_status* status);

bool agentseat_input_move_absolute(
    struct agentseat_input* input,
    double x,
    double y,
    char* error,
    size_t error_size);
bool agentseat_input_button(
    struct agentseat_input* input,
    uint32_t button,
    bool pressed,
    char* error,
    size_t error_size);
bool agentseat_input_scroll(
    struct agentseat_input* input,
    bool horizontal,
    int value120,
    char* error,
    size_t error_size);
bool agentseat_input_key(
    struct agentseat_input* input,
    uint32_t keycode,
    bool pressed,
    char* error,
    size_t error_size);
bool agentseat_input_prepare_text(
    struct agentseat_input* input, const char* text,
    uint32_t** output, size_t* count, bool* unsupported,
    char* error, size_t error_size);
bool agentseat_input_type_codepoint(
    struct agentseat_input* input, uint32_t codepoint,
    char* error, size_t error_size);
void agentseat_input_release_all(struct agentseat_input* input);

void agentseat_input_arm(struct agentseat_input* input, uint32_t epoch);
void agentseat_input_revoke(struct agentseat_input* input);

void agentseat_input_hold_timeout(struct agentseat_input* input, unsigned milliseconds);

#include <json-c/json.h>
struct as_capture;
struct as_capture* agentseat_input_capture(struct agentseat_input* input);
json_object* agentseat_input_capture_start(struct agentseat_input* input, const char* socket_path);

void agentseat_input_geometry(struct agentseat_input* input, uint32_t version);

void agentseat_input_check_authority(struct agentseat_input*, uint32_t token);

bool agentseat_input_paste_offer(struct agentseat_input*, uint32_t token, const char* text, char* error, size_t error_size);
