#define _GNU_SOURCE
#include "agentseat-capture.h"
#include "agentseat-control-v1-client-protocol.h"
#include "wlr-screencopy-unstable-v1-client-protocol.h"
#include <wayland-client.h>
#include <errno.h>
#include <stdbool.h>
#include <fcntl.h>
#include <limits.h>
#include <png.h>
#include <pthread.h>
#include <stdatomic.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>
#include <sys/eventfd.h>
#include <sys/stat.h>
#include <time.h>
#include <unistd.h>

#define RETAINED 8
#define MAX_BYTES (64U * 1024U * 1024U)
struct receipt {
    char id[64], path[PATH_MAX], status[32], error[128];
    uint32_t width, height, geometry_version, content_serial;
    uint64_t submitted_ns, presented_ns;
};
struct as_capture {
    struct wl_display *display;
    struct wl_registry *registry;
    struct wl_shm *shm;
    struct wl_output *output;
    struct agentseat_control_v1 *control;
    struct zwlr_screencopy_manager_v1 *manager;
    struct zwlr_screencopy_frame_v1 *frame;
    struct wl_buffer *buffer;
    void *pixels;
    size_t bytes;
    uint32_t format, stride, flags;
    struct receipt receipts[RETAINED], *active;
    uint64_t sequence, deadline;
    int completion_fd;
    pthread_t worker;
    bool worker_running;
    atomic_bool worker_done;
};
static uint64_t now_ns(void) {
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return (uint64_t)ts.tv_sec * 1000000000U + ts.tv_nsec;
}
static void cleanup(struct as_capture *c) {
    if (c->frame) zwlr_screencopy_frame_v1_destroy(c->frame);
    if (c->buffer) wl_buffer_destroy(c->buffer);
    if (c->pixels) munmap(c->pixels, c->bytes);
    c->frame = NULL; c->buffer = NULL; c->pixels = NULL; c->bytes = 0;
    c->active = NULL; c->deadline = 0;
}
static void fail(struct as_capture *c, const char *message) {
    if (c->active) {
        snprintf(c->active->status, sizeof(c->active->status), "failed");
        snprintf(c->active->error, sizeof(c->active->error), "%s", message);
    }
    cleanup(c);
}
static void *encode(void *data) {
    struct as_capture *c = data;
    struct receipt *r = c->active;
    int fd = open(r->path, O_WRONLY | O_CREAT | O_EXCL | O_CLOEXEC | O_NOFOLLOW, 0600);
    FILE *file = fd < 0 ? NULL : fdopen(fd, "wb");
    if (fd >= 0 && !file) close(fd);
    size_t rgb_bytes = (size_t)r->width * 3 * r->height;
    unsigned char *rgb = mmap(NULL, rgb_bytes, PROT_READ | PROT_WRITE, MAP_PRIVATE | MAP_ANONYMOUS, -1, 0);
    if (rgb == MAP_FAILED) rgb = NULL;
    bool success = false;
    if (file && rgb) {
        for (uint32_t y = 0; y < r->height; ++y) {
            uint32_t source_y = c->flags & 1 ? r->height - 1 - y : y;
            const uint32_t *row = (const uint32_t *)((char *)c->pixels + (size_t)source_y * c->stride);
            for (uint32_t x = 0; x < r->width; ++x) {
                uint32_t pixel = row[x];
                unsigned char *out = rgb + ((size_t)y * r->width + x) * 3;
                bool bgr = c->format == WL_SHM_FORMAT_XBGR8888 || c->format == WL_SHM_FORMAT_ABGR8888;
                out[0] = bgr ? pixel : pixel >> 16;
                out[1] = pixel >> 8;
                out[2] = bgr ? pixel >> 16 : pixel;
            }
        }
        png_image image = {.version = PNG_IMAGE_VERSION, .width = r->width,
                          .height = r->height, .format = PNG_FORMAT_RGB, .flags = PNG_IMAGE_FLAG_FAST};
        success = png_image_write_to_stdio(&image, file, 0, rgb, 0, NULL);
        png_image_free(&image);
    }
    if (rgb) munmap(rgb, rgb_bytes);
    if (file && fclose(file)) success = false;
    if (!success) unlink(r->path);
    snprintf(r->status, sizeof(r->status), "%s", success ? "finished" : "failed");
    if (!success) snprintf(r->error, sizeof(r->error), "PNG encoding or file write failed");
    atomic_store_explicit(&c->worker_done, true, memory_order_release);
    uint64_t done = 1;
    (void)write(c->completion_fd, &done, sizeof(done));
    return NULL;
}
static void buffer_event(void *data, struct zwlr_screencopy_frame_v1 *frame,
    uint32_t format, uint32_t width, uint32_t height, uint32_t stride) {
    struct as_capture *c = data;
    if (!width || !height || width > 8192 || height > 8192 || stride < width * 4 ||
        (uint64_t)stride * height > MAX_BYTES ||
        (format != WL_SHM_FORMAT_XRGB8888 && format != WL_SHM_FORMAT_ARGB8888 &&
         format != WL_SHM_FORMAT_XBGR8888 && format != WL_SHM_FORMAT_ABGR8888)) {
        fail(c, "Unsupported frame format or pixel budget exceeded"); return;
    }
    c->format = format; c->stride = stride; c->bytes = (size_t)stride * height;
    c->active->width = width; c->active->height = height;
    int fd = memfd_create("agentseat-frame", MFD_CLOEXEC);
    if (fd < 0) { fail(c, "Cannot allocate frame"); return; }
    if (ftruncate(fd, c->bytes)) { close(fd); fail(c, "Cannot size frame"); return; }
    c->pixels = mmap(NULL, c->bytes, PROT_READ | PROT_WRITE, MAP_SHARED, fd, 0);
    if (c->pixels == MAP_FAILED) {
        c->pixels = NULL; close(fd); fail(c, "Cannot map frame"); return;
    }
    struct wl_shm_pool *pool = wl_shm_create_pool(c->shm, fd, (int)c->bytes);
    close(fd);
    c->buffer = wl_shm_pool_create_buffer(pool, 0, width, height, stride, format);
    wl_shm_pool_destroy(pool);
    zwlr_screencopy_frame_v1_copy(frame, c->buffer);
    // Same connection: the copy is registered before requesting a render.
    agentseat_control_v1_render_once(c->control);
    wl_display_flush(c->display);
}
static void flags_event(void *data, struct zwlr_screencopy_frame_v1 *frame, uint32_t flags) {
    (void)frame; ((struct as_capture *)data)->flags = flags;
}
static void ready_event(void *data, struct zwlr_screencopy_frame_v1 *frame,
    uint32_t hi, uint32_t lo, uint32_t ns) {
    struct as_capture *c = data;
    c->active->presented_ns = (((uint64_t)hi << 32) | lo) * 1000000000U + ns;
    zwlr_screencopy_frame_v1_destroy(frame); c->frame = NULL;
    wl_buffer_destroy(c->buffer); c->buffer = NULL;
    atomic_store(&c->worker_done, false);
    if (pthread_create(&c->worker, NULL, encode, c)) { fail(c, "Cannot start PNG worker"); return; }
    c->worker_running = true;
    c->deadline = 0;
}
static void failed_event(void *data, struct zwlr_screencopy_frame_v1 *frame) {
    (void)frame; fail(data, "Compositor rejected frame copy");
}
static const struct zwlr_screencopy_frame_v1_listener frame_listener = {
    .buffer = buffer_event, .flags = flags_event, .ready = ready_event, .failed = failed_event,
};
static void global(void *data, struct wl_registry *registry, uint32_t id, const char *name, uint32_t version) {
    (void)version;
    struct as_capture *c = data;
    if (!strcmp(name, wl_shm_interface.name) && !c->shm)
        c->shm = wl_registry_bind(registry, id, &wl_shm_interface, 1);
    else if (!strcmp(name, wl_output_interface.name) && !c->output)
        c->output = wl_registry_bind(registry, id, &wl_output_interface, 1);
    else if (!strcmp(name, zwlr_screencopy_manager_v1_interface.name) && !c->manager)
        c->manager = wl_registry_bind(registry, id, &zwlr_screencopy_manager_v1_interface, 1);
}
static void removed(void *data, struct wl_registry *registry, uint32_t id) {
    (void)data; (void)registry; (void)id;
}
static const struct wl_registry_listener registry_listener = {.global = global, .global_remove = removed};
struct as_capture *as_capture_create(struct wl_display *display) {
    struct as_capture *c = calloc(1, sizeof(*c));
    if (!c) return NULL;
    c->completion_fd = eventfd(0, EFD_NONBLOCK | EFD_CLOEXEC);
    if (c->completion_fd < 0) { free(c); return NULL; }
    c->display = display;
    c->registry = wl_display_get_registry(display);
    wl_registry_add_listener(c->registry, &registry_listener, c);
    return c;
}
void as_capture_tick(struct as_capture *c) {
    if (!c) return;
    uint64_t done;
    (void)read(c->completion_fd, &done, sizeof(done));
    if (c->worker_running && atomic_load_explicit(&c->worker_done, memory_order_acquire)) {
        pthread_join(c->worker, NULL); c->worker_running = false; cleanup(c);
    } else if (c->deadline && now_ns() / 1000000U >= c->deadline) fail(c, "Frame deadline expired");
}
uint64_t as_capture_deadline(struct as_capture *c) { return c ? c->deadline : 0; }
static json_object *receipt_json(struct as_capture *c, struct receipt *r) {
    json_object *out = json_object_new_object();
    bool encoding = c->worker_running && c->active == r;
    json_object_object_add(out, "frame_id", json_object_new_string(r->id));
    json_object_object_add(out, "status", json_object_new_string(encoding ? "encoding" : r->status));
    if (!encoding) {
        json_object_object_add(out, "path", json_object_new_string(r->path));
        json_object_object_add(out, "error", r->error[0] ? json_object_new_string(r->error) : NULL);
    }
    json_object_object_add(out, "width", json_object_new_int64(r->width));
    json_object_object_add(out, "height", json_object_new_int64(r->height));
    json_object_object_add(out, "geometry_version", json_object_new_int64(r->geometry_version));
    json_object_object_add(out, "content_serial", json_object_new_int64(r->content_serial));
    json_object_object_add(out, "submitted_ns", json_object_new_int64(r->submitted_ns));
    json_object_object_add(out, "presented_ns", json_object_new_int64(r->presented_ns));
    return out;
}
json_object *as_capture_start(struct as_capture *c, struct agentseat_control_v1 *control,
    const char *socket_path, uint32_t geometry_version, uint32_t content_serial) {
    as_capture_tick(c);
    if (!c || c->active || !c->manager || !c->shm || !c->output) return NULL;
    struct receipt *r = &c->receipts[c->sequence++ % RETAINED];
    if (r->path[0]) unlink(r->path);
    memset(r, 0, sizeof(*r));
    r->submitted_ns = now_ns(); r->geometry_version = geometry_version; r->content_serial = content_serial;
    snprintf(r->id, sizeof(r->id), "f-%llu-%llu", (unsigned long long)r->submitted_ns, (unsigned long long)c->sequence);
    const char *slash = strrchr(socket_path, '/');
    if (!slash || (size_t)(slash - socket_path) + strlen(r->id) + 8 >= sizeof(r->path)) return NULL;
    snprintf(r->path, sizeof(r->path), "%.*s/%s.png", (int)(slash - socket_path), socket_path, r->id);
    snprintf(r->status, sizeof(r->status), "pending");
    c->active = r; c->control = control; c->flags = 0;
    c->deadline = now_ns() / 1000000U + 3000;
    c->frame = zwlr_screencopy_manager_v1_capture_output(c->manager, 1, c->output);
    zwlr_screencopy_frame_v1_add_listener(c->frame, &frame_listener, c);
    wl_display_flush(c->display);
    return receipt_json(c, r);
}
json_object *as_capture_get(struct as_capture *c, const char *id) {
    as_capture_tick(c);
    for (size_t i = 0; c && i < RETAINED; ++i)
        if (!strcmp(c->receipts[i].id, id)) return receipt_json(c, &c->receipts[i]);
    return NULL;
}
void as_capture_destroy(struct as_capture *c) {
    if (!c) return;
    if (c->worker_running) pthread_join(c->worker, NULL);
    cleanup(c);
    for (size_t i = 0; i < RETAINED; ++i) if (c->receipts[i].path[0]) unlink(c->receipts[i].path);
    if (c->manager) zwlr_screencopy_manager_v1_destroy(c->manager);
    if (c->shm) wl_shm_destroy(c->shm);
    if (c->output) wl_output_destroy(c->output);
    wl_registry_destroy(c->registry);
    close(c->completion_fd);
    free(c);
}

int as_capture_fd(struct as_capture *c) { return c ? c->completion_fd : -1; }
