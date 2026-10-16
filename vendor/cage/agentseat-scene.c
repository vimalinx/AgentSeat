#include "config.h"
#include <math.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <wlr/types/wlr_output_layout.h>
#include <wlr/types/wlr_scene.h>
#include <wlr/types/wlr_seat.h>
#include "agentseat-control-v1-server-protocol.h"
#include "agentseat-scene.h"
#include "output.h"
#include "seat.h"
#include "view.h"
#include "xdg_shell.h"
#if CAGE_HAS_XWAYLAND
#include "xwayland.h"
#endif

#define SCENE_WINDOWS 256
struct window_info {
    uint32_t id, parent, pid, flags;
    int32_t x, y, width, height;
    char title[257], app_id[257];
};
struct scene_snapshot {
    struct window_info windows[SCENE_WINDOWS];
    size_t count;
    uint32_t focused, scale_120, truncated;
    int32_t width, height, pixel_width, pixel_height;
};
struct cg_scene {
    struct cg_seat *seat;
    struct wl_event_source *idle;
    struct wl_list popups;
    struct scene_snapshot previous;
    uint32_t version, next_id;
};

static void copy_text(char *out, const char *text) {
    if (!text)
        return;
    size_t length = strlen(text);
    if (length > 256) {
        length = 256;
        while (length && ((unsigned char)text[length] & 0xc0) == 0x80)
            length--;
    }
    memcpy(out, text, length);
    out[length] = '\0';
}

static void publish(struct cg_seat *seat, bool force) {
    if (!seat->scene || !seat->control_resource)
        return;
    struct scene_snapshot snapshot = {0};
    struct cg_view *view, *focused = seat_get_focus(seat);
    struct wlr_box box;
    wlr_output_layout_get_box(seat->server->output_layout, NULL, &box);
    snapshot.width = box.width;
    snapshot.height = box.height;
    struct cg_output *output;
    wl_list_for_each(output, &seat->server->outputs, link) {
        snapshot.pixel_width = output->wlr_output->width;
        snapshot.pixel_height = output->wlr_output->height;
        snapshot.scale_120 = (uint32_t)lroundf(output->wlr_output->scale * 120);
        break;
    }
    wl_list_for_each(view, &seat->server->views, link) {
        if (view_is_agentseat_overlay(view) || !view->wlr_surface)
            continue;
        if (snapshot.count == SCENE_WINDOWS) {
            snapshot.truncated = 1;
            break;
        }
        struct window_info *info = &snapshot.windows[snapshot.count++];
        info->id = view->agentseat_id;
        info->x = view->lx;
        info->y = view->ly;
        view->impl->get_geometry(view, &info->width, &info->height);
        if (view == focused)
            snapshot.focused = info->id;
        if (view == seat->server->primary_view)
            info->flags |= 1;
        char *title = view_get_title(view);
        copy_text(info->title, title);
        free(title);
        if (view->type == CAGE_XDG_SHELL_VIEW) {
            struct cg_xdg_shell_view *xdg = wl_container_of(view, xdg, view);
            copy_text(info->app_id, xdg->xdg_toplevel->app_id);
            pid_t pid = 0;
            wl_client_get_credentials(wl_resource_get_client(view->wlr_surface->resource), &pid, NULL, NULL);
            info->pid = (uint32_t)pid;
            info->flags |= 4; // kernel-verified Wayland peer PID
        }
#if CAGE_HAS_XWAYLAND
        else {
            struct cg_xwayland_view *x11 = xwayland_view_from_view(view);
            info->flags |= 2;
            if (x11->xwayland_surface->override_redirect) info->flags |= 8;
            info->pid = (uint32_t)x11->xwayland_surface->pid;
            copy_text(info->app_id, x11->xwayland_surface->class);
        }
#endif
        struct cg_view *parent;
        wl_list_for_each(parent, &seat->server->views, link) {
            if (parent != view && view_is_transient_for(view, parent)) {
                info->parent = parent->agentseat_id;
                break;
            }
        }
    }
    struct cg_xdg_popup *popup;
    wl_list_for_each(popup, &seat->scene->popups, scene_link) {
        struct wlr_surface *surface = popup->xdg_popup->base->surface;
        if (!surface->mapped) continue;
        if (snapshot.count == SCENE_WINDOWS) { snapshot.truncated = 1; break; }
        struct window_info *info = &snapshot.windows[snapshot.count++];
        info->id = popup->agentseat_id; info->parent = popup->parent_id; info->flags = 8 | 4;
        struct wlr_scene_tree *tree = popup->xdg_popup->base->data;
        wlr_scene_node_coords(&tree->node, &info->x, &info->y);
        info->width = popup->xdg_popup->current.geometry.width;
        info->height = popup->xdg_popup->current.geometry.height;
        pid_t pid = 0;
        wl_client_get_credentials(wl_resource_get_client(surface->resource), &pid, NULL, NULL);
        info->pid = (uint32_t)pid;
        if (seat->seat->keyboard_state.focused_surface == surface) snapshot.focused = info->id;
    }
    if (memcmp(&snapshot, &seat->scene->previous, sizeof(snapshot)) != 0 || !seat->scene->version) {
        seat->scene->previous = snapshot;
        seat->scene->version++;
    } else if (!force) {
        return;
    }
    struct wl_resource *resource = seat->control_resource;
    agentseat_control_v1_send_scene_begin(resource, seat->scene->version);
    for (size_t i = 0; i < snapshot.count; ++i) {
        struct window_info *w = &snapshot.windows[i];
        agentseat_control_v1_send_window_info(resource, w->id, w->parent, w->pid, w->flags,
            w->x, w->y, w->width, w->height, w->title, w->app_id);
    }
    agentseat_control_v1_send_scene_done(resource, seat->scene->version, snapshot.focused,
        snapshot.width, snapshot.height, snapshot.pixel_width, snapshot.pixel_height,
        snapshot.scale_120, snapshot.truncated);
}

static void publish_idle(void *data) {
    struct cg_seat *seat = data;
    seat->scene->idle = NULL;
    publish(seat, false);
}

void cg_scene_changed(struct cg_seat *seat) {
    if (seat && seat->scene && seat->control_resource && !seat->scene->idle)
        seat->scene->idle = wl_event_loop_add_idle(wl_display_get_event_loop(seat->server->wl_display), publish_idle, seat);
}

struct cg_scene *cg_scene_create(struct cg_seat *seat) {
    struct cg_scene *scene = calloc(1, sizeof(*scene));
    if (scene) {
        scene->seat = seat;
        wl_list_init(&scene->popups);
    }
    return scene;
}

void cg_scene_destroy(struct cg_seat *seat) {
    if (!seat->scene)
        return;
    if (seat->scene->idle)
        wl_event_source_remove(seat->scene->idle);
    free(seat->scene);
    seat->scene = NULL;
}

uint32_t cg_scene_allocate_id(struct cg_seat *seat) {
    if (!seat->scene || seat->scene->next_id == UINT32_MAX)
        return 0;
    return ++seat->scene->next_id;
}

void cg_scene_snapshot(struct wl_client *client, struct wl_resource *resource) {
    (void)client;
    publish(wl_resource_get_user_data(resource), true);
}

void cg_scene_window_action(struct wl_client *client, struct wl_resource *resource,
    uint32_t epoch, uint32_t token, uint32_t window_id, uint32_t action, int32_t first, int32_t second) {
    struct cg_seat *seat = wl_resource_get_user_data(resource);
    uint32_t status = 1; // unavailable/stale gate
    if (seat->control_allowed && epoch == seat->control_epoch &&
        resource == seat->control_resource && wl_resource_get_client(resource) == client) {
        status = 2; // unknown window
        struct cg_view *view;
        wl_list_for_each(view, &seat->server->views, link) {
            if (view->agentseat_id != window_id || view_is_agentseat_overlay(view))
                continue;
            if (action == 1) {
                seat_set_focus(seat, view);
                status = seat_get_focus(seat) == view ? 0 : 3;
            } else if (action == 4) {
                view->impl->close(view);
                status = 0; // delivered; the client may keep a save/cancel dialog open
            } else if (view == seat->server->primary_view) {
                status = 3; // outer geometry is owned by the host
            } else if (action == 2) {
                struct wlr_box box;
                wlr_output_layout_get_box(seat->server->output_layout, NULL, &box);
                int width = 0, height = 0;
                view->impl->get_geometry(view, &width, &height);
                if (first < 0 || second < 0 || first + width > box.width || second + height > box.height) {
                    status = 3;
                } else {
                    view->lx = first;
                    view->ly = second;
                    view->user_positioned = true;
                    wlr_scene_node_set_position(&view->scene_tree->node, first, second);
#if CAGE_HAS_XWAYLAND
                    if (view->type == CAGE_XWAYLAND_VIEW)
                        view->impl->configure(view, width, height, false);
#endif
                    status = 0;
                }
            } else if (action == 3 && first > 0 && second > 0) {
                view_configure_requested(view, first, second);
                status = 0;
            } else {
                status = 3;
            }
            break;
        }
    }
    publish(seat, false);
    agentseat_control_v1_send_window_action_done(resource, token, status);
}

void cg_scene_render_once(struct wl_client *client, struct wl_resource *resource) {
    (void)client;
    struct cg_seat *seat = wl_resource_get_user_data(resource);
    output_render_all(seat->server);
}

void cg_scene_add_popup(struct cg_seat *seat, struct cg_xdg_popup *popup, struct cg_view *view) {
    popup->server = seat->server;
    popup->agentseat_id = cg_scene_allocate_id(seat);
    popup->parent_id = view->agentseat_id;
    struct cg_xdg_popup *parent;
    wl_list_for_each(parent, &seat->scene->popups, scene_link) {
        if (parent->xdg_popup->base->surface == popup->xdg_popup->parent) {
            popup->parent_id = parent->agentseat_id; break;
        }
    }
    wl_list_insert(&seat->scene->popups, &popup->scene_link);
    cg_scene_changed(seat);
}

uint32_t cg_scene_current_version(struct cg_seat *seat) {
    publish(seat, false);
    return seat->scene ? seat->scene->version : 0;
}
