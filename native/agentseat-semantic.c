#define _GNU_SOURCE
#include "agentseat-semantic.h"
#include <gio/gio.h>
#include <errno.h>
#include <fcntl.h>
#include <math.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <sys/stat.h>
#include <unistd.h>

/* The optional bridge prepares a scoped target but cannot perform a write.
 * D-Bus calls are dispatched once, by the daemon control loop. GIO setup and
 * replies run asynchronously on this isolated context; no GUI call blocks it. */
struct as_semantic {
    char *directory, *session_id, *scope_id;
    pid_t microhost_pid, bridge_pid;
    GMainContext* context;
    GPollFD polls[AS_SEMANTIC_POLL_MAX];
    int poll_count, priority;
    GCancellable* cancel;
    GSocketClient* client;
    GSocketConnection* socket;
    GDBusConnection* bus;
    GByteArray* incoming;
    char* outgoing;
    json_object* packet;
    GVariant* arguments;
    const char *interface, *member, *stage;
    char error[128];
    bool busy, ready, sent, replied, succeeded, cancelled;
    gint64 deadline;
    unsigned call_timeout;
};

static const char* str(json_object* object, const char* name) {
    json_object* value = NULL;
    if (!object || !json_object_object_get_ex(object, name, &value) || !json_object_is_type(value, json_type_string)) return NULL;
    const char* text = json_object_get_string(value);
    return strlen(text) == (size_t)json_object_get_string_len(value) ? text : NULL;
}
static int number(json_object* object, const char* name) {
    json_object* value = NULL;
    if (!object || !json_object_object_get_ex(object, name, &value) || !json_object_is_type(value, json_type_int)) return -1;
    int64_t n = json_object_get_int64(value);
    return n >= 0 && n <= INT32_MAX ? (int)n : -1;
}
static bool process_info(pid_t pid, pid_t* parent, char start[32]) {
    char path[64]; snprintf(path, sizeof(path), "/proc/%d/stat", pid);
    char* data = NULL;
    if (pid <= 1 || !g_file_get_contents(path, &data, NULL, NULL)) return false;
    char* end = strrchr(data, ')');
    char** fields = end && end[1] ? g_strsplit(end + 2, " ", -1) : NULL;
    bool valid = fields && g_strv_length(fields) > 19;
    if (valid) {
        *parent = (pid_t)strtol(fields[1], NULL, 10);
        snprintf(start, 32, "%s", fields[19]);
    }
    g_strfreev(fields); g_free(data);
    return valid;
}
static bool identity(pid_t pid, const char* start, pid_t root) {
    pid_t parent; char current[32];
    if (!start || !process_info(pid, &parent, current) || strcmp(start, current)) return false;
    for (unsigned i = 0; i < 32; ++i) {
        if (pid == root) return true;
        if (!process_info(pid, &parent, current)) return false;
        pid = parent;
    }
    return false;
}
static json_object* receipt_file(struct as_semantic* self) {
    char* path = g_build_filename(self->directory, "a11y.json", NULL);
    int fd = open(path, O_RDONLY | O_CLOEXEC | O_NOFOLLOW);
    g_free(path);
    if (fd < 0) return NULL;
    struct stat statbuf;
    char data[16385];
    bool valid = fstat(fd, &statbuf) == 0 && S_ISREG(statbuf.st_mode) && statbuf.st_uid == getuid() && statbuf.st_size > 0 && statbuf.st_size <= 16384;
    ssize_t size = valid ? read(fd, data, (size_t)statbuf.st_size) : -1;
    close(fd);
    if (!valid || size != statbuf.st_size) return NULL;
    data[size] = '\0';
    return json_tokener_parse(data);
}
static void fail(struct as_semantic* self, const char* error) {
    snprintf(self->error, sizeof(self->error), "%s", error);
    self->stage = self->sent ? "outcome_unknown" : self->cancelled ? "cancelled" : "rejected";
    self->busy = self->ready = false;
}
static void io_error(struct as_semantic* self, GError* error, const char* fallback) {
    fail(self, self->cancelled ? "OPERATION_CANCELLED" : fallback);
    g_clear_error(&error);
}
static void dispose_socket(struct as_semantic* self) {
    if (self->socket) {
        g_io_stream_close(G_IO_STREAM(self->socket), NULL, NULL);
        g_clear_object(&self->socket);
    }
}
static void prepare_arguments(struct as_semantic* self) {
    json_object* action = NULL;
    json_object_object_get_ex(self->packet, "action", &action);
    const char* kind = str(action, "kind");
    if (!kind) { fail(self, "INVALID_PREPARED_ACTION"); return; }
    self->interface = NULL;
    if (!strcmp(kind, "a11y.perform") && number(action, "action_index") >= 0) {
        self->interface = "org.a11y.atspi.Action"; self->member = "DoAction";
        self->arguments = g_variant_ref_sink(g_variant_new("(i)", number(action, "action_index")));
    } else if (!strcmp(kind, "a11y.set_text") && str(action, "text") && strlen(str(action, "text")) <= 32768) {
        self->interface = "org.a11y.atspi.EditableText"; self->member = "SetTextContents";
        self->arguments = g_variant_ref_sink(g_variant_new("(s)", str(action, "text")));
    } else if (!strcmp(kind, "a11y.set_value")) {
        json_object* value = NULL;
        if (json_object_object_get_ex(action, "value", &value) &&
            (json_object_is_type(value, json_type_int) || json_object_is_type(value, json_type_double)) && isfinite(json_object_get_double(value))) {
            self->interface = "org.freedesktop.DBus.Properties"; self->member = "Set";
            self->arguments = g_variant_ref_sink(g_variant_new("(ssv)", "org.a11y.atspi.Value", "CurrentValue", g_variant_new_double(json_object_get_double(value))));
        }
    } else if (!strcmp(kind, "a11y.focus")) {
        self->interface = "org.a11y.atspi.Component"; self->member = "GrabFocus";
        self->arguments = g_variant_ref_sink(g_variant_new("()"));
    } else if (!strcmp(kind, "a11y.select") && number(action, "index") >= 0) {
        self->interface = "org.a11y.atspi.Selection"; self->member = "SelectChild";
        self->arguments = g_variant_ref_sink(g_variant_new("(i)", number(action, "index")));
    } else if (!strcmp(kind, "a11y.scroll_to")) {
        self->interface = "org.a11y.atspi.Component"; self->member = "ScrollTo";
        self->arguments = g_variant_ref_sink(g_variant_new("(u)", 6U));
    }
    if (!self->interface) fail(self, "INVALID_PREPARED_ACTION");
}
static void target_pid_ready(GObject* object, GAsyncResult* result, gpointer data) {
    struct as_semantic* self = data;
    GError* error = NULL;
    GVariant* reply = g_dbus_connection_call_finish(G_DBUS_CONNECTION(object), result, &error);
    if (!reply) { io_error(self, error, "TARGET_UNAVAILABLE"); return; }
    guint32 pid = 0; g_variant_get(reply, "(u)", &pid); g_variant_unref(reply);
    if (self->cancelled) { fail(self, "OPERATION_CANCELLED"); return; }
    if ((int)pid != number(self->packet, "pid") || !identity((pid_t)pid, str(self->packet, "process_start"), number(self->packet, "app_pid")) ||
        !identity(number(self->packet, "app_pid"), str(self->packet, "app_start"), self->bridge_pid)) {
        fail(self, "TARGET_IDENTITY_CHANGED"); return;
    }
    prepare_arguments(self);
    if (self->interface) { self->ready = true; self->stage = "prepared"; }
}
static void bus_identity_ready(GObject* object, GAsyncResult* result, gpointer data) {
    struct as_semantic* self = data;
    GError* error = NULL;
    GVariant* reply = g_dbus_connection_call_finish(G_DBUS_CONNECTION(object), result, &error);
    if (!reply) { io_error(self, error, "PRIVATE_BUS_UNAVAILABLE"); return; }
    guint32 pid; g_variant_get(reply, "(u)", &pid); g_variant_unref(reply);
    if ((int)pid != number(self->packet, "bus_pid") || !identity(pid, str(self->packet, "bus_start"), self->bridge_pid)) {
        fail(self, "PRIVATE_BUS_IDENTITY_MISMATCH"); return;
    }
    if (self->cancelled) { fail(self, "OPERATION_CANCELLED"); return; }
    self->stage = "validating_target";
    g_dbus_connection_call(self->bus, "org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus",
        "GetConnectionUnixProcessID", g_variant_new("(s)", str(self->packet, "owner")), G_VARIANT_TYPE("(u)"),
        G_DBUS_CALL_FLAGS_NONE, 1000, self->cancel, target_pid_ready, self);
}
static void bus_ready(GObject* object, GAsyncResult* result, gpointer data) {
    (void)object;
    struct as_semantic* self = data;
    GError* error = NULL;
    self->bus = g_dbus_connection_new_for_address_finish(result, &error);
    if (!self->bus) { io_error(self, error, "PRIVATE_BUS_UNAVAILABLE"); return; }
    g_dbus_connection_set_exit_on_close(self->bus, false);
    GIOStream* stream = g_dbus_connection_get_stream(self->bus);
    if (!G_IS_SOCKET_CONNECTION(stream)) { fail(self, "PRIVATE_BUS_NOT_UNIX"); return; }
    GSocket* socket = g_socket_connection_get_socket(G_SOCKET_CONNECTION(stream));
    struct ucred credentials; socklen_t length = sizeof(credentials);
    if (getsockopt(g_socket_get_fd(socket), SOL_SOCKET, SO_PEERCRED, &credentials, &length) ||
        credentials.uid != getuid() || credentials.pid != number(self->packet, "bus_peer_pid") ||
        !identity(credentials.pid, str(self->packet, "bus_peer_start"), self->bridge_pid)) {
        fail(self, "PRIVATE_BUS_IDENTITY_MISMATCH"); return;
    }
    if (self->cancelled) { fail(self, "OPERATION_CANCELLED"); return; }
    self->stage = "validating_bus";
    g_dbus_connection_call(self->bus, "org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus",
        "GetConnectionUnixProcessID", g_variant_new("(s)", "org.freedesktop.DBus"), G_VARIANT_TYPE("(u)"),
        G_DBUS_CALL_FLAGS_NONE, 1000, self->cancel, bus_identity_ready, self);
}
static void read_ready(GObject* object, GAsyncResult* result, gpointer data) {
    struct as_semantic* self = data;
    GError* error = NULL;
    GBytes* bytes = g_input_stream_read_bytes_finish(G_INPUT_STREAM(object), result, &error);
    if (!bytes) { io_error(self, error, "BRIDGE_READ_FAILED"); return; }
    gsize size = 0; const char* buffer = g_bytes_get_data(bytes, &size);
    if (!size || self->incoming->len + size > 65536) {
        g_bytes_unref(bytes); fail(self, "BRIDGE_RESPONSE_INVALID"); return;
    }
    g_byte_array_append(self->incoming, (const guint8*)buffer, size); g_bytes_unref(bytes);
    if (!memchr(self->incoming->data, '\n', self->incoming->len)) {
        g_input_stream_read_bytes_async(G_INPUT_STREAM(object), 16384, G_PRIORITY_DEFAULT, self->cancel, read_ready, self);
        return;
    }
    g_byte_array_append(self->incoming, (const guint8*)"", 1);
    json_object* response = json_tokener_parse((const char*)self->incoming->data);
    json_object *packet = NULL, *failure = NULL;
    if (response) json_object_object_get_ex(response, "error", &failure);
    if (failure) {
        const char* kind = str(failure, "kind"); fail(self, kind ? kind : "BRIDGE_PREPARE_FAILED");
    } else if (response && json_object_object_get_ex(response, "result", &packet) && str(packet, "scope_id") &&
        !strcmp(str(packet, "scope_id"), self->scope_id) && str(packet, "bus_address") && g_str_has_prefix(str(packet, "bus_address"), "unix:") &&
        str(packet, "owner") && str(packet, "owner")[0] == ':' && g_dbus_is_unique_name(str(packet, "owner")) &&
        str(packet, "path") && g_variant_is_object_path(str(packet, "path"))) {
        self->packet = json_object_get(packet);
    } else fail(self, "BRIDGE_RESPONSE_INVALID");
    if (response) json_object_put(response);
    dispose_socket(self);
    if (!self->packet || !self->busy) return;
    self->stage = "connecting_bus";
    g_dbus_connection_new_for_address(str(self->packet, "bus_address"),
        G_DBUS_CONNECTION_FLAGS_AUTHENTICATION_CLIENT | G_DBUS_CONNECTION_FLAGS_MESSAGE_BUS_CONNECTION,
        NULL, self->cancel, bus_ready, self);
}
static void written(GObject* object, GAsyncResult* result, gpointer data) {
    struct as_semantic* self = data;
    GError* error = NULL; gsize size = 0;
    if (!g_output_stream_write_all_finish(G_OUTPUT_STREAM(object), result, &size, &error)) {
        io_error(self, error, "BRIDGE_WRITE_FAILED"); return;
    }
    g_input_stream_read_bytes_async(g_io_stream_get_input_stream(G_IO_STREAM(self->socket)), 16384,
        G_PRIORITY_DEFAULT, self->cancel, read_ready, self);
}
static void connected(GObject* object, GAsyncResult* result, gpointer data) {
    struct as_semantic* self = data;
    GError* error = NULL;
    self->socket = g_socket_client_connect_finish(G_SOCKET_CLIENT(object), result, &error);
    if (!self->socket) { io_error(self, error, "A11Y_UNAVAILABLE"); return; }
    struct ucred credentials; socklen_t length = sizeof(credentials);
    int fd = g_socket_get_fd(g_socket_connection_get_socket(self->socket));
    if (getsockopt(fd, SOL_SOCKET, SO_PEERCRED, &credentials, &length) || credentials.uid != getuid() || credentials.pid != self->bridge_pid) {
        fail(self, "BRIDGE_PEER_MISMATCH"); return;
    }
    g_output_stream_write_all_async(g_io_stream_get_output_stream(G_IO_STREAM(self->socket)), self->outgoing,
        strlen(self->outgoing), G_PRIORITY_DEFAULT, self->cancel, written, self);
}
static void action_replied(GObject* object, GAsyncResult* result, gpointer data) {
    struct as_semantic* self = data;
    GError* error = NULL;
    GVariant* reply = g_dbus_connection_call_finish(G_DBUS_CONNECTION(object), result, &error);
    self->ready = self->busy = false;
    if (!reply) {
        char* remote = error ? g_dbus_error_get_remote_error(error) : NULL;
        snprintf(self->error, sizeof(self->error), "%s", remote ? remote : "SEMANTIC_REPLY_UNCONFIRMED");
        /* Only standard dispatch refusals prove that no method was executed.
         * Other remote errors can follow side effects and remain ambiguous. */
        bool refused = remote && (!strcmp(remote, "org.freedesktop.DBus.Error.NotSupported") ||
            !strcmp(remote, "org.freedesktop.DBus.Error.UnknownMethod") ||
            !strcmp(remote, "org.freedesktop.DBus.Error.UnknownInterface") ||
            !strcmp(remote, "org.freedesktop.DBus.Error.UnknownObject"));
        self->replied = refused; /* Other errors may originate from the bus itself. */
        self->stage = refused ? "rejected" : "outcome_unknown";
        g_free(remote); g_clear_error(&error);
        return;
    }
    self->replied = true;
    if (g_variant_is_of_type(reply, G_VARIANT_TYPE("(b)"))) {
        gboolean value; g_variant_get(reply, "(b)", &value); self->succeeded = value;
    } else self->succeeded = g_variant_is_of_type(reply, G_VARIANT_TYPE("()"));
    g_variant_unref(reply);
    self->stage = self->succeeded ? "finished" : "rejected";
    if (!self->succeeded) snprintf(self->error, sizeof(self->error), "APPLICATION_REJECTED_ACTION");
}

struct as_semantic* as_semantic_new(const char* path, const char* session, pid_t microhost) {
    struct as_semantic* self = g_new0(struct as_semantic, 1);
    self->directory = g_path_get_dirname(path); self->session_id = g_strdup(session);
    self->microhost_pid = microhost; self->context = g_main_context_new();
    self->stage = "idle";
    return self;
}
bool as_semantic_start(struct as_semantic* self, const char* method, json_object* params) {
    if (self->busy) return false;
    dispose_socket(self);
    if (self->bus) { g_dbus_connection_close(self->bus, NULL, NULL, NULL); g_clear_object(&self->bus); }
    g_clear_object(&self->cancel); g_clear_object(&self->client);
    if (self->incoming) g_byte_array_unref(self->incoming);
    if (self->arguments) g_variant_unref(self->arguments);
    if (self->packet) json_object_put(self->packet);
    g_clear_pointer(&self->outgoing, g_free); g_clear_pointer(&self->scope_id, g_free);
    self->packet = NULL; self->arguments = NULL; self->interface = NULL;
    self->incoming = g_byte_array_new(); self->cancel = g_cancellable_new();
    self->sent = self->replied = self->succeeded = self->cancelled = self->ready = false;
    self->error[0] = '\0'; self->busy = true; self->stage = "preparing";
    self->deadline = g_get_monotonic_time() + 3000000;
    int timeout = number(params, "timeout_ms"); self->call_timeout = timeout >= 100 && timeout <= 10000 ? (unsigned)timeout : 5000;
    json_object* receipt = receipt_file(self);
    if (!receipt || !str(receipt, "scope_id") || !identity(number(receipt, "bridge_pid"), str(receipt, "bridge_start"), self->microhost_pid)) {
        if (receipt) json_object_put(receipt);
        fail(self, "A11Y_SCOPE_UNAVAILABLE"); return true;
    }
    self->bridge_pid = number(receipt, "bridge_pid"); self->scope_id = g_strdup(str(receipt, "scope_id"));
    json_object_put(receipt);
    json_object* request = NULL; json_object_deep_copy(params, &request, NULL);
    if (!request) { fail(self, "RESOURCE_LIMIT"); return true; }
    json_object_object_add(request, "method", json_object_new_string("a11y.prepare"));
    json_object_object_add(request, "kind", json_object_new_string(method));
    json_object_object_add(request, "scope_id", json_object_new_string(self->scope_id));
    json_object_object_add(request, "session_id", json_object_new_string(self->session_id));
    self->outgoing = g_strconcat(json_object_to_json_string_ext(request, JSON_C_TO_STRING_PLAIN), "\n", NULL);
    json_object_put(request);
    char* path = g_build_filename(self->directory, "a11y.sock", NULL);
    GSocketAddress* address = g_unix_socket_address_new(path); g_free(path);
    self->client = g_socket_client_new(); g_socket_client_set_timeout(self->client, 3);
    g_main_context_push_thread_default(self->context);
    g_socket_client_connect_async(self->client, G_SOCKET_CONNECTABLE(address), self->cancel, connected, self);
    g_main_context_pop_thread_default(self->context);
    g_object_unref(address);
    return true;
}
void as_semantic_submit(struct as_semantic* self) {
    if (!self->ready || self->sent || self->cancelled || !self->bus || self->error[0]) return;
    if (!identity(number(self->packet, "pid"), str(self->packet, "process_start"), number(self->packet, "app_pid"))) {
        fail(self, "TARGET_IDENTITY_CHANGED"); return;
    }
    self->sent = true; self->ready = false; self->stage = "in_flight";
    self->deadline = g_get_monotonic_time() + ((gint64)self->call_timeout + 1000) * 1000;
    g_main_context_push_thread_default(self->context);
    g_dbus_connection_call(self->bus, str(self->packet, "owner"), str(self->packet, "path"), self->interface, self->member,
        self->arguments, NULL, G_DBUS_CALL_FLAGS_NO_AUTO_START, (gint)self->call_timeout, NULL, action_replied, self);
    g_main_context_pop_thread_default(self->context);
}
void as_semantic_cancel(struct as_semantic* self) {
    if (!self || !self->busy) return;
    self->cancelled = true;
    if (!self->sent) {
        self->ready = false;
        if (!strcmp(self->stage, "prepared")) fail(self, "OPERATION_CANCELLED");
        else g_cancellable_cancel(self->cancel);
    }
    /* A dispatched method is not recalled. Keep its bounded pending reply so a
     * later application response can be recorded without replaying the method. */
}
void as_semantic_status(struct as_semantic* self, struct as_semantic_status* out) {
    if (!self) { *out = (struct as_semantic_status){.stage = "unavailable", .error = ""}; return; }
    *out = (struct as_semantic_status){self->stage, self->error, self->busy, self->ready, self->sent, self->replied, self->succeeded, self->cancelled};
}
int as_semantic_prepare_poll(struct as_semantic* self, struct pollfd* polls, int* timeout) {
    if (!self) return 0;
    if (self->busy && !self->sent && g_get_monotonic_time() >= self->deadline) {
        if (self->ready) fail(self, "PREPARATION_EXPIRED");
        else g_cancellable_cancel(self->cancel);
    }
    g_main_context_acquire(self->context);
    gboolean pending = g_main_context_prepare(self->context, &self->priority);
    gint wait = -1;
    self->poll_count = g_main_context_query(self->context, self->priority, &wait, self->polls, AS_SEMANTIC_POLL_MAX);
    if (self->poll_count > AS_SEMANTIC_POLL_MAX) {
        self->poll_count = AS_SEMANTIC_POLL_MAX;
        as_semantic_cancel(self); wait = 0;
    }
    if (pending) wait = 0;
    if (self->busy && !self->sent) {
        int deadline_ms = (int)MAX(0, (self->deadline - g_get_monotonic_time()) / 1000);
        if (wait < 0 || wait > deadline_ms) wait = deadline_ms;
    }
    if (wait >= 0 && (*timeout < 0 || wait < *timeout)) *timeout = wait;
    for (int i = 0; i < self->poll_count; ++i)
        polls[i] = (struct pollfd){self->polls[i].fd, self->polls[i].events, 0};
    return self->poll_count;
}
void as_semantic_dispatch(struct as_semantic* self, const struct pollfd* polls) {
    if (!self) return;
    for (int i = 0; i < self->poll_count; ++i) self->polls[i].revents = polls[i].revents;
    if (g_main_context_check(self->context, self->priority, self->polls, self->poll_count)) {
        g_main_context_push_thread_default(self->context);
        g_main_context_dispatch(self->context);
        g_main_context_pop_thread_default(self->context);
    }
    g_main_context_release(self->context);
}
void as_semantic_free(struct as_semantic* self) {
    if (!self) return;
    if (self->cancel) g_cancellable_cancel(self->cancel);
    if (self->bus) g_dbus_connection_close(self->bus, NULL, NULL, NULL);
    gint64 until = g_get_monotonic_time() + 500000;
    while (self->busy && g_get_monotonic_time() < until) {
        g_main_context_push_thread_default(self->context);
        g_main_context_iteration(self->context, false);
        g_main_context_pop_thread_default(self->context);
        usleep(1000);
    }
    /* Pending async callbacks hold self as user data. At process shutdown, if a
     * library callback is still outstanding, let process teardown reclaim it. */
    if (self->busy) return;
    dispose_socket(self);
    g_clear_object(&self->client); g_clear_object(&self->cancel); g_clear_object(&self->bus);
    g_main_context_unref(self->context);
    if (self->incoming) g_byte_array_unref(self->incoming);
    if (self->arguments) g_variant_unref(self->arguments);
    if (self->packet) json_object_put(self->packet);
    g_free(self->directory); g_free(self->session_id); g_free(self->scope_id); g_free(self->outgoing); g_free(self);
}
