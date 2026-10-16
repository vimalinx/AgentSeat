#pragma once
struct cg_seat;
struct wl_display;
struct cg_host_keyboard;
struct cg_host_keyboard *host_keyboard_create(struct cg_seat *, struct wl_display *);
void host_keyboard_destroy(struct cg_host_keyboard *);
