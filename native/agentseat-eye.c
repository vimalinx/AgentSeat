#define _GNU_SOURCE

#include <cairo.h>
#include <errno.h>
#include <gtk/gtk.h>
#include <gtk4-layer-shell/gtk4-layer-shell.h>
#include <json-c/json.h>
#include <math.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <sys/time.h>
#include <sys/un.h>
#include <unistd.h>

#include "agentseat-i18n.h"

#define RPC_LIMIT (1024U * 1024U)
#define COLLAPSED_SIZE 52
#define HEAD_DRAW_SIZE 46
#define PANEL_WIDTH 320
#define PANEL_HEIGHT 232
#define HUD_CURSOR_SIZE 20

struct eye_app {
    GtkApplication* application;
    GtkWindow* window;
    GtkWidget* stack;
    GtkWidget* eye_root;
    GtkWidget* panel_root;
    GtkWidget* head_area;
    GtkWidget* activity_label;
    GtkWidget* detail_label;
    GtkWidget* state_label;
    GtkWidget* messages_box;
    GtkWidget* messages_scroll;
    GtkWidget* entry;
    GdkCursor* arrow_cursor;
    bool entry_cursor_idle_pending;
    bool enforcing_entry_cursor;
    bool expanded;
    bool demo;
    bool embedded;
    bool animations;
    bool drag_moved;
    bool move_started;
    double drag_origin_x;
    double drag_origin_y;
    int margin_top;
    int margin_right;
    int drag_start_top;
    int drag_start_right;
    double gaze_x;
    double gaze_y;
    double target_x;
    double target_y;
    gint64 animation_start;
    uint64_t rpc_id;
    uint64_t last_message_id;
    char socket_path[108];
};

static struct eye_app app_state = {
    .margin_top = 24,
    .margin_right = 24,
};

static bool write_all(int fd, const char* data, size_t length) {
    size_t written = 0;
    while (written < length) {
        ssize_t count = write(fd, data + written, length - written);
        if (count < 0 && errno == EINTR)
            continue;
        if (count <= 0)
            return false;
        written += (size_t)count;
    }
    return true;
}

static json_object* rpc_call(const char* method, json_object* params) {
    if (app_state.demo)
        return NULL;
    int fd = socket(AF_UNIX, SOCK_STREAM | SOCK_CLOEXEC, 0);
    if (fd < 0)
        return NULL;
    struct timeval timeout = {.tv_sec = 0, .tv_usec = 180000};
    setsockopt(fd, SOL_SOCKET, SO_RCVTIMEO, &timeout, sizeof(timeout));
    setsockopt(fd, SOL_SOCKET, SO_SNDTIMEO, &timeout, sizeof(timeout));
    struct sockaddr_un address = {.sun_family = AF_UNIX};
    snprintf(address.sun_path, sizeof(address.sun_path), "%s", app_state.socket_path);
    if (connect(fd, (struct sockaddr*)&address, sizeof(address)) != 0) {
        close(fd);
        return NULL;
    }

    json_object* request = json_object_new_object();
    json_object_object_add(request, "jsonrpc", json_object_new_string("2.0"));
    json_object_object_add(request, "id", json_object_new_int64((int64_t)++app_state.rpc_id));
    json_object_object_add(request, "method", json_object_new_string(method));
    json_object_object_add(request, "params", params ? json_object_get(params) : json_object_new_object());
    const char* encoded = json_object_to_json_string_ext(request, JSON_C_TO_STRING_PLAIN);
    bool sent = write_all(fd, encoded, strlen(encoded)) && write_all(fd, "\n", 1);
    json_object_put(request);
    if (!sent) {
        close(fd);
        return NULL;
    }

    char* response = calloc(RPC_LIMIT + 1, 1);
    if (!response) {
        close(fd);
        return NULL;
    }
    size_t length = 0;
    while (length < RPC_LIMIT) {
        ssize_t count = read(fd, response + length, RPC_LIMIT - length);
        if (count < 0 && errno == EINTR)
            continue;
        if (count <= 0)
            break;
        length += (size_t)count;
        if (memchr(response, '\n', length))
            break;
    }
    close(fd);
    char* newline = memchr(response, '\n', length);
    if (newline)
        *newline = '\0';
    json_object* envelope = length ? json_tokener_parse(response) : NULL;
    free(response);
    if (!envelope)
        return NULL;
    json_object* result = NULL;
    if (!json_object_object_get_ex(envelope, "result", &result)) {
        json_object_put(envelope);
        return NULL;
    }
    json_object_get(result);
    json_object_put(envelope);
    return result;
}

static void set_accessible_label(GtkWidget* widget, const char* text) {
    gtk_accessible_update_property(GTK_ACCESSIBLE(widget), GTK_ACCESSIBLE_PROPERTY_LABEL, text, -1);
}

static void draw_eye(GtkDrawingArea* area, cairo_t* cr, int width, int height, gpointer data) {
    (void)area;
    (void)data;
    const double size = MIN(width, height);
    const double cx = width / 2.0;
    const double cy = height / 2.0;
    const double outer = size * 0.475;

    /* The open arc is the Agent's bounded seat, not a second desktop shell. */
    cairo_arc(cr, cx, cy, outer - 0.7, 0.47, (2 * G_PI) - 1.00);
    cairo_set_line_width(cr, 1.35);
    cairo_set_source_rgba(cr, 0.663, 0.863, 0.969, 0.98);
    cairo_stroke(cr);

    double elapsed = (g_get_monotonic_time() - app_state.animation_start) / 1000000.0;
    double phase = fmod(elapsed, 4.8);
    double blink = 1.0;
    if (app_state.animations && phase > 4.45) {
        double t = (phase - 4.45) / 0.35;
        blink = 0.10 + 0.90 * fabs(cos(t * G_PI));
    }
    /* The reference is a tiny head: pale-blue shell, black face, two white
     * eyes. The white shapes move and blink; the black face stays a face. */
    const double face_radius = outer * 0.955;
    const double face_x = cx + sin(elapsed * 0.82) * outer * 0.018;
    const double face_y = cy + cos(elapsed * 0.61) * outer * 0.014;
    cairo_save(cr);
    cairo_translate(cr, face_x, face_y);
    cairo_rotate(cr, sin(elapsed * 0.56) * 0.024);
    cairo_arc(cr, 0, 0, face_radius, 0, 2 * G_PI);
    cairo_set_source_rgb(cr, 0.015, 0.02, 0.025);
    cairo_fill(cr);

    const double eye_shift_x = app_state.gaze_x * face_radius * 0.045;
    const double eye_shift_y = app_state.gaze_y * face_radius * 0.038;
    cairo_save(cr);
    cairo_translate(cr, -face_radius * 0.19 + eye_shift_x, -face_radius * 0.22 + eye_shift_y);
    cairo_rotate(cr, -0.34);
    cairo_scale(cr, 0.43, 1.0 * blink);
    cairo_arc(cr, 0, 0, face_radius * 0.215, 0, 2 * G_PI);
    cairo_set_source_rgba(cr, 1, 1, 1, 0.96);
    cairo_fill(cr);
    cairo_restore(cr);

    cairo_save(cr);
    cairo_translate(cr, face_radius * 0.25 + eye_shift_x, -face_radius * 0.27 + eye_shift_y);
    cairo_rotate(cr, -0.22);
    cairo_scale(cr, 0.43, 1.0 * blink);
    cairo_arc(cr, 0, 0, face_radius * 0.215, 0, 2 * G_PI);
    cairo_set_source_rgba(cr, 1, 1, 1, 0.96);
    cairo_fill(cr);
    cairo_restore(cr);
    cairo_restore(cr);
}

static gboolean animate_eye(gpointer data) {
    (void)data;
    if (!app_state.animations)
        return G_SOURCE_CONTINUE;
    double elapsed = (g_get_monotonic_time() - app_state.animation_start) / 1000000.0;
    double idle_x = sin(elapsed * 0.62) * 0.62;
    double idle_y = sin(elapsed * 0.41 + 1.3) * 0.46;
    app_state.target_x = idle_x;
    app_state.target_y = idle_y;
    app_state.gaze_x += (app_state.target_x - app_state.gaze_x) * 0.10;
    app_state.gaze_y += (app_state.target_y - app_state.gaze_y) * 0.10;
    if (app_state.head_area)
        gtk_widget_queue_draw(app_state.head_area);
    return G_SOURCE_CONTINUE;
}

static GdkCursor* create_native_arrow_cursor(void) {
    cairo_surface_t* surface = cairo_image_surface_create(CAIRO_FORMAT_ARGB32, HUD_CURSOR_SIZE, HUD_CURSOR_SIZE);
    if (cairo_surface_status(surface) != CAIRO_STATUS_SUCCESS) {
        cairo_surface_destroy(surface);
        return NULL;
    }
    cairo_t* cr = cairo_create(surface);
    cairo_set_operator(cr, CAIRO_OPERATOR_CLEAR);
    cairo_paint(cr);
    cairo_set_operator(cr, CAIRO_OPERATOR_OVER);
    cairo_move_to(cr, 1.5, 1.0);
    cairo_line_to(cr, 1.5, 15.0);
    cairo_line_to(cr, 5.4, 11.2);
    cairo_line_to(cr, 8.8, 18.7);
    cairo_line_to(cr, 12.0, 17.2);
    cairo_line_to(cr, 8.5, 10.0);
    cairo_line_to(cr, 14.0, 10.0);
    cairo_close_path(cr);
    cairo_set_line_join(cr, CAIRO_LINE_JOIN_ROUND);
    cairo_set_line_width(cr, 2.0);
    cairo_set_source_rgba(cr, 1.0, 1.0, 1.0, 0.96);
    cairo_stroke_preserve(cr);
    cairo_set_source_rgba(cr, 0.04, 0.05, 0.06, 1.0);
    cairo_fill(cr);
    cairo_destroy(cr);
    cairo_surface_flush(surface);

    int stride = cairo_image_surface_get_stride(surface);
    GBytes* bytes = g_bytes_new(cairo_image_surface_get_data(surface), (gsize)stride * HUD_CURSOR_SIZE);
    GdkTexture* texture = gdk_memory_texture_new(
        HUD_CURSOR_SIZE, HUD_CURSOR_SIZE, GDK_MEMORY_DEFAULT, bytes, (gsize)stride);
    GdkCursor* cursor = gdk_cursor_new_from_texture(texture, 1, 1, NULL);
    g_object_unref(texture);
    g_bytes_unref(bytes);
    cairo_surface_destroy(surface);
    return cursor;
}

static gboolean apply_entry_arrow_idle(gpointer data) {
    (void)data;
    app_state.entry_cursor_idle_pending = false;
    if (app_state.entry && app_state.arrow_cursor &&
        gtk_widget_get_cursor(app_state.entry) != app_state.arrow_cursor) {
        app_state.enforcing_entry_cursor = true;
        gtk_widget_set_cursor(app_state.entry, app_state.arrow_cursor);
        app_state.enforcing_entry_cursor = false;
    }
    return G_SOURCE_REMOVE;
}

static void queue_entry_arrow(void) {
    if (!app_state.entry_cursor_idle_pending) {
        app_state.entry_cursor_idle_pending = true;
        g_idle_add(apply_entry_arrow_idle, NULL);
    }
}

static void entry_cursor_enter(GtkEventControllerMotion* controller, double x, double y, gpointer data) {
    (void)controller;
    (void)x;
    (void)y;
    (void)data;
    /* GtkText selects a 32 px themed I-beam after capture-phase controllers
     * run. Reapply the HUD's native 20 px arrow at the end of the same event
     * turn so the buffer size and its 1,1 hotspot stay consistent. */
    queue_entry_arrow();
}

static void entry_cursor_changed(GObject* object, GParamSpec* specification, gpointer data) {
    (void)object;
    (void)specification;
    (void)data;
    if (!app_state.enforcing_entry_cursor)
        queue_entry_arrow();
}

static void rounded_rectangle(cairo_t* cr, double x, double y, double width, double height, double radius) {
    const double right = x + width;
    const double bottom = y + height;
    cairo_new_sub_path(cr);
    cairo_arc(cr, right - radius, y + radius, radius, -G_PI / 2.0, 0);
    cairo_arc(cr, right - radius, bottom - radius, radius, 0, G_PI / 2.0);
    cairo_arc(cr, x + radius, bottom - radius, radius, G_PI / 2.0, G_PI);
    cairo_arc(cr, x + radius, y + radius, radius, G_PI, 3.0 * G_PI / 2.0);
    cairo_close_path(cr);
}

static void draw_acrylic_veil(GtkDrawingArea* area, cairo_t* cr, int width, int height, gpointer data) {
    (void)area;
    (void)data;

    /* Keep the task context in one compact floating material. The composer and
     * head remain outside this sheet so the HUD reads as ambient collaboration,
     * not as a modal card covering the application. */
    const double x = 10.0;
    const double y = 6.0;
    const double sheet_width = width - 16.0;
    const double sheet_height = height - 70.0;
    rounded_rectangle(cr, x, y + 4.0, sheet_width, sheet_height, 27.0);
    cairo_set_source_rgba(cr, 0.0, 0.0, 0.0, 0.18);
    cairo_fill(cr);
    rounded_rectangle(cr, x, y, sheet_width, sheet_height, 27.0);
    cairo_pattern_t* glass = cairo_pattern_create_linear(0, y, 0, y + sheet_height);
    cairo_pattern_add_color_stop_rgba(glass, 0.0, 0.055, 0.070, 0.088, 0.76);
    cairo_pattern_add_color_stop_rgba(glass, 1.0, 0.025, 0.036, 0.050, 0.86);
    cairo_set_source(cr, glass);
    cairo_fill_preserve(cr);
    cairo_pattern_destroy(glass);
    cairo_set_line_width(cr, 1.0);
    cairo_set_source_rgba(cr, 0.82, 0.91, 0.97, 0.14);
    cairo_stroke(cr);
}

static void apply_position(void) {
    if (app_state.demo || app_state.embedded)
        return;
    app_state.margin_top = CLAMP(app_state.margin_top, 8, 1800);
    app_state.margin_right = CLAMP(app_state.margin_right, 8, 3200);
    gtk_layer_set_margin(app_state.window, GTK_LAYER_SHELL_EDGE_TOP, app_state.margin_top);
    gtk_layer_set_margin(app_state.window, GTK_LAYER_SHELL_EDGE_RIGHT, app_state.margin_right);
}

static void apply_surface_input_region(void) {
    GdkSurface* surface = gtk_native_get_surface(GTK_NATIVE(app_state.window));
    if (!surface)
        return;
    cairo_rectangle_int_t rectangle = app_state.expanded
        ? (cairo_rectangle_int_t){0, 0, PANEL_WIDTH, PANEL_HEIGHT}
        : (cairo_rectangle_int_t){
              PANEL_WIDTH - COLLAPSED_SIZE,
              PANEL_HEIGHT - COLLAPSED_SIZE,
              COLLAPSED_SIZE,
              COLLAPSED_SIZE};
    cairo_region_t* region = cairo_region_create_rectangle(&rectangle);
    gdk_surface_set_input_region(surface, region);
    cairo_region_destroy(region);
}

static gboolean apply_surface_input_region_idle(gpointer data) {
    (void)data;
    apply_surface_input_region();
    return G_SOURCE_REMOVE;
}

static gboolean finish_surface_transition(gpointer data) {
    bool expanded = GPOINTER_TO_INT(data);
    if (app_state.expanded != expanded)
        return G_SOURCE_REMOVE;

    GtkWidget* current = expanded ? app_state.panel_root : app_state.eye_root;
    GtkWidget* previous = expanded ? app_state.eye_root : app_state.panel_root;
    gtk_widget_set_visible(previous, false);
    gtk_widget_set_visible(current, true);
    apply_surface_input_region();
    return G_SOURCE_REMOVE;
}

static void transition_running_changed(GObject* object, GParamSpec* specification, gpointer data) {
    (void)specification;
    (void)data;
    if (!gtk_stack_get_transition_running(GTK_STACK(object)))
        finish_surface_transition(GINT_TO_POINTER(app_state.expanded));
}

static void set_expanded(bool expanded) {
    app_state.expanded = expanded;
    if (app_state.head_area) {
        set_accessible_label(
            app_state.head_area,
            expanded ? AS_TR("Collapse AgentSeat AI") : AS_TR("Expand the AgentSeat AI head"));
    }
    GtkWidget* current = expanded ? app_state.panel_root : app_state.eye_root;
    GtkWidget* previous = expanded ? app_state.eye_root : app_state.panel_root;
    gtk_widget_set_visible(current, true);
    gtk_widget_set_visible(previous, true);

    /* The Wayland surface stays fixed so expanding never races an asynchronous
     * xdg-toplevel resize. In collapsed mode its compositor input region is
     * only the 52 px head, so the transparent remainder clicks through to the
     * wrapped application. */
    gtk_widget_set_size_request(app_state.stack, PANEL_WIDTH, PANEL_HEIGHT);
    gtk_window_set_default_size(app_state.window, PANEL_WIDTH, PANEL_HEIGHT);
    apply_surface_input_region();
    gtk_stack_set_visible_child_name(GTK_STACK(app_state.stack), expanded ? "panel" : "eye");
    if (!app_state.animations || !gtk_widget_get_mapped(GTK_WIDGET(app_state.window)) ||
        !gtk_stack_get_transition_running(GTK_STACK(app_state.stack))) {
        finish_surface_transition(GINT_TO_POINTER(expanded));
    }
    if (!app_state.demo && !app_state.embedded) {
        gtk_layer_set_keyboard_mode(
            app_state.window,
            expanded ? GTK_LAYER_SHELL_KEYBOARD_MODE_ON_DEMAND : GTK_LAYER_SHELL_KEYBOARD_MODE_NONE);
    }
    if (expanded) {
        gtk_widget_grab_focus(app_state.entry);
    }
}

static void toggle_eye(void) {
    if (app_state.drag_moved) {
        app_state.drag_moved = false;
        return;
    }
    set_expanded(!app_state.expanded);
}

static void head_drag_end(GtkGestureDrag* gesture, double dx, double dy, gpointer data) {
    (void)gesture;
    (void)dx;
    (void)dy;
    (void)data;
    toggle_eye();
}

static void drag_begin(GtkGestureDrag* gesture, double x, double y, gpointer data) {
    (void)gesture;
    (void)x;
    (void)y;
    (void)data;
    app_state.drag_moved = false;
    app_state.move_started = false;
    app_state.drag_origin_x = x;
    app_state.drag_origin_y = y;
    if (app_state.embedded) {
        return;
    }
    app_state.drag_start_top = app_state.margin_top;
    app_state.drag_start_right = app_state.margin_right;
}

static void drag_update(GtkGestureDrag* gesture, double dx, double dy, gpointer data) {
    (void)gesture;
    (void)data;
    if (fabs(dx) > 4.0 || fabs(dy) > 4.0)
        app_state.drag_moved = true;
    if (app_state.embedded) {
        if (app_state.drag_moved && !app_state.move_started) {
            GdkSurface* surface = gtk_native_get_surface(GTK_NATIVE(app_state.window));
            GdkEvent* event = gtk_event_controller_get_current_event(GTK_EVENT_CONTROLLER(gesture));
            if (surface && GDK_IS_TOPLEVEL(surface) && event) {
                gdk_toplevel_begin_move(
                    GDK_TOPLEVEL(surface),
                    gdk_event_get_device(event),
                    (int)gtk_gesture_single_get_current_button(GTK_GESTURE_SINGLE(gesture)),
                    app_state.drag_origin_x,
                    app_state.drag_origin_y,
                    gdk_event_get_time(event));
                app_state.move_started = true;
            }
        }
        return;
    }
    app_state.margin_top = app_state.drag_start_top + (int)dy;
    app_state.margin_right = app_state.drag_start_right - (int)dx;
    apply_position();
}

static void add_message_bubble(const char* role, const char* text) {
    GtkWidget* row = gtk_box_new(GTK_ORIENTATION_HORIZONTAL, 0);
    GtkWidget* label = gtk_label_new(text);
    gtk_label_set_wrap(GTK_LABEL(label), true);
    gtk_label_set_wrap_mode(GTK_LABEL(label), PANGO_WRAP_WORD_CHAR);
    gtk_label_set_xalign(GTK_LABEL(label), 0.0f);
    gtk_label_set_max_width_chars(GTK_LABEL(label), 34);
    gtk_widget_add_css_class(label, strcmp(role, "human") == 0 ? "human-bubble" : "agent-bubble");
    if (strcmp(role, "human") == 0)
        gtk_box_append(GTK_BOX(row), gtk_box_new(GTK_ORIENTATION_HORIZONTAL, 0));
    gtk_box_append(GTK_BOX(row), label);
    gtk_box_append(GTK_BOX(app_state.messages_box), row);
    if (app_state.messages_scroll)
        gtk_widget_set_visible(app_state.messages_scroll, true);
}

static void send_message(void) {
    const char* text = gtk_editable_get_text(GTK_EDITABLE(app_state.entry));
    if (!text || !text[0])
        return;
    if (app_state.demo) {
        add_message_bubble("human", text);
        add_message_bubble(
            "agent",
            AS_TR("Got it. In a live session, the Agent operating this app reads and replies here."));
    } else {
        json_object* params = json_object_new_object();
        json_object_object_add(params, "text", json_object_new_string(text));
        json_object* result = rpc_call("chat.send", params);
        json_object_put(params);
        if (!result) {
            gtk_label_set_text(GTK_LABEL(app_state.state_label), AS_TR("Connection temporarily unavailable"));
            return;
        }
        json_object_put(result);
    }
    gtk_editable_set_text(GTK_EDITABLE(app_state.entry), "");
}

static void send_clicked(GtkButton* button, gpointer data) {
    (void)button;
    (void)data;
    send_message();
}

static void entry_activate(GtkWidget* entry, gpointer data) {
    (void)entry;
    (void)data;
    send_message();
}

static gboolean poll_state(gpointer data) {
    (void)data;
    if (app_state.demo)
        return G_SOURCE_CONTINUE;
    json_object* status = rpc_call("collaboration.status", NULL);
    if (status) {
        json_object* value = NULL;
        const char* summary = AS_TR("Waiting for Agent");
        const char* detail = "";
        bool human_active = false;
        if (json_object_object_get_ex(status, "activity_summary", &value))
            summary = json_object_get_string(value);
        if (json_object_object_get_ex(status, "activity_detail", &value))
            detail = json_object_get_string(value);
        if (json_object_object_get_ex(status, "human_active", &value))
            human_active = json_object_get_boolean(value);
        if (json_object_object_get_ex(status, "agent_allowed", &value)) {
            bool allowed = json_object_get_boolean(value);
            gtk_label_set_text(
                GTK_LABEL(app_state.state_label),
                human_active
                    ? AS_TR("You're in control · AI stepped aside")
                    : (allowed ? AS_TR("AI can operate") : AS_TR("AI is paused")));
        }
        gtk_label_set_text(
            GTK_LABEL(app_state.activity_label),
            summary && summary[0] ? summary : AS_TR("Waiting for Agent"));
        gtk_label_set_text(GTK_LABEL(app_state.detail_label), detail ? detail : "");
        json_object_put(status);
    } else {
        gtk_label_set_text(GTK_LABEL(app_state.state_label), AS_TR("Waiting for AgentSeat"));
    }

    json_object* params = json_object_new_object();
    json_object_object_add(params, "after_id", json_object_new_int64((int64_t)app_state.last_message_id));
    json_object* messages = rpc_call("chat.list", params);
    json_object_put(params);
    if (messages) {
        json_object* array = NULL;
        if (json_object_object_get_ex(messages, "messages", &array) && json_object_is_type(array, json_type_array)) {
            for (size_t i = 0; i < json_object_array_length(array); ++i) {
                json_object* message = json_object_array_get_idx(array, i);
                json_object* role_value = NULL;
                json_object* text_value = NULL;
                json_object* id_value = NULL;
                if (json_object_object_get_ex(message, "role", &role_value) &&
                    json_object_object_get_ex(message, "text", &text_value) &&
                    json_object_object_get_ex(message, "id", &id_value)) {
                    add_message_bubble(json_object_get_string(role_value), json_object_get_string(text_value));
                    app_state.last_message_id = (uint64_t)json_object_get_int64(id_value);
                }
            }
        }
        json_object_put(messages);
    }
    return G_SOURCE_CONTINUE;
}

static gboolean key_pressed(GtkEventControllerKey* controller, guint keyval, guint keycode, GdkModifierType state, gpointer data) {
    (void)controller;
    (void)keycode;
    (void)state;
    (void)data;
    if (keyval == GDK_KEY_Escape && app_state.expanded) {
        set_expanded(false);
        return true;
    }
    return false;
}

static GtkWidget* make_button(const char* label, const char* css_class, GCallback callback, gpointer data) {
    GtkWidget* button = gtk_button_new_with_label(label);
    if (css_class)
        gtk_widget_add_css_class(button, css_class);
    g_signal_connect(button, "clicked", callback, data);
    return button;
}

static GtkWidget* build_head_button(const char* accessible_label);

static GtkWidget* build_panel(void) {
    GtkWidget* panel = gtk_overlay_new();
    gtk_widget_add_css_class(panel, "acrylic-root");

    GtkWidget* veil = gtk_drawing_area_new();
    gtk_drawing_area_set_content_width(GTK_DRAWING_AREA(veil), PANEL_WIDTH);
    gtk_drawing_area_set_content_height(GTK_DRAWING_AREA(veil), PANEL_HEIGHT);
    gtk_drawing_area_set_draw_func(GTK_DRAWING_AREA(veil), draw_acrylic_veil, NULL, NULL);
    gtk_overlay_set_child(GTK_OVERLAY(panel), veil);

    GtkWidget* content = gtk_box_new(GTK_ORIENTATION_VERTICAL, 0);
    gtk_widget_add_css_class(content, "floating-content");
    gtk_widget_set_margin_top(content, 24);
    gtk_widget_set_margin_bottom(content, 7);
    gtk_widget_set_margin_start(content, 26);
    gtk_widget_set_margin_end(content, 7);

    app_state.state_label = gtk_label_new(
        app_state.demo ? AS_TR("AI can operate") : AS_TR("Connecting"));
    gtk_label_set_xalign(GTK_LABEL(app_state.state_label), 0.0f);
    gtk_widget_add_css_class(app_state.state_label, "status-whisper");
    gtk_box_append(GTK_BOX(content), app_state.state_label);

    app_state.activity_label = gtk_label_new(
        app_state.demo ? AS_TR("Reviewing the table layout") : AS_TR("Waiting for Agent"));
    gtk_label_set_xalign(GTK_LABEL(app_state.activity_label), 0.0f);
    gtk_label_set_wrap(GTK_LABEL(app_state.activity_label), true);
    gtk_label_set_lines(GTK_LABEL(app_state.activity_label), 2);
    gtk_label_set_ellipsize(GTK_LABEL(app_state.activity_label), PANGO_ELLIPSIZE_END);
    gtk_widget_add_css_class(app_state.activity_label, "activity-title");
    gtk_widget_set_margin_top(app_state.activity_label, 11);
    gtk_box_append(GTK_BOX(content), app_state.activity_label);

    app_state.detail_label = gtk_label_new(
        app_state.demo
            ? AS_TR("AgentSeat clicks only inside this app. When you take over, AI pauses automatically.")
            : "");
    gtk_label_set_xalign(GTK_LABEL(app_state.detail_label), 0.0f);
    gtk_label_set_wrap(GTK_LABEL(app_state.detail_label), true);
    gtk_label_set_lines(GTK_LABEL(app_state.detail_label), 2);
    gtk_label_set_ellipsize(GTK_LABEL(app_state.detail_label), PANGO_ELLIPSIZE_END);
    gtk_widget_add_css_class(app_state.detail_label, "task-detail");
    gtk_widget_set_margin_top(app_state.detail_label, 5);
    gtk_box_append(GTK_BOX(content), app_state.detail_label);

    GtkWidget* spacer = gtk_box_new(GTK_ORIENTATION_VERTICAL, 0);
    gtk_widget_set_vexpand(spacer, true);
    gtk_box_append(GTK_BOX(content), spacer);

    GtkWidget* scroll = gtk_scrolled_window_new();
    app_state.messages_scroll = scroll;
    gtk_widget_set_visible(scroll, false);
    gtk_widget_set_size_request(scroll, -1, 32);
    gtk_scrolled_window_set_policy(GTK_SCROLLED_WINDOW(scroll), GTK_POLICY_NEVER, GTK_POLICY_NEVER);
    app_state.messages_box = gtk_box_new(GTK_ORIENTATION_VERTICAL, 10);
    gtk_widget_set_valign(app_state.messages_box, GTK_ALIGN_END);
    gtk_scrolled_window_set_child(GTK_SCROLLED_WINDOW(scroll), app_state.messages_box);
    gtk_box_append(GTK_BOX(content), scroll);
    if (app_state.demo) {
        add_message_bubble(
            "agent", AS_TR("I'm checking the table margins on page 3. The body is unchanged."));
        add_message_bubble(
            "human", AS_TR("Align the header first. Wait for my confirmation before changing the body."));
    }

    GtkWidget* conversation = gtk_box_new(GTK_ORIENTATION_HORIZONTAL, 0);
    gtk_widget_add_css_class(conversation, "conversation-ribbon");
    gtk_widget_set_valign(conversation, GTK_ALIGN_END);
    /* The one persistent head is overlaid at the bottom-right. Reserve its
     * footprint so only the task material moves during expand/collapse. */
    gtk_widget_set_margin_end(conversation, HEAD_DRAW_SIZE + 6);
    GtkWidget* composer = gtk_box_new(GTK_ORIENTATION_HORIZONTAL, 6);
    gtk_widget_add_css_class(composer, "composer");
    app_state.entry = gtk_text_new();
    gtk_widget_add_css_class(app_state.entry, "chat-entry");
    GtkEventController* entry_motion = gtk_event_controller_motion_new();
    gtk_event_controller_set_propagation_phase(entry_motion, GTK_PHASE_CAPTURE);
    g_signal_connect(entry_motion, "enter", G_CALLBACK(entry_cursor_enter), NULL);
    gtk_widget_add_controller(app_state.entry, entry_motion);
    g_signal_connect(app_state.entry, "notify::cursor", G_CALLBACK(entry_cursor_changed), NULL);
    if (app_state.arrow_cursor)
        gtk_widget_set_cursor(app_state.entry, app_state.arrow_cursor);
    gtk_text_set_placeholder_text(GTK_TEXT(app_state.entry), AS_TR("Tell AI what to do next…"));
    gtk_text_set_propagate_text_width(GTK_TEXT(app_state.entry), false);
    gtk_text_set_truncate_multiline(GTK_TEXT(app_state.entry), true);
    gtk_widget_set_hexpand(app_state.entry, true);
    set_accessible_label(app_state.entry, AS_TR("Message AgentSeat AI"));
    g_signal_connect(app_state.entry, "activate", G_CALLBACK(entry_activate), NULL);
    GtkWidget* send = make_button("↑", "send-button", G_CALLBACK(send_clicked), NULL);
    set_accessible_label(send, AS_TR("Send message"));
    gtk_box_append(GTK_BOX(composer), app_state.entry);
    gtk_box_append(GTK_BOX(composer), send);
    gtk_widget_set_hexpand(composer, true);
    gtk_box_append(GTK_BOX(conversation), composer);
    gtk_box_append(GTK_BOX(content), conversation);
    gtk_overlay_add_overlay(GTK_OVERLAY(panel), content);
    return panel;
}

static GtkWidget* build_head_button(const char* accessible_label) {
    GtkWidget* head = gtk_drawing_area_new();
    gtk_widget_set_size_request(head, HEAD_DRAW_SIZE, HEAD_DRAW_SIZE);
    gtk_drawing_area_set_draw_func(GTK_DRAWING_AREA(head), draw_eye, NULL, NULL);
    gtk_widget_set_halign(head, GTK_ALIGN_CENTER);
    gtk_widget_set_valign(head, GTK_ALIGN_CENTER);
    /* Cursor surfaces bypass the AgentSeat output viewport. Draw the host's
     * native 20 px arrow directly instead of scaling a GTK theme bitmap. */
    if (app_state.arrow_cursor)
        gtk_widget_set_cursor(head, app_state.arrow_cursor);
    set_accessible_label(head, accessible_label);
    GtkGesture* drag = gtk_gesture_drag_new();
    gtk_event_controller_set_propagation_phase(GTK_EVENT_CONTROLLER(drag), GTK_PHASE_CAPTURE);
    g_signal_connect(drag, "drag-begin", G_CALLBACK(drag_begin), NULL);
    g_signal_connect(drag, "drag-update", G_CALLBACK(drag_update), NULL);
    g_signal_connect(drag, "drag-end", G_CALLBACK(head_drag_end), NULL);
    gtk_widget_add_controller(head, GTK_EVENT_CONTROLLER(drag));
    return head;
}

static GtkWidget* build_collapsed_placeholder(void) {
    GtkWidget* placeholder = gtk_drawing_area_new();
    gtk_widget_set_hexpand(placeholder, true);
    gtk_widget_set_vexpand(placeholder, true);
    return placeholder;
}

static void load_css(void) {
    const char* css =
        "window.agentseat-hud, window.agentseat-hud.background, window.agentseat-hud.csd { padding: 0; margin: 0; border: 0; border-radius: 0; outline: 0; background-color: transparent; background-image: none; box-shadow: none; color: #f6f9fc; font-family: sans-serif; }"
        "window.agentseat-hud > contents, window.agentseat-hud.csd > contents { padding: 0; margin: 0; border: 0; border-radius: 0; outline: 0; background-color: transparent; background-image: none; box-shadow: none; }"
        "window.agentseat-hud > stack { padding: 0; margin: 0; background-color: transparent; background-image: none; box-shadow: none; }"
        ".acrylic-root, .floating-content { background: transparent; }"
        ".status-whisper { color: rgba(246,250,253,.74); font-size: 10px; font-weight: 560; }"
        ".activity-title { color: #ffffff; font-size: 17px; font-weight: 680; line-height: 1.18; }"
        ".task-detail { color: rgba(247,250,253,.80); font-size: 11px; line-height: 1.34; }"
        ".agent-bubble, .human-bubble { padding: 6px 10px; border-radius: 999px; font-size: 11px; }"
        ".agent-bubble { color: rgba(247,251,254,.90); background: rgba(255,255,255,.08); }"
        ".human-bubble { color: rgba(247,251,254,.94); background: rgba(119,169,202,.28); margin-left: 38px; }"
        ".conversation-ribbon { margin-top: 8px; }"
        ".composer { min-height: 46px; padding: 2px 3px 2px 13px; border-radius: 999px; background: #ffffff; border: 1px solid rgba(255,255,255,.84); box-shadow: 0 7px 18px rgba(0,0,0,.20); }"
        ".composer text.chat-entry, .composer text.chat-entry:focus { min-height: 38px; padding: 0; background: transparent; background-image: none; border-style: none; border-width: 0; outline-style: none; outline-width: 0; box-shadow: none; color: #15191d; caret-color: #15191d; font-size: 12px; }"
        ".composer text.chat-entry placeholder { color: rgba(30,38,44,.62); }"
        ".send-button { min-height: 38px; min-width: 38px; padding: 0; border: 0; border-radius: 999px; color: #15191d; background: transparent; background-image: none; box-shadow: none; font-size: 17px; font-weight: 700; }"
        ".send-button label { color: #15191d; }"
        ".send-button:hover { color: #080a0c; background: rgba(18,24,28,.06); } .send-button:active { background: rgba(18,24,28,.12); }";
    GtkCssProvider* provider = gtk_css_provider_new();
    gtk_css_provider_load_from_string(provider, css);
    gtk_style_context_add_provider_for_display(
        gdk_display_get_default(), GTK_STYLE_PROVIDER(provider), GTK_STYLE_PROVIDER_PRIORITY_APPLICATION);
    g_object_unref(provider);
}

static void activate(GtkApplication* application, gpointer data) {
    (void)data;
    load_css();
    app_state.application = application;
    app_state.window = GTK_WINDOW(gtk_application_window_new(application));
    gtk_widget_add_css_class(GTK_WIDGET(app_state.window), "agentseat-hud");
    gtk_widget_remove_css_class(GTK_WIDGET(app_state.window), "background");
    gtk_window_set_title(app_state.window, "AgentSeat AI");
    gtk_window_set_decorated(app_state.window, false);
    gtk_window_set_resizable(app_state.window, false);
    if (!app_state.demo && !app_state.embedded) {
        if (!gtk_layer_is_supported()) {
            fprintf(
                stderr,
                "agentseat-eye: %s\n",
                AS_TR("host compositor does not support layer shell"));
            g_application_quit(G_APPLICATION(application));
            return;
        }
        gtk_layer_init_for_window(app_state.window);
        gtk_layer_set_namespace(app_state.window, "agentseat-eye");
        gtk_layer_set_layer(app_state.window, GTK_LAYER_SHELL_LAYER_OVERLAY);
        gtk_layer_set_anchor(app_state.window, GTK_LAYER_SHELL_EDGE_TOP, true);
        gtk_layer_set_anchor(app_state.window, GTK_LAYER_SHELL_EDGE_RIGHT, true);
        gtk_layer_set_exclusive_zone(app_state.window, -1);
        gtk_layer_set_keyboard_mode(app_state.window, GTK_LAYER_SHELL_KEYBOARD_MODE_NONE);
        apply_position();
    }

    GtkSettings* settings = gtk_settings_get_default();
    gboolean animations = true;
    if (settings) {
        g_object_get(settings, "gtk-enable-animations", &animations, NULL);
    }
    app_state.animations = animations;
    app_state.animation_start = g_get_monotonic_time();
    app_state.arrow_cursor = create_native_arrow_cursor();

    app_state.stack = gtk_stack_new();
    /* GTK's themed cursors are submitted as 32 px buffers in this physical
     * pixel micro-host. Make every HUD child inherit the host-native 20 px
     * arrow; widgets with a semantic cursor, such as the text entry, override
     * it with their own native-size vector cursor. */
    if (app_state.arrow_cursor)
        gtk_widget_set_cursor(app_state.stack, app_state.arrow_cursor);
    gtk_stack_set_hhomogeneous(GTK_STACK(app_state.stack), false);
    gtk_stack_set_vhomogeneous(GTK_STACK(app_state.stack), false);
    gtk_stack_set_transition_type(GTK_STACK(app_state.stack), animations ? GTK_STACK_TRANSITION_TYPE_OVER_LEFT_RIGHT : GTK_STACK_TRANSITION_TYPE_NONE);
    gtk_stack_set_transition_duration(GTK_STACK(app_state.stack), 210);
    g_signal_connect(app_state.stack, "notify::transition-running", G_CALLBACK(transition_running_changed), NULL);
    app_state.eye_root = build_collapsed_placeholder();
    app_state.panel_root = build_panel();
    gtk_stack_add_named(GTK_STACK(app_state.stack), app_state.eye_root, "eye");
    gtk_stack_add_named(GTK_STACK(app_state.stack), app_state.panel_root, "panel");

    GtkWidget* hud_root = gtk_overlay_new();
    gtk_overlay_set_child(GTK_OVERLAY(hud_root), app_state.stack);
    app_state.head_area = build_head_button(AS_TR("Expand the AgentSeat AI head"));
    gtk_widget_set_halign(app_state.head_area, GTK_ALIGN_END);
    gtk_widget_set_valign(app_state.head_area, GTK_ALIGN_END);
    gtk_overlay_add_overlay(GTK_OVERLAY(hud_root), app_state.head_area);
    gtk_window_set_child(app_state.window, hud_root);

    GtkEventController* keys = gtk_event_controller_key_new();
    g_signal_connect(keys, "key-pressed", G_CALLBACK(key_pressed), NULL);
    gtk_widget_add_controller(GTK_WIDGET(app_state.window), keys);
    set_expanded(app_state.demo);
    gtk_window_present(app_state.window);
    g_idle_add(apply_surface_input_region_idle, NULL);
    g_timeout_add(33, animate_eye, NULL);
    g_timeout_add(300, poll_state, NULL);
    poll_state(NULL);
}

int main(int argc, char** argv) {
    agentseat_i18n_init();
    const char* runtime = getenv("XDG_RUNTIME_DIR");
    if (!runtime)
        runtime = "/tmp";
    snprintf(app_state.socket_path, sizeof(app_state.socket_path), "%s/agentseat/control.sock", runtime);
    const char* render_head_path = NULL;
    for (int i = 1; i < argc; ++i) {
        if (strcmp(argv[i], "--socket") == 0 && i + 1 < argc) {
            snprintf(app_state.socket_path, sizeof(app_state.socket_path), "%s", argv[++i]);
        } else if (strcmp(argv[i], "--demo") == 0) {
            app_state.demo = true;
        } else if (strcmp(argv[i], "--embedded") == 0) {
            app_state.embedded = true;
        } else if (strcmp(argv[i], "--render-head") == 0 && i + 1 < argc) {
            render_head_path = argv[++i];
        } else {
            fprintf(
                stderr,
                AS_TR("usage: %s [--socket PATH] [--demo] [--embedded] [--render-head PNG]\n"),
                argv[0]);
            return 2;
        }
    }
    if (render_head_path) {
        app_state.animation_start = g_get_monotonic_time();
        cairo_surface_t* surface = cairo_image_surface_create(
            CAIRO_FORMAT_ARGB32, COLLAPSED_SIZE, COLLAPSED_SIZE);
        cairo_t* cr = cairo_create(surface);
        cairo_set_operator(cr, CAIRO_OPERATOR_SOURCE);
        cairo_set_source_rgba(cr, 0, 0, 0, 0);
        cairo_paint(cr);
        cairo_set_operator(cr, CAIRO_OPERATOR_OVER);
        draw_eye(NULL, cr, COLLAPSED_SIZE, COLLAPSED_SIZE, NULL);
        cairo_destroy(cr);
        cairo_status_t status = cairo_surface_write_to_png(surface, render_head_path);
        cairo_surface_destroy(surface);
        if (status != CAIRO_STATUS_SUCCESS) {
            fprintf(
                stderr,
                AS_TR("agentseat-eye: cannot render head: %s\n"),
                cairo_status_to_string(status));
            return 1;
        }
        return 0;
    }
    GtkApplication* application = gtk_application_new(
        "dev.vimalinx.agentseat.eye", G_APPLICATION_NON_UNIQUE);
    g_signal_connect(application, "activate", G_CALLBACK(activate), NULL);
    int result = g_application_run(G_APPLICATION(application), 0, NULL);
    g_clear_object(&app_state.arrow_cursor);
    g_object_unref(application);
    return result;
}
