/* Ordinary wl_data_device bridging for the human-facing window. No data-control
 * access to the parent desktop. Offers are lazy: only a physical human gesture
 * can read parent data or publish an application selection. Agent Unicode
 * offers and imported selections never propagate back to the parent. */
#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <signal.h>
#include <stdlib.h>
#include <stdio.h>
#include <string.h>
#include <time.h>
#include <unistd.h>
#include <wayland-client.h>
#include <wlr/types/wlr_data_device.h>
#include "host-clipboard.h"
#include "seat.h"

struct host_offer {
    struct wl_list link;
    struct wl_data_offer *proxy;
    struct wl_array mimes;
    unsigned refs;
    bool pending;
};
struct imported_source {
    struct wlr_data_source base;
    struct cg_host_clipboard *bridge;
    struct host_offer *offer;
};
struct export_blob {
    struct wl_list link, writers;
    struct exported_source *owner;
    struct wl_event_source *reader, *timeout;
    char *mime, *bytes;
    size_t size, capacity;
    int fd;
    bool ready, failed;
};
struct export_writer {
    struct wl_list link;
    struct export_blob *blob;
    struct wl_event_source *event, *timeout;
    size_t offset;
    int fd;
};
struct exported_source {
    struct wl_list blobs;
    size_t total_bytes;
    unsigned writer_count;
    struct cg_host_clipboard *bridge;
    struct wl_data_source *proxy;
    struct wlr_data_source *source;
    struct wl_listener destroy;
};
struct cg_host_clipboard {
    struct cg_server *server;
    struct wl_display *display;
    struct wl_surface *surface;
    struct wl_registry *registry;
    struct wl_seat *seat;
    struct wl_keyboard *keyboard;
    struct wl_pointer *pointer;
    struct wl_data_device_manager *manager;
    struct wl_data_device *device;
    struct wl_list offers;
    struct host_offer *current;
    struct imported_source *imported;
    struct exported_source *exported;
    struct wl_listener selection;
    uint32_t serial;
    bool focused;
    char marker[96];
};

static bool human_active(struct cg_host_clipboard *b) {
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    uint64_t now = (uint64_t)ts.tv_sec * 1000 + ts.tv_nsec / 1000000;
    return b->focused && now < b->server->seat->human_until_msec;
}
static void offer_unref(struct host_offer *o) {
    if (!o || --o->refs) return;
    char **mime;
    wl_array_for_each(mime, &o->mimes) free(*mime);
    wl_array_release(&o->mimes);
    wl_data_offer_destroy(o->proxy);
    wl_list_remove(&o->link);
    free(o);
}
static void import_send(struct wlr_data_source *source, const char *mime, int fd) {
    struct imported_source *s = wl_container_of(source, s, base);
    if (human_active(s->bridge)) {
        wl_data_offer_receive(s->offer->proxy, mime, fd);
        wl_display_flush(s->bridge->display);
    }
    close(fd);
}
static void import_destroy(struct wlr_data_source *source) {
    struct imported_source *s = wl_container_of(source, s, base);
    if (s->bridge->imported == s) s->bridge->imported = NULL;
    offer_unref(s->offer);
    free(s);
}
static const struct wlr_data_source_impl import_impl = {.send = import_send, .destroy = import_destroy};
static void import_current(struct cg_host_clipboard *b) {
    if (!b->current || !b->focused || b->imported) return;
    struct imported_source *s = calloc(1, sizeof(*s));
    if (!s) return;
    wlr_data_source_init(&s->base, &import_impl);
    s->bridge = b; s->offer = b->current; s->offer->refs++;
    char **mime;
    wl_array_for_each(mime, &s->offer->mimes) {
        char *copy = strdup(*mime);
        char **slot = copy ? wl_array_add(&s->base.mime_types, sizeof(char *)) : NULL;
        if (!slot) { free(copy); wlr_data_source_destroy(&s->base); return; }
        *slot = copy;
    }
    b->imported = s;
    wlr_seat_set_selection(b->server->seat->seat, &s->base,
        wl_display_next_serial(b->server->wl_display));
}
/* Snapshot a human copy while its application owns the selection. Agent
 * Unicode paste may subsequently replace the inner selection; the human's
 * external copy must remain usable. Never block the compositor on pipe I/O. */
#define EXPORT_MAX_BYTES (64U * 1024U * 1024U)
#define EXPORT_MAX_WRITERS 32
static void writer_free(struct export_writer *w) {
    if (w->event) wl_event_source_remove(w->event);
    if (w->timeout) wl_event_source_remove(w->timeout);
    close(w->fd); wl_list_remove(&w->link);
    w->blob->owner->writer_count--;
    free(w);
}
static int writer_timeout(void *data) { writer_free(data); return 0; }
static ssize_t safe_write(int fd, const void *bytes, size_t size) {
    sigset_t block, old, pending;
    sigemptyset(&block); sigaddset(&block, SIGPIPE);
    if (sigprocmask(SIG_BLOCK, &block, &old) < 0) return -1;
    sigpending(&pending);
    bool already = sigismember(&pending, SIGPIPE);
    ssize_t n = write(fd, bytes, size);
    int error = errno;
    if (n < 0 && error == EPIPE && !already) {
        struct timespec zero = {0}; sigtimedwait(&block, NULL, &zero);
    }
    sigprocmask(SIG_SETMASK, &old, NULL); errno = error;
    return n;
}
static int writer_ready(int fd, uint32_t mask, void *data) {
    struct export_writer *w = data;
    struct export_blob *blob = w->blob;
    if (blob->failed || (mask & (WL_EVENT_HANGUP | WL_EVENT_ERROR))) { writer_free(w); return 0; }
    ssize_t n = safe_write(fd, blob->bytes + w->offset, blob->size - w->offset);
    if (n < 0 && (errno == EAGAIN || errno == EINTR)) return 0;
    if (n < 0) { writer_free(w); return 0; }
    w->offset += (size_t)n;
    if (w->offset == blob->size) writer_free(w);
    return 0;
}
static void blob_complete(struct export_blob *blob, bool failed) {
    if (blob->reader) { wl_event_source_remove(blob->reader); blob->reader = NULL; }
    if (blob->timeout) { wl_event_source_remove(blob->timeout); blob->timeout = NULL; }
    if (blob->fd >= 0) { close(blob->fd); blob->fd = -1; }
    blob->ready = true; blob->failed = failed;
    struct export_writer *w, *tmp;
    wl_list_for_each_safe(w, tmp, &blob->writers, link) {
        if (failed || !blob->size) writer_free(w);
        else if (!w->event) {
            w->event = wl_event_loop_add_fd(wl_display_get_event_loop(blob->owner->bridge->server->wl_display),
                w->fd, WL_EVENT_WRITABLE, writer_ready, w);
            if (!w->event) writer_free(w);
        }
    }
}
static int reader_timeout(void *data) { blob_complete(data, true); return 0; }
static int reader_ready(int fd, uint32_t mask, void *data) {
    struct export_blob *blob = data;
    /* At most 64 KiB per dispatch, including large image/HTML clipboard types. */
    char bytes[65536];
    ssize_t n = read(fd, bytes, sizeof(bytes));
    if (n < 0 && (errno == EAGAIN || errno == EINTR)) return 0;
    if (n <= 0) { blob_complete(blob, n < 0); return 0; }
    if (blob->owner->total_bytes + (size_t)n > EXPORT_MAX_BYTES) { blob_complete(blob, true); return 0; }
    if (blob->size + (size_t)n > blob->capacity) {
        size_t capacity = blob->size + (size_t)n;
        char *allocation = realloc(blob->bytes, capacity);
        if (!allocation) { blob_complete(blob, true); return 0; }
        blob->bytes = allocation; blob->capacity = capacity;
    }
    memcpy(blob->bytes + blob->size, bytes, (size_t)n);
    blob->size += (size_t)n; blob->owner->total_bytes += (size_t)n;
    return 0;
}
static bool cache_mime(struct exported_source *s, const char *mime) {
    struct export_blob *blob;
    wl_list_for_each(blob, &s->blobs, link) if (!strcmp(blob->mime, mime)) return true;
    int pipefd[2];
    if (pipe2(pipefd, O_CLOEXEC) < 0) return false;
    if (fcntl(pipefd[0], F_SETFL, O_NONBLOCK) < 0) { close(pipefd[0]); close(pipefd[1]); return false; }
    blob = calloc(1, sizeof(*blob));
    if (!blob) { close(pipefd[0]); close(pipefd[1]); return false; }
    blob->mime = strdup(mime);
    if (!blob->mime) { close(pipefd[0]); close(pipefd[1]); free(blob); return false; }
    blob->owner = s; blob->fd = pipefd[0];
    wl_list_init(&blob->writers); wl_list_insert(&s->blobs, &blob->link);
    struct wl_event_loop *loop = wl_display_get_event_loop(s->bridge->server->wl_display);
    blob->reader = wl_event_loop_add_fd(loop, blob->fd, WL_EVENT_READABLE, reader_ready, blob);
    blob->timeout = wl_event_loop_add_timer(loop, reader_timeout, blob);
    if (!blob->reader || !blob->timeout) { close(pipefd[1]); blob_complete(blob, true); return false; }
    wl_event_source_timer_update(blob->timeout, 10000);
    wlr_data_source_send(s->source, mime, pipefd[1]);
    return true;
}
static void export_free(struct exported_source *s) {
    if (s->bridge->exported == s) s->bridge->exported = NULL;
    wl_list_remove(&s->destroy.link);
    wl_data_source_destroy(s->proxy);
    struct export_blob *blob, *tmp;
    wl_list_for_each_safe(blob, tmp, &s->blobs, link) {
        blob_complete(blob, true);
        wl_list_remove(&blob->link);
        free(blob->bytes); free(blob->mime); free(blob);
    }
    free(s);
}
static void export_source_destroyed(struct wl_listener *listener, void *data) {
    struct exported_source *s = wl_container_of(listener, s, destroy);
    wl_list_remove(&s->destroy.link); wl_list_init(&s->destroy.link);
    s->source = NULL; /* Completed copies survive an Agent's private paste. */
}
static void export_target(void *data, struct wl_data_source *source, const char *mime) {}
static void export_send(void *data, struct wl_data_source *source, const char *mime, int32_t fd) {
    struct exported_source *s = data;
    struct export_blob *blob;
    wl_list_for_each(blob, &s->blobs, link) {
        if (strcmp(blob->mime, mime)) continue;
        if (blob->failed || (blob->ready && !blob->size) || s->writer_count >= EXPORT_MAX_WRITERS) break;
        int flags = fcntl(fd, F_GETFL);
        if (flags < 0 || fcntl(fd, F_SETFL, flags | O_NONBLOCK) < 0) break;
        struct export_writer *w = calloc(1, sizeof(*w));
        if (!w) break;
        w->blob = blob; w->fd = fd;
        wl_list_insert(&blob->writers, &w->link); s->writer_count++;
        w->timeout = wl_event_loop_add_timer(wl_display_get_event_loop(s->bridge->server->wl_display), writer_timeout, w);
        if (!w->timeout) { writer_free(w); return; }
        wl_event_source_timer_update(w->timeout, 10000);
        if (blob->ready) blob_complete(blob, false);
        return;
    }
    close(fd);
}
static void export_cancel(void *data, struct wl_data_source *source) { export_free(data); }
static const struct wl_data_source_listener source_listener = {
    .target = export_target, .send = export_send, .cancelled = export_cancel,
};
static void selection_changed(struct wl_listener *listener, void *data) {
    struct cg_host_clipboard *b = wl_container_of(listener, b, selection);
    struct wlr_data_source *source = b->server->seat->seat->selection_source;
    if (!b->device || !b->serial || !source || source->impl == &import_impl ||
        b->server->seat->clipboard || !human_active(b)) return;
    if (b->exported) export_free(b->exported);
    struct exported_source *s = calloc(1, sizeof(*s));
    if (!s) return;
    s->bridge = b; s->source = source;
    wl_list_init(&s->blobs);
    s->proxy = wl_data_device_manager_create_data_source(b->manager);
    if (!s->proxy) { free(s); return; }
    b->exported = s;
    s->destroy.notify = export_source_destroyed;
    wl_signal_add(&source->events.destroy, &s->destroy);
    wl_data_source_add_listener(s->proxy, &source_listener, s);
    char **mime;
    unsigned count = 0;
    wl_array_for_each(mime, &source->mime_types) {
        if (++count > 64) break;
        if (cache_mime(s, *mime)) wl_data_source_offer(s->proxy, *mime);
    }
    wl_data_source_offer(s->proxy, b->marker);
    wl_data_device_set_selection(b->device, s->proxy, b->serial);
    wl_display_flush(b->display);
}
static void offer_mime(void *data, struct wl_data_offer *proxy, const char *mime) {
    struct host_offer *o = data;
    if (o->mimes.size >= 128 * sizeof(char *) || strlen(mime) > 1024) return;
    char *copy = strdup(mime);
    char **slot = copy ? wl_array_add(&o->mimes, sizeof(char *)) : NULL;
    if (slot) *slot = copy; else free(copy);
}
static const struct wl_data_offer_listener offer_listener = {.offer = offer_mime};
static void data_offer(void *data, struct wl_data_device *device, struct wl_data_offer *proxy) {
    struct cg_host_clipboard *b = data;
    struct host_offer *o = calloc(1, sizeof(*o));
    if (!o) { wl_data_offer_destroy(proxy); return; }
    o->proxy = proxy; o->refs = 1; o->pending = true;
    wl_array_init(&o->mimes);
    wl_list_insert(&b->offers, &o->link);
    wl_data_offer_add_listener(proxy, &offer_listener, o);
}
static void data_selection(void *data, struct wl_data_device *device, struct wl_data_offer *proxy) {
    struct cg_host_clipboard *b = data;
    struct host_offer *o = proxy ? wl_data_offer_get_user_data(proxy) : NULL;
    if (o) o->refs++; /* Keep this callback safe if it replaces the same offer. */
    bool own = false;
    if (o) {
        char **mime;
        wl_array_for_each(mime, &o->mimes) if (!strcmp(*mime, b->marker)) own = true;
    }
    if (!own) {
        if (o) o->refs++;
        if (b->imported) {
            wlr_seat_set_selection(b->server->seat->seat, NULL,
                wl_display_next_serial(b->server->wl_display));
        }
        offer_unref(b->current);
        b->current = o;
        import_current(b);
    } else {
        offer_unref(b->current);
        b->current = NULL;
    }
    /* Release the protocol event's reference; the selection/import owns theirs. */
    if (o && o->pending) { o->pending = false; offer_unref(o); }
    offer_unref(o);
}
static void data_enter(void *data, struct wl_data_device *device, uint32_t serial,
    struct wl_surface *surface, wl_fixed_t x, wl_fixed_t y, struct wl_data_offer *offer) {
    /* Drag and drop is a separate protocol and is not a clipboard selection. */
    if (offer) offer_unref(wl_data_offer_get_user_data(offer));
}
static void data_leave(void *data, struct wl_data_device *device) {}
static void data_motion(void *data, struct wl_data_device *device, uint32_t time, wl_fixed_t x, wl_fixed_t y) {}
static void data_drop(void *data, struct wl_data_device *device) {}
static const struct wl_data_device_listener device_listener = {
    .data_offer = data_offer, .selection = data_selection, .enter = data_enter,
    .leave = data_leave, .motion = data_motion, .drop = data_drop,
};
static void keymap(void *data, struct wl_keyboard *keyboard, uint32_t format, int fd, uint32_t size) { close(fd); }
static void key_enter(void *data, struct wl_keyboard *keyboard, uint32_t serial,
    struct wl_surface *surface, struct wl_array *keys) {
    struct cg_host_clipboard *b = data;
    b->focused = surface == b->surface;
    b->serial = serial;
    import_current(b);
}
static void key_leave(void *data, struct wl_keyboard *keyboard, uint32_t serial, struct wl_surface *surface) {
    struct cg_host_clipboard *b = data;
    b->focused = false; b->serial = 0;
}
static void key(void *data, struct wl_keyboard *keyboard, uint32_t serial, uint32_t time,
    uint32_t key, uint32_t state) {
    struct cg_host_clipboard *b = data;
    b->serial = serial;
    /* A private Agent paste may have replaced the lazy parent offer. Restore
     * it on the next physical key before the application processes Ctrl+V. */
    if (!b->exported) import_current(b);
}
static void modifiers(void *data, struct wl_keyboard *keyboard, uint32_t serial,
    uint32_t depressed, uint32_t latched, uint32_t locked, uint32_t group) {
    ((struct cg_host_clipboard *)data)->serial = serial;
}
static const struct wl_keyboard_listener keyboard_listener = {
    .keymap = keymap, .enter = key_enter, .leave = key_leave, .key = key, .modifiers = modifiers,
};
static void pointer_enter(void *data, struct wl_pointer *pointer, uint32_t serial,
    struct wl_surface *surface, wl_fixed_t x, wl_fixed_t y) { ((struct cg_host_clipboard *)data)->serial = serial; }
static void pointer_leave(void *data, struct wl_pointer *pointer, uint32_t serial, struct wl_surface *surface) {}
static void pointer_motion(void *data, struct wl_pointer *pointer, uint32_t time, wl_fixed_t x, wl_fixed_t y) {}
static void pointer_button(void *data, struct wl_pointer *pointer, uint32_t serial,
    uint32_t time, uint32_t button, uint32_t state) {
    struct cg_host_clipboard *b = data;
    b->serial = serial;
    if (!b->exported) import_current(b);
}
static void pointer_axis(void *data, struct wl_pointer *pointer, uint32_t time, uint32_t axis, wl_fixed_t value) {}
static const struct wl_pointer_listener pointer_listener = {
    .enter = pointer_enter, .leave = pointer_leave, .motion = pointer_motion,
    .button = pointer_button, .axis = pointer_axis,
};
static void capabilities(void *data, struct wl_seat *seat, uint32_t caps) {
    struct cg_host_clipboard *b = data;
    if ((caps & WL_SEAT_CAPABILITY_KEYBOARD) && !b->keyboard) {
        b->keyboard = wl_seat_get_keyboard(seat);
        wl_keyboard_add_listener(b->keyboard, &keyboard_listener, b);
    }
    if ((caps & WL_SEAT_CAPABILITY_POINTER) && !b->pointer) {
        b->pointer = wl_seat_get_pointer(seat);
        wl_pointer_add_listener(b->pointer, &pointer_listener, b);
    }
}
static const struct wl_seat_listener seat_listener = {.capabilities = capabilities};
static void global(void *data, struct wl_registry *registry, uint32_t name, const char *interface, uint32_t version) {
    struct cg_host_clipboard *b = data;
    if (!strcmp(interface, wl_seat_interface.name) && !b->seat) {
        b->seat = wl_registry_bind(registry, name, &wl_seat_interface, 1);
        wl_seat_add_listener(b->seat, &seat_listener, b);
    } else if (!strcmp(interface, wl_data_device_manager_interface.name) && !b->manager) {
        b->manager = wl_registry_bind(registry, name, &wl_data_device_manager_interface, 1);
    }
}
static void global_remove(void *data, struct wl_registry *registry, uint32_t name) {}
static const struct wl_registry_listener registry_listener = {.global = global, .global_remove = global_remove};
struct cg_host_clipboard *host_clipboard_create(struct cg_server *server, struct wl_display *display,
    struct wl_surface *surface) {
    struct cg_host_clipboard *b = calloc(1, sizeof(*b));
    if (!b) return NULL;
    b->server = server; b->display = display; b->surface = surface;
    wl_list_init(&b->offers);
    struct timespec now;
    clock_gettime(CLOCK_MONOTONIC, &now);
    snprintf(b->marker, sizeof(b->marker), "application/x-agentseat-clipboard-%ld-%lx-%lx",
        (long)getpid(), (unsigned long)now.tv_sec, (unsigned long)now.tv_nsec);
    b->selection.notify = selection_changed;
    wl_signal_add(&server->seat->seat->events.set_selection, &b->selection);
    b->registry = wl_display_get_registry(display);
    wl_registry_add_listener(b->registry, &registry_listener, b);
    if (wl_display_roundtrip(display) < 0) { host_clipboard_destroy(b); return NULL; }
    if (b->manager && b->seat) {
        b->device = wl_data_device_manager_get_data_device(b->manager, b->seat);
        wl_data_device_add_listener(b->device, &device_listener, b);
    }
    return b;
}
void host_clipboard_destroy(struct cg_host_clipboard *b) {
    if (!b) return;
    wl_list_remove(&b->selection.link);
    if (b->exported) export_free(b->exported);
    if (b->imported) wlr_seat_set_selection(b->server->seat->seat, NULL,
        wl_display_next_serial(b->server->wl_display));
    offer_unref(b->current);
    struct host_offer *o, *tmp;
    wl_list_for_each_safe(o, tmp, &b->offers, link) offer_unref(o);
    if (b->device) wl_data_device_destroy(b->device);
    if (b->pointer) wl_pointer_destroy(b->pointer);
    if (b->keyboard) wl_keyboard_destroy(b->keyboard);
    if (b->seat) wl_seat_destroy(b->seat);
    if (b->manager) wl_data_device_manager_destroy(b->manager);
    if (b->registry) wl_registry_destroy(b->registry);
    free(b);
}
