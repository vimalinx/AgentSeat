#pragma once

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

struct agentseat_input;

struct agentseat_input_status {
    bool created;
    bool paused;
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
bool agentseat_input_type(
    struct agentseat_input* input,
    const char* text,
    int interval_ms,
    size_t* typed,
    bool* unsupported,
    char* error,
    size_t error_size);
