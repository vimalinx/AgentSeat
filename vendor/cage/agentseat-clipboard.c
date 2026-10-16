#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <signal.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <unistd.h>
#include <wayland-server-core.h>
#include <wlr/types/wlr_data_device.h>
#include "agentseat-control-v1-server-protocol.h"
#include "agentseat-clipboard.h"
#include "seat.h"

#define CLIP_BYTES 32768
#define CLIP_READS 64
#define CLIP_WRITERS 16
struct cg_clipboard {
    struct wlr_data_source base;
    struct cg_seat *seat;
    struct wl_event_source *timer;
    struct wl_list writers;
    uint32_t token, epoch, reads, active;
    size_t size;
    char text[CLIP_BYTES];
};
struct clip_writer {
    struct wl_list link;
    struct cg_clipboard *clip;
    struct wl_event_source *event;
    int fd;
    size_t offset;
};
static bool valid_utf8(const unsigned char *text, size_t size) {
    for (size_t i = 0; i < size;) {
        uint32_t value = text[i++];
        if (value < 0x80) { if (!value) return false; continue; }
        unsigned need; uint32_t minimum;
        if (value >= 0xc2 && value <= 0xdf) { need = 1; minimum = 0x80; value &= 0x1f; }
        else if (value >= 0xe0 && value <= 0xef) { need = 2; minimum = 0x800; value &= 0x0f; }
        else if (value >= 0xf0 && value <= 0xf4) { need = 3; minimum = 0x10000; value &= 7; }
        else return false;
        if (i + need > size) return false;
        for (unsigned n = 0; n < need; n++) {
            if ((text[i] & 0xc0) != 0x80) return false;
            value = (value << 6) | (text[i++] & 0x3f);
        }
        if (value < minimum || value > 0x10ffff || (value >= 0xd800 && value <= 0xdfff)) return false;
    }
    return true;
}
static void writer_destroy(struct clip_writer *writer) {
    if (writer->event) wl_event_source_remove(writer->event);
    close(writer->fd);
    wl_list_remove(&writer->link);
    writer->clip->active--;
    free(writer);
}
/* A broken recipient must not signal the compositor or change inherited
 * signal disposition for applications. Consume only our newly raised SIGPIPE. */
static ssize_t pipe_write(int fd, const void *bytes, size_t length) {
    sigset_t block, old, pending;
    sigemptyset(&block); sigaddset(&block, SIGPIPE);
    if (sigprocmask(SIG_BLOCK, &block, &old) < 0) return -1;
    sigpending(&pending);
    bool already_pending = sigismember(&pending, SIGPIPE);
    ssize_t result = write(fd, bytes, length);
    int saved_errno = errno;
    if (result < 0 && saved_errno == EPIPE && !already_pending) {
        struct timespec zero = {0};
        sigtimedwait(&block, NULL, &zero);
    }
    sigprocmask(SIG_SETMASK, &old, NULL);
    errno = saved_errno;
    return result;
}
static int writer_ready(int fd, uint32_t mask, void *data) {
    struct clip_writer *writer = data;
    struct cg_clipboard *clip = writer->clip;
    if (mask & (WL_EVENT_HANGUP | WL_EVENT_ERROR)) { writer_destroy(writer); return 0; }
    ssize_t count = pipe_write(fd, clip->text + writer->offset, clip->size - writer->offset);
    if (count < 0 && (errno == EAGAIN || errno == EINTR)) return 0;
    if (count < 0) { writer_destroy(writer); return 0; }
    writer->offset += (size_t)count;
    if (writer->offset == clip->size) {
        if (clip->seat->control_resource)
            agentseat_control_v1_send_clipboard_transfer(clip->seat->control_resource, clip->token, clip->size);
        writer_destroy(writer);
    }
    return 0;
}
static void source_send(struct wlr_data_source *source, const char *mime, int32_t fd) {
    struct cg_clipboard *clip = wl_container_of(source, clip, base);
    struct stat stat;
    bool supported = !strcmp(mime, "text/plain;charset=utf-8") || !strcmp(mime, "text/plain") || !strcmp(mime, "UTF8_STRING");
    if (!supported || !clip->seat->control_allowed || clip->epoch != clip->seat->control_epoch ||
        ++clip->reads > CLIP_READS || clip->active >= CLIP_WRITERS || fstat(fd, &stat) ||
        !(S_ISFIFO(stat.st_mode) || S_ISSOCK(stat.st_mode))) { close(fd); return; }
    int flags = fcntl(fd, F_GETFL);
    if (flags < 0 || fcntl(fd, F_SETFL, flags | O_NONBLOCK) < 0) { close(fd); return; }
    struct clip_writer *writer = calloc(1, sizeof(*writer));
    if (!writer) { close(fd); return; }
    writer->clip = clip; writer->fd = fd;
    wl_list_insert(&clip->writers, &writer->link); clip->active++;
    writer->event = wl_event_loop_add_fd(wl_display_get_event_loop(clip->seat->server->wl_display),
        fd, WL_EVENT_WRITABLE, writer_ready, writer);
    if (!writer->event) writer_destroy(writer);
}
static void source_destroy(struct wlr_data_source *source) {
    struct cg_clipboard *clip = wl_container_of(source, clip, base);
    if (clip->seat->clipboard == clip) clip->seat->clipboard = NULL;
    if (clip->timer) wl_event_source_remove(clip->timer);
    struct clip_writer *writer, *tmp;
    wl_list_for_each_safe(writer, tmp, &clip->writers, link) writer_destroy(writer);
    memset(clip->text, 0, sizeof(clip->text));
    free(clip);
}
static const struct wlr_data_source_impl implementation = {.send = source_send, .destroy = source_destroy};
void cg_clipboard_clear(struct cg_seat *seat) {
    if (!seat->clipboard) return;
    struct cg_clipboard *clip = seat->clipboard;
    if (seat->seat->selection_source == &clip->base)
        wlr_seat_set_selection(seat->seat, NULL, wl_display_next_serial(seat->server->wl_display));
    else
        wlr_data_source_destroy(&clip->base);
}
static int expire(void *data) { cg_clipboard_clear(data); return 0; }
void cg_clipboard_offer(struct cg_seat *seat, struct wl_resource *resource,
    uint32_t epoch, uint32_t token, int fd, uint32_t size) {
    struct stat stat;
    int seals = fcntl(fd, F_GET_SEALS);
    int needed = F_SEAL_WRITE | F_SEAL_GROW | F_SEAL_SHRINK;
    if (!seat->control_allowed || epoch != seat->control_epoch || !size || size > CLIP_BYTES ||
        seals < 0 || (seals & needed) != needed || fstat(fd, &stat) ||
        !S_ISREG(stat.st_mode) || stat.st_size != size) {
        close(fd); agentseat_control_v1_send_clipboard_offered(resource, token, 1); return;
    }
    struct cg_clipboard *clip = calloc(1, sizeof(*clip));
    if (!clip) { close(fd); agentseat_control_v1_send_clipboard_offered(resource, token, 2); return; }
    ssize_t got = pread(fd, clip->text, size, 0);
    close(fd);
    if (got != size || !valid_utf8((const unsigned char*)clip->text, size)) {
        free(clip); agentseat_control_v1_send_clipboard_offered(resource, token, 1); return;
    }
    clip->seat = seat; clip->token = token; clip->epoch = epoch; clip->size = size;
    wl_list_init(&clip->writers);
    wlr_data_source_init(&clip->base, &implementation);
    const char *types[] = {"text/plain;charset=utf-8", "text/plain", "UTF8_STRING"};
    for (size_t i = 0; i < 3; i++) {
        char **slot = wl_array_add(&clip->base.mime_types, sizeof(char *));
        if (!slot || !(*slot = strdup(types[i]))) {
            wlr_data_source_destroy(&clip->base);
            agentseat_control_v1_send_clipboard_offered(resource, token, 2); return;
        }
    }
    clip->timer = wl_event_loop_add_timer(wl_display_get_event_loop(seat->server->wl_display), expire, seat);
    if (!clip->timer) { wlr_data_source_destroy(&clip->base); agentseat_control_v1_send_clipboard_offered(resource, token, 2); return; }
    cg_clipboard_clear(seat);
    seat->clipboard = clip;
    wlr_seat_set_selection(seat->seat, &clip->base, wl_display_next_serial(seat->server->wl_display));
    wl_event_source_timer_update(clip->timer, 10000);
    agentseat_control_v1_send_clipboard_offered(resource, token, 0);
}
