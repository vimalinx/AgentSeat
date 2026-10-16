#pragma once
#include <stdint.h>
#include <json-c/json.h>
struct wl_display;
struct agentseat_control_v1;
struct as_capture;
struct as_capture *as_capture_create(struct wl_display *display);
void as_capture_destroy(struct as_capture *capture);
json_object *as_capture_start(struct as_capture *capture, struct agentseat_control_v1 *control,
    const char *socket_path, uint32_t geometry_version);
json_object *as_capture_get(struct as_capture *capture, const char *id);
void as_capture_tick(struct as_capture *capture);
uint64_t as_capture_deadline(struct as_capture *capture);

int as_capture_fd(struct as_capture *capture);
