#ifndef CG_APPLICATION_LIFETIME_H
#define CG_APPLICATION_LIFETIME_H
#include <stdbool.h>
struct cg_server;
struct cg_view;
void application_lifetime_track(struct cg_view *view);
bool application_lifetime_alive(struct cg_server *server);
void application_lifetime_finish(struct cg_server *server);
#endif
