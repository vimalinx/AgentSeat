/* Relay the standard text-input-v3 protocol to the user's compositor. No
 * engine selection, keystroke synthesis, clipboard or input-method daemon. */
#define _POSIX_C_SOURCE 200809L
#include <stdlib.h>
#include <string.h>
#include <math.h>
#include <wayland-client.h>
#include <wlr/backend/wayland.h>
#include <wlr/types/wlr_compositor.h>
#include <wlr/types/wlr_scene.h>
#include <wlr/types/wlr_text_input_v3.h>
#include "host-text-input.h"
#include "text-input-client-protocol.h"
#include "server.h"
#include "seat.h"
#include "output.h"

struct inner_input {
    struct wl_list link;
    struct cg_host_text_input *bridge;
    struct wlr_text_input_v3 *input;
    struct wl_listener commit, enable, disable, destroy;
};
struct cg_host_text_input {
    struct cg_server *server;
    struct wl_display *display;
    struct wl_surface *surface;
    struct wl_registry *registry;
    struct wl_seat *seat;
    struct zwp_text_input_manager_v3 *parent_manager;
    struct zwp_text_input_v3 *parent;
    struct wlr_text_input_manager_v3 *manager;
    struct wl_listener new_input, focus;
    struct wl_list inputs;
    struct inner_input *active;
    uint32_t serial, inner_serial;
    bool entered, enabled;
    char *preedit, *text;
    bool has_preedit, has_text, has_delete;
    int32_t begin, end;
    uint32_t before, after;
};
static void reset_pending(struct cg_host_text_input *b) {
    free(b->preedit); free(b->text);
    b->preedit = b->text = NULL;
    b->has_preedit = b->has_text = b->has_delete = false;
}
static void parent_commit(struct cg_host_text_input *b) {
    zwp_text_input_v3_commit(b->parent);
    b->serial++;
    wl_display_flush(b->display);
}
static void deactivate(struct cg_host_text_input *b) {
    if (b->enabled) {
        zwp_text_input_v3_disable(b->parent);
        parent_commit(b);
    }
    b->enabled = false;
    b->active = NULL;
    reset_pending(b);
}
struct surface_position { struct wlr_surface *surface; int x, y; };
static void find_position(struct wlr_scene_buffer *buffer, int x, int y, void *data) {
    struct surface_position *p = data;
    struct wlr_scene_surface *s = wlr_scene_surface_try_from_buffer(buffer);
    if (s && s->surface == p->surface) { p->x = x; p->y = y; }
}
static void forward_state(struct inner_input *i) {
    struct cg_host_text_input *b = i->bridge;
    struct wlr_text_input_v3 *t = i->input;
    if (!b->entered || !t->focused_surface || !t->current_enabled) {
        if (b->active == i) deactivate(b);
        return;
    }
    if (b->active != i) {
        deactivate(b);
        b->active = i;
        zwp_text_input_v3_enable(b->parent);
        b->enabled = true;
    }
    struct wlr_text_input_v3_state *s = &t->current;
    if (t->active_features & WLR_TEXT_INPUT_V3_FEATURE_SURROUNDING_TEXT)
        zwp_text_input_v3_set_surrounding_text(b->parent, s->surrounding.text ? s->surrounding.text : "", s->surrounding.cursor, s->surrounding.anchor);
    zwp_text_input_v3_set_text_change_cause(b->parent, s->text_change_cause);
    if (t->active_features & WLR_TEXT_INPUT_V3_FEATURE_CONTENT_TYPE)
        zwp_text_input_v3_set_content_type(b->parent, s->content_type.hint, s->content_type.purpose);
    if (t->active_features & WLR_TEXT_INPUT_V3_FEATURE_CURSOR_RECTANGLE) {
        struct surface_position pos = {.surface = t->focused_surface};
        wlr_scene_node_for_each_buffer(&b->server->scene->tree.node, find_position, &pos);
        double scale = 1;
        struct cg_output *output;
        wl_list_for_each(output, &b->server->outputs, link) {
            if (wlr_output_is_wl(output->wlr_output) && wlr_wl_output_get_surface(output->wlr_output) == b->surface) {
                scale = output->wlr_output->scale / output->host_coordinate_scale;
                break;
            }
        }
        struct wlr_box *r = &s->cursor_rectangle;
        zwp_text_input_v3_set_cursor_rectangle(b->parent,
            (int)floor((pos.x + r->x) * scale), (int)floor((pos.y + r->y) * scale),
            (int)ceil(r->width * scale), (int)ceil(r->height * scale));
    }
    b->inner_serial = t->current_serial;
    parent_commit(b);
}
static void sync_focus(struct cg_host_text_input *b) {
    struct wlr_surface *surface = b->entered ? b->server->seat->seat->keyboard_state.focused_surface : NULL;
    struct inner_input *i;
    wl_list_for_each(i, &b->inputs, link) {
        struct wlr_text_input_v3 *t = i->input;
        bool match = surface && t->seat == b->server->seat->seat &&
            wl_resource_get_client(t->resource) == wl_resource_get_client(surface->resource);
        if (t->focused_surface && (!match || t->focused_surface != surface)) {
            if (b->active == i) deactivate(b);
            wlr_text_input_v3_send_leave(t);
        }
        if (match && !t->focused_surface) wlr_text_input_v3_send_enter(t, surface);
    }
}
static void focus_changed(struct wl_listener *listener, void *data) {
    struct cg_host_text_input *b = wl_container_of(listener, b, focus);
    sync_focus(b);
}
static void input_committed(struct wl_listener *listener, void *data) {
    struct inner_input *i = wl_container_of(listener, i, commit);
    forward_state(i);
}
static void input_enabled(struct wl_listener *listener, void *data) {
    struct inner_input *i = wl_container_of(listener, i, enable);
    /* Re-enabling resets the parent's field state, even on the same surface. */
    if (i->bridge->active == i) deactivate(i->bridge);
    forward_state(i);
}
static void input_disabled(struct wl_listener *listener, void *data) {
    struct inner_input *i = wl_container_of(listener, i, disable);
    if (i->bridge->active == i) deactivate(i->bridge);
}
static void input_destroyed(struct wl_listener *listener, void *data) {
    struct inner_input *i = wl_container_of(listener, i, destroy);
    if (i->bridge->active == i) deactivate(i->bridge);
    wl_list_remove(&i->commit.link); wl_list_remove(&i->enable.link);
    wl_list_remove(&i->disable.link); wl_list_remove(&i->destroy.link);
    wl_list_remove(&i->link); free(i);
}
static void new_input(struct wl_listener *listener, void *data) {
    struct cg_host_text_input *b = wl_container_of(listener, b, new_input);
    struct inner_input *i = calloc(1, sizeof(*i));
    if (!i) return;
    i->bridge = b; i->input = data;
    i->commit.notify = input_committed; i->enable.notify = input_enabled;
    i->disable.notify = input_disabled; i->destroy.notify = input_destroyed;
    wl_signal_add(&i->input->events.commit, &i->commit);
    wl_signal_add(&i->input->events.enable, &i->enable);
    wl_signal_add(&i->input->events.disable, &i->disable);
    wl_signal_add(&i->input->events.destroy, &i->destroy);
    wl_list_insert(&b->inputs, &i->link);
    sync_focus(b);
}
static void parent_enter(void *data, struct zwp_text_input_v3 *t, struct wl_surface *surface) {
    struct cg_host_text_input *b = data;
    b->entered = surface == b->surface;
    sync_focus(b);
}
static void parent_leave(void *data, struct zwp_text_input_v3 *t, struct wl_surface *surface) {
    struct cg_host_text_input *b = data;
    b->entered = false;
    deactivate(b); sync_focus(b);
}
static void parent_preedit(void *data, struct zwp_text_input_v3 *t, const char *text, int32_t begin, int32_t end) {
    struct cg_host_text_input *b = data;
    free(b->preedit); b->preedit = text ? strdup(text) : NULL;
    b->has_preedit = true; b->begin = begin; b->end = end;
}
static void parent_text(void *data, struct zwp_text_input_v3 *t, const char *text) {
    struct cg_host_text_input *b = data;
    free(b->text); b->text = text ? strdup(text) : NULL; b->has_text = true;
}
static void parent_delete(void *data, struct zwp_text_input_v3 *t, uint32_t before, uint32_t after) {
    struct cg_host_text_input *b = data;
    b->has_delete = true; b->before = before; b->after = after;
}
static void parent_done(void *data, struct zwp_text_input_v3 *parent, uint32_t serial) {
    struct cg_host_text_input *b = data;
    struct wlr_text_input_v3 *t = b->active ? b->active->input : NULL;
    if (b->entered && t && t->current_enabled && t->focused_surface &&
        serial == b->serial && t->current_serial == b->inner_serial) {
        if (b->has_delete) wlr_text_input_v3_send_delete_surrounding_text(t, b->before, b->after);
        if (b->has_text) wlr_text_input_v3_send_commit_string(t, b->text);
        if (b->has_preedit) wlr_text_input_v3_send_preedit_string(t, b->preedit, b->begin, b->end);
        wlr_text_input_v3_send_done(t);
    }
    reset_pending(b);
}
static const struct zwp_text_input_v3_listener parent_listener = {
    .enter = parent_enter, .leave = parent_leave, .preedit_string = parent_preedit,
    .commit_string = parent_text, .delete_surrounding_text = parent_delete, .done = parent_done,
};
static void global(void *data, struct wl_registry *registry, uint32_t name, const char *interface, uint32_t version) {
    struct cg_host_text_input *b = data;
    if (!strcmp(interface, wl_seat_interface.name) && !b->seat)
        b->seat = wl_registry_bind(registry, name, &wl_seat_interface, 1);
    if (!strcmp(interface, zwp_text_input_manager_v3_interface.name) && !b->parent_manager)
        b->parent_manager = wl_registry_bind(registry, name, &zwp_text_input_manager_v3_interface, 1);
}
static void global_remove(void *data, struct wl_registry *registry, uint32_t name) {}
static const struct wl_registry_listener registry_listener = {.global = global, .global_remove = global_remove};
struct cg_host_text_input *host_text_input_create(struct cg_server *server, struct wl_display *display, struct wl_surface *surface) {
    struct cg_host_text_input *b = calloc(1, sizeof(*b));
    if (!b) return NULL;
    b->server = server; b->display = display; b->surface = surface;
    wl_list_init(&b->inputs);
    wl_list_init(&b->new_input.link); wl_list_init(&b->focus.link);
    b->registry = wl_display_get_registry(display);
    wl_registry_add_listener(b->registry, &registry_listener, b);
    if (wl_display_roundtrip(display) < 0) { host_text_input_destroy(b); return NULL; }
    /* Never advertise a dead input channel; toolkits can use their DBus modules
     * when the parent compositor does not support this protocol. */
    if (!b->seat || !b->parent_manager) return b;
    b->parent = zwp_text_input_manager_v3_get_text_input(b->parent_manager, b->seat);
    zwp_text_input_v3_add_listener(b->parent, &parent_listener, b);
    b->manager = wlr_text_input_manager_v3_create(server->wl_display);
    if (!b->manager) { host_text_input_destroy(b); return NULL; }
    b->new_input.notify = new_input;
    wl_signal_add(&b->manager->events.new_text_input, &b->new_input);
    b->focus.notify = focus_changed;
    wl_signal_add(&server->seat->seat->keyboard_state.events.focus_change, &b->focus);
    wlr_log(WLR_INFO, "Parent text-input-v3 relay available");
    return b;
}
void host_text_input_destroy(struct cg_host_text_input *b) {
    if (!b) return;
    struct inner_input *i, *tmp;
    wl_list_for_each_safe(i, tmp, &b->inputs, link) input_destroyed(&i->destroy, NULL);
    deactivate(b);
    wl_list_remove(&b->new_input.link); wl_list_remove(&b->focus.link);
    if (b->manager) wl_global_remove(b->manager->global);
    if (b->parent) zwp_text_input_v3_destroy(b->parent);
    if (b->parent_manager) zwp_text_input_manager_v3_destroy(b->parent_manager);
    if (b->seat) wl_seat_destroy(b->seat);
    if (b->registry) wl_registry_destroy(b->registry);
    free(b);
}
