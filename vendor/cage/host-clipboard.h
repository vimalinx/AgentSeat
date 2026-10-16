#pragma once
#include <stdbool.h>
struct cg_server;
struct wl_display;
struct wl_surface;
struct cg_host_clipboard;
struct cg_host_clipboard *host_clipboard_create(struct cg_server *, struct wl_display *, struct wl_surface *);
void host_clipboard_destroy(struct cg_host_clipboard *);
