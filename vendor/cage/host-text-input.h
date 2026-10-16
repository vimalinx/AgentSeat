#pragma once
struct cg_server;
struct wl_display;
struct wl_surface;
struct cg_host_text_input;
struct cg_host_text_input *host_text_input_create(struct cg_server *, struct wl_display *, struct wl_surface *);
void host_text_input_destroy(struct cg_host_text_input *);
