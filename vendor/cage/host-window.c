/* AgentSeat owns the parent toplevel so its close request reaches the focused
 * application window instead of destroying the application's display server. */
#define _POSIX_C_SOURCE 200809L
#include <stdlib.h>
#include <string.h>
#include <wayland-client.h>
#include <wlr/backend/wayland.h>
#include <wlr/interfaces/wlr_output.h>
#include <wlr/types/wlr_output.h>
#include <wlr/util/log.h>

#include "host-window.h"
#include "host-clipboard.h"
#include "host-text-input.h"
#include "host-keyboard.h"
#include "output.h"
#include "server.h"
#include "seat.h"
#include "view.h"
#include "xdg-shell-client-protocol.h"

struct cg_host_window {
	struct cg_server *server;
	struct wl_registry *registry;
	struct wl_compositor *compositor;
	struct xdg_wm_base *shell;
	struct wl_surface *surface;
	struct cg_host_clipboard *clipboard;
	struct cg_host_text_input *text_input;
	struct cg_host_keyboard *keyboard;
	struct xdg_surface *xdg_surface;
	struct xdg_toplevel *toplevel;
	struct wlr_output *output;
	struct wl_listener output_destroy;
	struct wl_listener backend_destroy;
	int width, height;
	bool configured, hidden;
	char *title;
};

static void
shell_ping(void *data, struct xdg_wm_base *shell, uint32_t serial)
{
	xdg_wm_base_pong(shell, serial);
}

static const struct xdg_wm_base_listener shell_listener = {.ping = shell_ping};

static void
registry_global(void *data, struct wl_registry *registry, uint32_t name, const char *interface,
		uint32_t version)
{
	struct cg_host_window *host = data;
	if (strcmp(interface, wl_compositor_interface.name) == 0 && !host->compositor) {
		host->compositor = wl_registry_bind(registry, name, &wl_compositor_interface, version < 4 ? version : 4);
	} else if (strcmp(interface, xdg_wm_base_interface.name) == 0 && !host->shell) {
		host->shell = wl_registry_bind(registry, name, &xdg_wm_base_interface, 1);
		xdg_wm_base_add_listener(host->shell, &shell_listener, host);
	}
}

static void
registry_remove(void *data, struct wl_registry *registry, uint32_t name)
{
}

static const struct wl_registry_listener registry_listener = {
	.global = registry_global, .global_remove = registry_remove,
};

static void
resize_output(struct cg_host_window *host)
{
	if (!host->output)
		return;
	/* Reuse the output's normal configure path, including fractional scaling. */
	struct wlr_output_state state;
	wlr_output_state_init(&state);
	wlr_output_state_set_custom_mode(&state, host->width, host->height, 0);
	wlr_output_send_request_state(host->output, &state);
	wlr_output_state_finish(&state);
}

static void
surface_configure(void *data, struct xdg_surface *surface, uint32_t serial)
{
	struct cg_host_window *host = data;
	xdg_surface_ack_configure(surface, serial);
	bool remapping = !host->configured;
	host->configured = true;
	resize_output(host);
	if (remapping && host->output && !host->hidden)
		output_repaint_all(host->server);
}

static const struct xdg_surface_listener surface_listener = {.configure = surface_configure};

static void
toplevel_configure(void *data, struct xdg_toplevel *toplevel, int32_t width, int32_t height,
		   struct wl_array *states)
{
	struct cg_host_window *host = data;
	if (width > 0)
		host->width = width;
	if (height > 0)
		host->height = height;
}

static void
toplevel_close(void *data, struct xdg_toplevel *toplevel)
{
	struct cg_host_window *host = data;
	seat_close_focused_application(host->server->seat);
}

static const struct xdg_toplevel_listener toplevel_listener = {
	.configure = toplevel_configure, .close = toplevel_close,
};

static void
output_destroyed(struct wl_listener *listener, void *data)
{
	struct cg_host_window *host = wl_container_of(listener, host, output_destroy);
	wl_list_remove(&host->output_destroy.link);
	host->output = NULL;
}

static void
backend_destroyed(struct wl_listener *listener, void *data)
{
	struct cg_host_window *host = wl_container_of(listener, host, backend_destroy);
	struct cg_server *server = host->server;
	/* Backend loss destroys outputs before it disconnects the remote display.
	 * Drop our remaining proxies in this interval, not after display teardown. */
	output_finish_host_viewporter(server);
	host_window_finish(server);
	server->parent_backend = NULL;
}

bool
host_window_create(struct cg_server *server)
{
	if (!server->parent_backend)
		return true;
	struct cg_host_window *host = calloc(1, sizeof(*host));
	if (!host)
		return false;
	server->host_window = host;
	host->server = server;
	host->width = 1280;
	host->height = 720;
	host->backend_destroy.notify = backend_destroyed;
	wl_signal_add(&server->parent_backend->events.destroy, &host->backend_destroy);
	struct wl_display *display = wlr_wl_backend_get_remote_display(server->parent_backend);
	host->registry = wl_display_get_registry(display);
	if (!host->registry)
		return false;
	wl_registry_add_listener(host->registry, &registry_listener, host);
	if (wl_display_roundtrip(display) < 0 || !host->compositor || !host->shell)
		return false;
	host->surface = wl_compositor_create_surface(host->compositor);
	if (!host->surface)
		return false;
	host->clipboard = host_clipboard_create(server, display, host->surface);
	if (!host->clipboard)
		return false;
	host->keyboard = host_keyboard_create(server->seat, display);
	if (!host->keyboard)
		return false;
	host->text_input = host_text_input_create(server, display, host->surface);
	if (!host->text_input)
		return false;
	host->xdg_surface = xdg_wm_base_get_xdg_surface(host->shell, host->surface);
	if (!host->xdg_surface)
		return false;
	xdg_surface_add_listener(host->xdg_surface, &surface_listener, host);
	host->toplevel = xdg_surface_get_toplevel(host->xdg_surface);
	if (!host->toplevel)
		return false;
	xdg_toplevel_add_listener(host->toplevel, &toplevel_listener, host);
	/* Retain the existing host-window identity and placement rules. */
	xdg_toplevel_set_app_id(host->toplevel, "wlroots");
	xdg_toplevel_set_title(host->toplevel, "AgentSeat");
	wl_surface_commit(host->surface);
	if (wl_display_roundtrip(display) < 0 || !host->configured)
		return false;
	host->output = wlr_wl_output_create_from_surface(server->parent_backend, host->surface);
	if (!host->output)
		return false;
	host->output_destroy.notify = output_destroyed;
	wl_signal_add(&host->output->events.destroy, &host->output_destroy);
	resize_output(host);
	wl_display_flush(display);
	return true;
}

bool
host_window_set_title(struct cg_server *server, struct wlr_output *output, const char *title)
{
	struct cg_host_window *host = server->host_window;
	if (!host || wlr_wl_output_get_surface(output) != host->surface)
		return false;
	free(host->title);
	host->title = strdup(title ? title : "AgentSeat");
	if (host->toplevel)
		xdg_toplevel_set_title(host->toplevel, title ? title : "AgentSeat");
	return true;
}

bool
host_window_can_render(struct cg_server *server)
{
    struct cg_host_window *host = server->host_window;
    return !host || (!host->hidden && host->configured);
}

void
host_window_sync_visibility(struct cg_server *server)
{
    struct cg_host_window *host = server->host_window;
    if (!host || !host->output || !server->application_mapped_once)
        return;
    bool visible = false;
    struct cg_view *view;
    wl_list_for_each(view, &server->views, link) {
        if (view->wlr_surface && view->application_lifetime_counted && !view_is_agentseat_overlay(view)) {
            visible = true;
            break;
        }
    }
    if (visible == !host->hidden)
        return;
    wlr_log(WLR_INFO, "Application desktop window %s", visible ? "restoring" : "hidden in background");
    host->hidden = !visible;
    host->configured = false;
    if (!visible) {
        // Unmap the desktop surface, while retaining the application's display.
        wl_surface_attach(host->surface, NULL, 0, 0);
        wl_surface_commit(host->surface);
        // Drop only the desktop role. Keeping an unmapped role here leaves some
        // parents waiting indefinitely instead of issuing a fresh configure.
        xdg_toplevel_destroy(host->toplevel);
        host->toplevel = NULL;
        xdg_surface_destroy(host->xdg_surface);
        host->xdg_surface = NULL;
    } else {
        host->xdg_surface = xdg_wm_base_get_xdg_surface(host->shell, host->surface);
        xdg_surface_add_listener(host->xdg_surface, &surface_listener, host);
        host->toplevel = xdg_surface_get_toplevel(host->xdg_surface);
        xdg_toplevel_add_listener(host->toplevel, &toplevel_listener, host);
        // A new desktop role needs an empty commit and a fresh configure.
        xdg_toplevel_set_app_id(host->toplevel, "wlroots");
        xdg_toplevel_set_title(host->toplevel, host->title ? host->title : "AgentSeat");
        wl_surface_commit(host->surface);
    }
    wl_display_flush(wlr_wl_backend_get_remote_display(server->parent_backend));
}

void
host_window_finish(struct cg_server *server)
{
	struct cg_host_window *host = server->host_window;
	if (!host)
		return;
	wl_list_remove(&host->backend_destroy.link);
	host_keyboard_destroy(host->keyboard);
	host_text_input_destroy(host->text_input);
	host_clipboard_destroy(host->clipboard);
	/* The backend references our surface. Release it before its role and surface,
	 * and release every client proxy before the backend disconnects the display. */
	if (host->output)
		wlr_output_destroy(host->output);
	if (host->toplevel)
		xdg_toplevel_destroy(host->toplevel);
	if (host->xdg_surface)
		xdg_surface_destroy(host->xdg_surface);
	if (host->surface)
		wl_surface_destroy(host->surface);
	if (host->shell)
		xdg_wm_base_destroy(host->shell);
	if (host->compositor)
		wl_compositor_destroy(host->compositor);
	if (host->registry)
		wl_registry_destroy(host->registry);
	free(host->title);
	free(host);
	server->host_window = NULL;
}
