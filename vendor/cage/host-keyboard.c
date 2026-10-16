/* wlroots' nested backend forwards raw keys but does not import the parent's
 * keymap/repeat settings. Read those standard wl_keyboard events explicitly. */
#define _POSIX_C_SOURCE 200809L
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>
#include <sys/stat.h>
#include <unistd.h>
#include <wayland-client.h>
#include <xkbcommon/xkbcommon.h>
#include "host-keyboard.h"
#include "seat.h"
struct cg_host_keyboard {
    struct cg_seat *inner;
    struct wl_registry *registry;
    struct wl_seat *seat;
    struct wl_keyboard *keyboard;
};
static void keymap(void *data, struct wl_keyboard *keyboard, uint32_t format, int fd, uint32_t size) {
    struct cg_host_keyboard *b = data;
    struct stat st;
    if (format != WL_KEYBOARD_KEYMAP_FORMAT_XKB_V1 || size < 1 || size > 16*1024*1024 ||
        fstat(fd,&st)<0 || st.st_size < size) { close(fd); return; }
    char *text = mmap(NULL,size,PROT_READ,MAP_PRIVATE,fd,0); close(fd);
    if (text == MAP_FAILED) return;
    if (text[size-1] == '\0') {
        struct xkb_context *context = xkb_context_new(XKB_CONTEXT_NO_FLAGS);
        if (context) {
            struct xkb_keymap *map = xkb_keymap_new_from_string(context,text,XKB_KEYMAP_FORMAT_TEXT_V1,XKB_KEYMAP_COMPILE_NO_FLAGS);
            if (map) { seat_set_parent_keymap(b->inner,map); xkb_keymap_unref(map); }
            xkb_context_unref(context);
        }
    }
    munmap(text,size);
}
static void repeat(void *data, struct wl_keyboard *keyboard, int32_t rate, int32_t delay) {
    seat_set_parent_repeat(((struct cg_host_keyboard *)data)->inner,rate,delay);
}
static void enter(void *d,struct wl_keyboard *k,uint32_t serial,struct wl_surface *s,struct wl_array *keys) {}
static void leave(void *d,struct wl_keyboard *k,uint32_t serial,struct wl_surface *s) {}
static void key(void *d,struct wl_keyboard *k,uint32_t serial,uint32_t time,uint32_t key,uint32_t state) {}
static void modifiers(void *d,struct wl_keyboard *k,uint32_t serial,uint32_t dep,uint32_t lat,uint32_t lock,uint32_t group) {}
static const struct wl_keyboard_listener keyboard_listener = {
    .keymap=keymap,.repeat_info=repeat,.enter=enter,.leave=leave,.key=key,.modifiers=modifiers,
};
static void capabilities(void *data,struct wl_seat *seat,uint32_t caps) {
    struct cg_host_keyboard *b = data;
    if ((caps & WL_SEAT_CAPABILITY_KEYBOARD) && !b->keyboard) {
        b->keyboard=wl_seat_get_keyboard(seat);
        wl_keyboard_add_listener(b->keyboard,&keyboard_listener,b);
    } else if (!(caps & WL_SEAT_CAPABILITY_KEYBOARD) && b->keyboard) {
        if (wl_keyboard_get_version(b->keyboard)>=3) wl_keyboard_release(b->keyboard);
        else wl_keyboard_destroy(b->keyboard);
        b->keyboard=NULL;
    }
}
static void seat_name(void *d,struct wl_seat *s,const char *name) {}
static const struct wl_seat_listener seat_listener = {.capabilities=capabilities,.name=seat_name};
static void global(void *data,struct wl_registry *r,uint32_t name,const char *interface,uint32_t version) {
    struct cg_host_keyboard *b = data;
    if (!strcmp(interface,"wl_seat") && !b->seat) {
        b->seat=wl_registry_bind(r,name,&wl_seat_interface,version<4?version:4);
        wl_seat_add_listener(b->seat,&seat_listener,b);
    }
}
static void global_remove(void *d,struct wl_registry *r,uint32_t name) {}
static const struct wl_registry_listener registry_listener = {.global=global,.global_remove=global_remove};
struct cg_host_keyboard *host_keyboard_create(struct cg_seat *inner,struct wl_display *display) {
    struct cg_host_keyboard *b=calloc(1,sizeof(*b)); if(!b)return NULL;
    b->inner=inner;b->registry=wl_display_get_registry(display);
    wl_registry_add_listener(b->registry,&registry_listener,b);
    if(wl_display_roundtrip(display)<0){host_keyboard_destroy(b);return NULL;}
    return b;
}
void host_keyboard_destroy(struct cg_host_keyboard *b) {
    if(!b)return;
    if(b->keyboard) {
        if(wl_keyboard_get_version(b->keyboard)>=3)wl_keyboard_release(b->keyboard);
        else wl_keyboard_destroy(b->keyboard);
    }
    if(b->seat)wl_seat_destroy(b->seat);
    if(b->registry)wl_registry_destroy(b->registry);
    free(b);
}
