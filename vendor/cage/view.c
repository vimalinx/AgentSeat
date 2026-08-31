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
#include <stdbool.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include <wayland-server-core.h>
#include <wlr/types/wlr_foreign_toplevel_management_v1.h>
#include <wlr/types/wlr_output.h>
#include <wlr/types/wlr_scene.h>

#include "output.h"
#include "seat.h"
#include "server.h"
#include "view.h"
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
	view_center(view, &container_box, &layout_box, constrained_width, constrained_height);
	view->impl->configure(view, constrained_width, constrained_height, false);
}

void
view_position(struct cg_view *view)
{
	struct wlr_box layout_box;
	wlr_output_layout_get_box(view->server->output_layout, NULL, &layout_box);
	if (view_is_agentseat_overlay(view)) {
		int width = 52, height = 52;
		view->impl->get_geometry(view, &width, &height);
		if (width <= 0)
			width = 52;
		if (height <= 0)
			height = 52;
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
	struct cg_view *view;
	wl_list_for_each (view, &server->views, link) {
		view_position(view);
	}
}

void
view_unmap(struct cg_view *view)
{
	if (view->server->seat->moving_view == view)
		view->server->seat->moving_view = NULL;
	wl_list_remove(&view->link);

	wl_list_remove(&view->request_activate.link);
	wl_list_remove(&view->request_close.link);
	wlr_foreign_toplevel_handle_v1_destroy(view->foreign_toplevel_handle);
	view->foreign_toplevel_handle = NULL;

	wlr_scene_node_destroy(&view->scene_tree->node);

	view->wlr_surface->data = NULL;
	view->wlr_surface = NULL;
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

#if CAGE_HAS_XWAYLAND
	/* We shouldn't position override-redirect windows. They set
	   their own (x,y) coordinates in handle_wayland_surface_map. */
	if (view->type != CAGE_XWAYLAND_VIEW || xwayland_view_should_manage(view))
#endif
	{
		view_position(view);
	}

	wl_list_insert(&view->server->views, &view->link);

	view->foreign_toplevel_handle = wlr_foreign_toplevel_handle_v1_create(view->server->foreign_toplevel_manager);
	if (!view->foreign_toplevel_handle)
		goto fail;

	view->request_activate.notify = handle_surface_request_activate;
	wl_signal_add(&view->foreign_toplevel_handle->events.request_activate, &view->request_activate);
	view->request_close.notify = handle_surface_request_close;
	wl_signal_add(&view->foreign_toplevel_handle->events.request_close, &view->request_close);
	if (!view_is_agentseat_overlay(view) && !view->application_lifetime_counted) {
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

	const char *ready_path = getenv("AGENTSEAT_READY_FILE");
	if (!view_is_agentseat_overlay(view) && ready_path && ready_path[0]) {
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
	if (view->application_lifetime_counted) {
		assert(server->application_view_count > 0);
		view->application_lifetime_counted = false;
		server->application_view_count--;
		if (server->primary_client_exited && !view_has_application_views(server)) {
			server_terminate(server);
		}
	}

	view->impl->destroy(view);

	/* If there is a previous view in the list, focus that. */
	bool empty = wl_list_empty(&server->views);
	if (!empty) {
		struct cg_view *prev = wl_container_of(server->views.next, prev, link);
		seat_set_focus(server->seat, prev);
	}
}

void
view_init(struct cg_view *view, struct cg_server *server, enum cg_view_type type, const struct cg_view_impl *impl)
{
	view->server = server;
	view->type = type;
	view->impl = impl;
}

struct cg_view *
view_from_wlr_surface(struct wlr_surface *surface)
{
	assert(surface);
	return surface->data;
}
