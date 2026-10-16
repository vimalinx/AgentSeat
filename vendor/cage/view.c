/*
 * Cage: A Wayland kiosk.
 *
 * Copyright (C) 2018-2021 Jente Hidskes
 *
 * See the LICENSE file accompanying this file.
 */

#define _POSIX_C_SOURCE 200809L

#include <assert.h>
#include <fcntl.h>
#include <math.h>
#include <stdio.h>
#include <stdbool.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include <wayland-server-core.h>
#include <wlr/types/wlr_foreign_toplevel_management_v1.h>
#include <wlr/types/wlr_output.h>
#include <wlr/types/wlr_scene.h>

#include "seat.h"
#include "host-window.h"
#include "application-lifetime.h"
#include "server.h"
#include "view.h"
#include "agentseat-scene.h"
#if CAGE_HAS_XWAYLAND
#include "xwayland.h"
#endif

char *
view_get_title(struct cg_view *view)
{
	const char *title = view->impl->get_title(view);
	if (!title) {
		return NULL;
	}
	return strndup(title, strlen(title));
}

bool
view_is_primary(struct cg_view *view)
{
	return view->impl->is_primary(view);
}

bool
view_is_transient_for(struct cg_view *child, struct cg_view *parent)
{
	if (!child || !parent || child == parent) {
		return false;
	}
	return child->impl->is_transient_for(child, parent);
}

bool
view_is_agentseat_overlay(struct cg_view *view)
{
	return view && view->agentseat_overlay;
}

bool
view_has_application_views(struct cg_server *server)
{
	return server->application_view_count > 0;
}

void
view_activate(struct cg_view *view, bool activate)
{
	view->impl->activate(view, activate);
	wlr_foreign_toplevel_handle_v1_set_activated(view->foreign_toplevel_handle, activate);
}

static void
view_center(struct cg_view *view, const struct wlr_box *container_box, const struct wlr_box *layout_box, int width,
	    int height)
{
	view->lx = container_box->x + (container_box->width - width) / 2;
	view->ly = container_box->y + (container_box->height - height) / 2;
	int max_x = layout_box->x + layout_box->width - width;
	int max_y = layout_box->y + layout_box->height - height;
	if (max_x < layout_box->x)
		max_x = layout_box->x;
	if (max_y < layout_box->y)
		max_y = layout_box->y;
	view->lx = view->lx < layout_box->x ? layout_box->x : (view->lx > max_x ? max_x : view->lx);
	view->ly = view->ly < layout_box->y ? layout_box->y : (view->ly > max_y ? max_y : view->ly);

	if (view->scene_tree) {
		wlr_scene_node_set_position(&view->scene_tree->node, view->lx, view->ly);
	}
}

static struct cg_view *
view_transient_parent(struct cg_view *view)
{
	struct cg_view *candidate;
	wl_list_for_each (candidate, &view->server->views, link) {
		if (!view_is_agentseat_overlay(candidate) && view_is_transient_for(view, candidate)) {
			return candidate;
		}
	}
	return NULL;
}

void
view_configure_requested(struct cg_view *view, int width, int height)
{
	struct wlr_box layout_box;
	wlr_output_layout_get_box(view->server->output_layout, NULL, &layout_box);
	if (view == view->server->primary_view) {
		/* Hyprland owns the outer window geometry. Make each application root
		 * consume that private output, while leaving transient dialogs natural. */
		view->lx = layout_box.x;
		view->ly = layout_box.y;
		if (view->scene_tree) {
			wlr_scene_node_set_position(&view->scene_tree->node, view->lx, view->ly);
		}
		view->impl->configure(view, layout_box.width, layout_box.height, true);
		return;
	}
	if (width <= 0 || height <= 0) {
		/* A zero-sized XDG configure lets the client choose its natural initial
		 * geometry instead of inheriting the private output's kiosk dimensions. */
		view->impl->configure(view, 0, 0, false);
		return;
	}

	int constrained_width = width > layout_box.width ? layout_box.width : width;
	int constrained_height = height > layout_box.height ? layout_box.height : height;
	struct wlr_box container_box = layout_box;
	struct cg_view *parent = view_transient_parent(view);
	if (parent) {
		int parent_width = 0, parent_height = 0;
		parent->impl->get_geometry(parent, &parent_width, &parent_height);
		if (parent_width > 0 && parent_height > 0) {
			container_box.x = parent->lx;
			container_box.y = parent->ly;
			container_box.width = parent_width;
			container_box.height = parent_height;
		}
	}
	if (view->user_positioned || view->client_positioned) {
		int max_x = layout_box.x + layout_box.width - constrained_width;
		int max_y = layout_box.y + layout_box.height - constrained_height;
		view->lx = view->lx < layout_box.x ? layout_box.x : (view->lx > max_x ? max_x : view->lx);
		view->ly = view->ly < layout_box.y ? layout_box.y : (view->ly > max_y ? max_y : view->ly);
		if (view->scene_tree)
			wlr_scene_node_set_position(&view->scene_tree->node, view->lx, view->ly);
	} else {
		view_center(view, &container_box, &layout_box, constrained_width, constrained_height);
	}
	view->impl->configure(view, constrained_width, constrained_height, false);
}

static void
load_overlay_position(struct cg_view *view)
{
    if (view->overlay_position_loaded)
        return;
    view->overlay_position_loaded = true;
    const char *path = getenv("AGENTSEAT_HUD_POSITION_FILE");
    if (!path || !*path)
        return;
    FILE *file = fopen(path, "r");
    if (!file)
        return;
    double x, y;
    int version;
    int count = fscanf(file, " { \"version\" : %d , \"x\" : %lf , \"y\" : %lf }", &version, &x, &y);
    fclose(file);
    if (count == 3 && version == 1 && isfinite(x) && isfinite(y) && x >= 0 && x <= 1 && y >= 0 && y <= 1) {
        view->overlay_position_x = x;
        view->overlay_position_y = y;
        view->overlay_position_valid = true;
        view->user_positioned = true;
    }
}

void
view_save_overlay_position(struct cg_view *view)
{
    if (!view_is_agentseat_overlay(view) || !view->user_positioned)
        return;
    struct wlr_box box;
    wlr_output_layout_get_box(view->server->output_layout, NULL, &box);
    int width = 0, height = 0;
    view->impl->get_geometry(view, &width, &height);
    int travel_x = box.width - width - 16, travel_y = box.height - height - 16;
    // A temporarily undersized output must not erase the last usable anchor.
    if (travel_x > 0)
        view->overlay_position_x = fmin(1, fmax(0, (double)(view->lx - box.x - 8) / travel_x));
    if (travel_y > 0)
        view->overlay_position_y = fmin(1, fmax(0, (double)(view->ly - box.y - 8) / travel_y));
    view->overlay_position_valid = true;
    const char *path = getenv("AGENTSEAT_HUD_POSITION_FILE");
    if (!path || !*path)
        return;
    size_t size = strlen(path) + 16;
    char *temporary = malloc(size);
    if (!temporary)
        return;
    snprintf(temporary, size, "%s.XXXXXX", path);
    int fd = mkstemp(temporary);
    if (fd >= 0) {
        char data[160];
        int length = snprintf(data, sizeof(data), "{\"version\":1,\"x\":%.9f,\"y\":%.9f}\n",
                              view->overlay_position_x, view->overlay_position_y);
        bool written = write(fd, data, length) == length && fsync(fd) == 0;
        close(fd);
        if (!written || rename(temporary, path) != 0) {
            fprintf(stderr, "AgentSeat: could not save HUD position\n");
            unlink(temporary);
        }
    }
    free(temporary);
}

void
view_position(struct cg_view *view)
{
#if CAGE_HAS_XWAYLAND
	if (view->type == CAGE_XWAYLAND_VIEW && !xwayland_view_should_manage(view)) {
		if (view->scene_tree)
			wlr_scene_node_set_position(&view->scene_tree->node, view->lx, view->ly);
		return;
	}
#endif
	struct wlr_box layout_box;
	wlr_output_layout_get_box(view->server->output_layout, NULL, &layout_box);
	if (view_is_agentseat_overlay(view)) {
		int width = 52, height = 52;
		view->impl->get_geometry(view, &width, &height);
		if (width <= 0)
			width = 52;
		if (height <= 0)
			height = 52;
        load_overlay_position(view);
        if (view->overlay_position_valid && view->server->seat->moving_view != view) {
            int travel_x = layout_box.width - width - 16;
            int travel_y = layout_box.height - height - 16;
            view->lx = layout_box.x + 8 + (int)lround(view->overlay_position_x * (travel_x > 0 ? travel_x : 0));
            view->ly = layout_box.y + 8 + (int)lround(view->overlay_position_y * (travel_y > 0 ? travel_y : 0));
        }
		bool collapsed = width <= 80 && height <= 80;
		bool had_geometry = view->overlay_last_width > 0 && view->overlay_last_height > 0;
		bool was_collapsed = view->overlay_last_width <= 80 && view->overlay_last_height <= 80;
		if (view->user_positioned && had_geometry && collapsed != was_collapsed) {
			if (!collapsed) {
				view->overlay_anchor_x = view->lx;
				view->overlay_anchor_y = view->ly;
				view->overlay_anchor_valid = true;
			} else if (view->overlay_anchor_valid) {
				view->lx = view->overlay_anchor_x;
				view->ly = view->overlay_anchor_y;
			}
		}
		view->overlay_last_width = width;
		view->overlay_last_height = height;
		if (!view->user_positioned) {
			view->lx = layout_box.x + layout_box.width - width - 16;
			view->ly = layout_box.y + layout_box.height - height - 16;
		} else {
			int min_x = layout_box.x + 8;
			int min_y = layout_box.y + 8;
			int max_x = layout_box.x + layout_box.width - width - 8;
			int max_y = layout_box.y + layout_box.height - height - 8;
			if (max_x < min_x)
				max_x = min_x;
			if (max_y < min_y)
				max_y = min_y;
			view->lx = view->lx < min_x ? min_x : (view->lx > max_x ? max_x : view->lx);
			view->ly = view->ly < min_y ? min_y : (view->ly > max_y ? max_y : view->ly);
		}
		if (view->scene_tree)
			wlr_scene_node_set_position(&view->scene_tree->node, view->lx, view->ly);
		return;
	}

	int width = 0, height = 0;
	view->impl->get_geometry(view, &width, &height);
	view_configure_requested(view, width, height);
}

void
view_position_all(struct cg_server *server)
{
	cg_scene_changed(server->seat);
	struct cg_view *view;
	wl_list_for_each (view, &server->views, link) {
		view_position(view);
	}
}

void
view_unmap(struct cg_view *view)
{
	struct cg_seat *seat = view->server->seat;
	struct cg_view *focused = seat_get_focus(seat);
	bool restore_focus = focused == view ||
		((!focused || view_is_agentseat_overlay(focused)) && seat->last_application_focus_id == view->agentseat_id);
	struct cg_view *restore = NULL;
	if (restore_focus) {
		struct cg_view *candidate;
		wl_list_for_each (candidate, &view->server->views, link) {
			if (candidate == view || !candidate->wlr_surface || view_is_agentseat_overlay(candidate))
				continue;
			if (!restore)
				restore = candidate;
			if (view_is_transient_for(view, candidate)) {
				restore = candidate;
				break;
			}
		}
	}
	wl_list_remove(&view->scene_commit.link);
	cg_scene_changed(view->server->seat);
	if (view->server->seat->moving_view == view)
		view->server->seat->moving_view = NULL;
	bool was_primary = view->server->primary_view == view;
	wl_list_remove(&view->link);
	if (was_primary) {
		view->server->primary_view = NULL;
		struct cg_view *candidate;
		wl_list_for_each (candidate, &view->server->views, link) {
			if (!view_is_agentseat_overlay(candidate) && view_is_primary(candidate)) {
				view->server->primary_view = candidate;
				view_position(candidate);
				break;
			}
		}
	}

	wl_list_remove(&view->request_activate.link);
	wl_list_remove(&view->request_close.link);
	wlr_foreign_toplevel_handle_v1_destroy(view->foreign_toplevel_handle);
	view->foreign_toplevel_handle = NULL;

	/* X11 clients may request geometry while hidden, before their next map.
	 * Clear the retained view pointer before releasing the scene so those
	 * requests cannot reposition an already destroyed tree. */
	struct wlr_scene_tree *scene_tree = view->scene_tree;
	view->scene_tree = NULL;
	wlr_scene_node_destroy(&scene_tree->node);

	view->wlr_surface->data = NULL;
	view->wlr_surface = NULL;
	if (restore_focus) {
		seat->last_application_focus_id = 0;
		if (restore)
			seat_set_focus(seat, restore);
		else
			wlr_seat_keyboard_notify_clear_focus(seat->seat);
	}
	cg_scene_changed(seat);
	host_window_sync_visibility(view->server);
}

void
handle_surface_request_activate(struct wl_listener *listener, void *data)
{
	struct cg_view *view = wl_container_of(listener, view, request_activate);

	wlr_scene_node_raise_to_top(&view->scene_tree->node);
	struct cg_view *previous = seat_get_focus(view->server->seat);
	if (!view_is_agentseat_overlay(view))
		seat_set_focus(view->server->seat, view);
	else if (previous)
		seat_set_focus(view->server->seat, previous);

	/* The small AgentSeat collaboration head stays above the wrapped app, but
	 * mapping it never takes keyboard focus from that app. */
	struct cg_view *candidate;
	wl_list_for_each (candidate, &view->server->views, link) {
		if (view_is_agentseat_overlay(candidate) && candidate->scene_tree)
			wlr_scene_node_raise_to_top(&candidate->scene_tree->node);
	}
}

void
handle_surface_request_close(struct wl_listener *listener, void *data)
{
	struct cg_view *view = wl_container_of(listener, view, request_close);
	view->impl->close(view);
}

static void
scene_surface_commit(struct wl_listener *listener, void *data)
{
	struct cg_view *view = wl_container_of(listener, view, scene_commit);
	cg_scene_changed(view->server->seat);
}

void
view_map(struct cg_view *view, struct wlr_surface *surface)
{
	struct cg_view *previous = seat_get_focus(view->server->seat);
	view->scene_tree = wlr_scene_subsurface_tree_create(&view->server->scene->tree, surface);
	if (!view->scene_tree)
		goto fail;
	view->scene_tree->node.data = view;

	view->wlr_surface = surface;
	surface->data = view;
	view->scene_commit.notify = scene_surface_commit;
	wl_signal_add(&surface->events.commit, &view->scene_commit);

	bool manage = true;
#if CAGE_HAS_XWAYLAND
	/* We shouldn't position override-redirect windows. They set
	   their own (x,y) coordinates in handle_wayland_surface_map. */
	manage = view->type != CAGE_XWAYLAND_VIEW || xwayland_view_should_manage(view);
#endif
	if (manage) {
		if (!view->server->primary_view && !view_is_agentseat_overlay(view) && view_is_primary(view)) {
			view->server->primary_view = view;
		}
		view_position(view);
	}
	if (!manage) {
		/* Unmanaged popups supply coordinates before their scene tree exists. */
		wlr_scene_node_set_position(&view->scene_tree->node, view->lx, view->ly);
	}

	wl_list_insert(&view->server->views, &view->link);

	view->foreign_toplevel_handle = wlr_foreign_toplevel_handle_v1_create(view->server->foreign_toplevel_manager);
	if (!view->foreign_toplevel_handle)
		goto fail;

	view->request_activate.notify = handle_surface_request_activate;
	wl_signal_add(&view->foreign_toplevel_handle->events.request_activate, &view->request_activate);
	view->request_close.notify = handle_surface_request_close;
	wl_signal_add(&view->foreign_toplevel_handle->events.request_close, &view->request_close);
	/* A desktop IME outlives this application. Its reusable candidate/menu
	 * surfaces must not keep an otherwise closed wrapper alive. */
	if (manage && !view_is_agentseat_overlay(view) && !view->application_lifetime_counted) {
		view->application_lifetime_counted = true;
		view->server->application_mapped_once = true;
		view->server->application_view_count++;
	}

	if (!view_is_agentseat_overlay(view) || previous == NULL)
		seat_set_focus(view->server->seat, view);
	else
		seat_set_focus(view->server->seat, previous);

	/* The collaboration head is rendered by the private micro-host. Keep it
	 * above the wrapped app without treating it as the app's primary window. */
	struct cg_view *candidate;
	wl_list_for_each (candidate, &view->server->views, link) {
		if (view_is_agentseat_overlay(candidate) && candidate->scene_tree)
			wlr_scene_node_raise_to_top(&candidate->scene_tree->node);
	}

	if (manage && !view_is_agentseat_overlay(view))
		application_lifetime_track(view);
	host_window_sync_visibility(view->server);

	const char *ready_path = getenv("AGENTSEAT_READY_FILE");
	if (manage && !view_is_agentseat_overlay(view) && ready_path && ready_path[0]) {
		int fd = open(ready_path, O_WRONLY | O_CREAT | O_TRUNC | O_CLOEXEC | O_NOFOLLOW, 0600);
		if (fd >= 0) {
			(void)write(fd, "mapped\n", 7);
			close(fd);
		}
	}
	return;

fail:
	wl_resource_post_no_memory(surface->resource);
}

void
view_destroy(struct cg_view *view)
{
	struct cg_server *server = view->server;

	if (view->wlr_surface != NULL) {
		view_unmap(view);
	}
	if (server->primary_view == view) {
		/* An XDG toplevel can disappear after its initial configure but before
		 * mapping. Do not leave a dangling primary_view in that case. */
		server->primary_view = NULL;
	}
	if (view->application_lifetime_counted) {
		assert(server->application_view_count > 0);
		view->application_lifetime_counted = false;
		server->application_view_count--;
		if (server->primary_client_exited && !application_lifetime_alive(server)) {
			server_terminate(server);
		}
	}

	view->impl->destroy(view);

	/* Unmapping restores application focus. Destroying a hidden or unrelated
	 * view must not steal focus, including from a newly opened save dialog. */
}

void
view_init(struct cg_view *view, struct cg_server *server, enum cg_view_type type, const struct cg_view_impl *impl)
{
	view->server = server;
	view->agentseat_id = cg_scene_allocate_id(server->seat);
	view->type = type;
	view->impl = impl;
}

struct cg_view *
view_from_wlr_surface(struct wlr_surface *surface)
{
	assert(surface);
	return surface->data;
}
