#ifndef CG_AGENTSEAT_SCENE_H
#define CG_AGENTSEAT_SCENE_H
#include <stdint.h>
#include <wayland-server-core.h>
struct cg_seat;
struct cg_scene;
struct cg_xdg_popup;
struct cg_view;
void cg_scene_add_popup(struct cg_seat *seat, struct cg_xdg_popup *popup, struct cg_view *view);
struct cg_scene *cg_scene_create(struct cg_seat *seat);
void cg_scene_destroy(struct cg_seat *seat);
uint32_t cg_scene_allocate_id(struct cg_seat *seat);
uint32_t cg_scene_current_version(struct cg_seat *seat);
void cg_scene_changed(struct cg_seat *seat);
void cg_scene_snapshot(struct wl_client *client, struct wl_resource *resource);
void cg_scene_window_action(struct wl_client *client, struct wl_resource *resource,
    uint32_t epoch, uint32_t token, uint32_t window_id, uint32_t action, int32_t first, int32_t second);
void cg_scene_render_once(struct wl_client *client, struct wl_resource *resource);
#endif
