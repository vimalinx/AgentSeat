#ifndef CG_HOST_WINDOW_H
#define CG_HOST_WINDOW_H

#include <stdbool.h>

struct cg_server;
struct wlr_output;

bool host_window_create(struct cg_server *server);
void host_window_sync_visibility(struct cg_server *server);
bool host_window_can_render(struct cg_server *server);
void host_window_finish(struct cg_server *server);
bool host_window_set_title(struct cg_server *server, struct wlr_output *output, const char *title);

#endif
