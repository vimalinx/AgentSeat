#ifndef CG_SEAT_H
#define CG_SEAT_H

#include <wayland-server-core.h>
#include <stdint.h>
#include <wlr/types/wlr_cursor.h>
#include <wlr/types/wlr_data_device.h>
#include <wlr/types/wlr_input_device.h>
#include <wlr/types/wlr_seat.h>
#include <wlr/types/wlr_xcursor_manager.h>

#include "server.h"
#include "view.h"

#define DEFAULT_XCURSOR "left_ptr"
#define XCURSOR_SIZE 20

#define CG_KEY_COUNT 768
#define CG_BUTTON_COUNT 1024

struct cg_seat {
	struct wlr_seat *seat;
	struct cg_server *server;
	struct wl_listener destroy;

	struct wl_list keyboards;
	struct wl_list keyboard_groups;
	struct xkb_keymap *parent_keymap;
	bool parent_repeat_known;
	int parent_repeat_rate, parent_repeat_delay;
	struct wl_list pointers;
	struct wl_list touch;
	struct wl_listener new_input;

	struct wlr_cursor *cursor;
	struct wlr_xcursor_manager *xcursor_manager;
	struct wl_listener cursor_motion_relative;
	struct wl_listener cursor_motion_absolute;
	struct wl_listener cursor_button;
	struct wl_listener cursor_axis;
	struct wl_listener cursor_frame;

	int32_t touch_id;
	double touch_lx;
	double touch_ly;
	struct wl_listener touch_down;
	struct wl_listener touch_up;
	struct wl_listener touch_motion;
	struct wl_listener touch_frame;

	struct wl_list drag_icons;
	struct wl_listener request_start_drag;
	struct wl_listener start_drag;

	struct wl_listener request_set_cursor;
	struct wl_listener request_set_selection;
	struct wl_listener request_set_primary_selection;

	/* The compositor is the authority for the final virtual-input gate. */
	struct cg_scene *scene;
	struct cg_clipboard *clipboard;
	struct wl_global *control_global;
	struct wl_resource *control_resource;
	struct wl_event_source *control_timer;
	struct wl_event_source *hold_timer;
	unsigned hold_timeout_ms;
	uint64_t key_deadlines[CG_KEY_COUNT];
	uint64_t button_deadlines[CG_BUTTON_COUNT];
	uint32_t control_epoch;
	bool control_allowed;
	uint64_t human_until_msec;
	unsigned human_grace_ms;
	bool forwarded_keys[CG_KEY_COUNT];
	bool forwarded_buttons[CG_BUTTON_COUNT];
	/* Ownership of the first effective press which created the pointer grab. */
	bool pointer_grab_virtual;

	/* AgentSeat's optional human-priority activity channel. */
	int activity_fd;
	uint64_t last_activity_msec;
	char activity_socket_path[108];

	/* Interactive move is accepted only for AgentSeat's in-window overlay. */
	struct cg_view *moving_view;
	uint32_t last_application_focus_id;
	double move_offset_x;
	double move_offset_y;
	double pointer_press_x;
	double pointer_press_y;
};

struct cg_keyboard_group {
	struct wlr_keyboard_group *wlr_group;
	struct wlr_keyboard *source;
	struct wl_listener source_destroy;
	struct cg_seat *seat;
	struct wl_listener key;
	struct wl_listener modifiers;
	struct wl_list link; // cg_seat::keyboard_groups
	bool is_virtual;
	struct wl_client *client;
	bool held_keys[CG_KEY_COUNT];
};

struct cg_pointer {
	struct wl_list link; // seat::pointers
	struct cg_seat *seat;
	struct wlr_pointer *pointer;
	bool is_virtual;
	struct wl_client *client;
	bool held_buttons[CG_BUTTON_COUNT];
	bool has_host_position;
	double host_x, host_y;

	struct wl_listener destroy;
};

struct cg_touch {
	struct wl_list link; // seat::touch
	struct cg_seat *seat;
	struct wlr_touch *touch;

	struct wl_listener destroy;
};

struct cg_drag_icon {
	struct wl_list link; // seat::drag_icons
	struct cg_seat *seat;
	struct wlr_drag_icon *wlr_drag_icon;
	struct wlr_scene_tree *scene_tree;

	/* The drag icon has a position in layout coordinates. */
	double lx, ly;

	struct wl_listener destroy;
};

struct cg_seat *seat_create(struct cg_server *server, struct wlr_backend *backend);
void seat_destroy(struct cg_seat *seat);
void seat_set_parent_keymap(struct cg_seat *, struct xkb_keymap *);
void seat_set_parent_repeat(struct cg_seat *, int rate, int delay);
struct cg_view *seat_get_focus(struct cg_seat *seat);
void seat_close_focused_application(struct cg_seat *seat);
void seat_set_focus(struct cg_seat *seat, struct cg_view *view);
void seat_center_cursor(struct cg_seat *seat);

#endif
