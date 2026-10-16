#pragma once
#include <stdint.h>
struct cg_seat;
struct wl_resource;
void cg_clipboard_clear(struct cg_seat *seat);
void cg_clipboard_offer(struct cg_seat *seat, struct wl_resource *resource,
    uint32_t epoch, uint32_t token, int fd, uint32_t size);
