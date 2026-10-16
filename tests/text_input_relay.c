/* Real client/server protocol test. Both displays and all text are disposable;
 * no connection to the user's compositor, IME or clipboard is made. */
#define _POSIX_C_SOURCE 200809L
#include <assert.h>
#include <poll.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <sys/prctl.h>
#include <sys/wait.h>
#include <unistd.h>
#include <wayland-client.h>
#include <wlr/types/wlr_compositor.h>
#include <wlr/types/wlr_scene.h>
#include <wlr/types/wlr_text_input_v3.h>
#include "server.h"
#include "seat.h"
#include "host-text-input.h"
#include "text-input-client-protocol.h"

static struct wlr_surface *parent_surface;
static struct wlr_text_input_v3 *parent_input;
static struct wl_listener parent_surface_new, parent_input_new, parent_commit_listener, parent_enable_listener;
static int evidence_fd;
static void maybe_enter(void) {
    if (parent_surface && parent_input && !parent_input->focused_surface)
        wlr_text_input_v3_send_enter(parent_input, parent_surface);
}
static void parent_new_surface(struct wl_listener *listener, void *data) {
    parent_surface = data; maybe_enter();
}
static void respond(struct wl_listener *listener, void *data) {
    struct wlr_text_input_v3 *t = parent_input;
    if (!t->current_enabled) return;
    if (!t->current.surrounding.text || strcmp(t->current.surrounding.text,"hello")) return;
    assert(t->current.surrounding.cursor == 5 && t->current.surrounding.anchor == 2);
    assert(t->current.content_type.hint == 0x80 && t->current.content_type.purpose == 8);
    assert(t->current.cursor_rectangle.x == 17 && t->current.cursor_rectangle.y == 23);
    assert(t->current.cursor_rectangle.width == 4 && t->current.cursor_rectangle.height == 19);
    wlr_text_input_v3_send_delete_surrounding_text(t, 1, 2);
    wlr_text_input_v3_send_commit_string(t, "你好🙂");
    wlr_text_input_v3_send_preedit_string(t, "é中", 2, 5);
    wlr_text_input_v3_send_done(t);
    assert(write(evidence_fd,"S",1)==1);
}
static void parent_new_input(struct wl_listener *listener, void *data) {
    parent_input = data;
    parent_commit_listener.notify = respond;
    parent_enable_listener.notify = respond;
    wl_signal_add(&parent_input->events.commit, &parent_commit_listener);
    wl_signal_add(&parent_input->events.enable, &parent_enable_listener);
    maybe_enter();
}
static void parent_server(int ready, int evidence) {
    evidence_fd = evidence;
    struct wl_display *d = wl_display_create(); assert(d);
    struct wlr_compositor *compositor = wlr_compositor_create(d, 4, NULL); assert(compositor);
    assert(wlr_seat_create(d,"fixture-parent"));
    struct wlr_text_input_manager_v3 *manager = wlr_text_input_manager_v3_create(d); assert(manager);
    parent_surface_new.notify = parent_new_surface;
    wl_signal_add(&compositor->events.new_surface, &parent_surface_new);
    parent_input_new.notify = parent_new_input;
    wl_signal_add(&manager->events.new_text_input, &parent_input_new);
    const char *name = wl_display_add_socket_auto(d); assert(name);
    assert(write(ready, name, strlen(name)+1) == (ssize_t)strlen(name)+1);
    close(ready);
    wl_display_run(d);
    _exit(0);
}
struct client {
    struct wl_display *display;
    struct wl_registry *registry;
    struct wl_compositor *compositor;
    struct wl_seat *seat;
    struct zwp_text_input_manager_v3 *manager;
    struct wl_surface *surface;
    struct zwp_text_input_v3 *input;
    int enters, leaves, preedits, commits, deletes, dones;
    uint32_t serial;
};
static void global(void *data, struct wl_registry *r, uint32_t name, const char *interface, uint32_t version) {
    struct client *c = data;
    if (!strcmp(interface, "wl_compositor")) c->compositor = wl_registry_bind(r,name,&wl_compositor_interface,4);
    if (!strcmp(interface, "wl_seat")) c->seat = wl_registry_bind(r,name,&wl_seat_interface,1);
    if (!strcmp(interface, "zwp_text_input_manager_v3")) c->manager = wl_registry_bind(r,name,&zwp_text_input_manager_v3_interface,1);
}
static void global_remove(void *d, struct wl_registry *r, uint32_t n) {}
static const struct wl_registry_listener registry_listener = {.global=global,.global_remove=global_remove};
static void enter(void *data, struct zwp_text_input_v3 *t, struct wl_surface *s) { ((struct client *)data)->enters++; }
static void leave(void *data, struct zwp_text_input_v3 *t, struct wl_surface *s) { ((struct client *)data)->leaves++; }
static void preedit(void *data, struct zwp_text_input_v3 *t, const char *text, int32_t begin, int32_t end) {
    assert(text && !strcmp(text,"é中") && begin==2 && end==5); ((struct client *)data)->preedits++;
}
static void text(void *data, struct zwp_text_input_v3 *t, const char *text) {
    assert(text && !strcmp(text,"你好🙂")); ((struct client *)data)->commits++;
}
static void deletion(void *data, struct zwp_text_input_v3 *t, uint32_t before, uint32_t after) {
    assert(before==1 && after==2); ((struct client *)data)->deletes++;
}
static void done(void *data, struct zwp_text_input_v3 *t, uint32_t serial) {
    struct client *c = data; assert(serial==c->serial); c->dones++;
}
static const struct zwp_text_input_v3_listener input_listener = {
    .enter=enter,.leave=leave,.preedit_string=preedit,.commit_string=text,.delete_surrounding_text=deletion,.done=done,
};
static struct cg_server server;
static struct wlr_surface *inner_surface;
static struct wl_listener inner_surface_new;
static void new_surface(struct wl_listener *listener, void *data) { inner_surface = data; }
static void dispatch_ready(struct wl_display *d) {
    struct pollfd p = {.fd=wl_display_get_fd(d), .events=POLLIN};
    if (poll(&p,1,0)>0) assert(wl_display_dispatch(d)>=0);
    else assert(wl_display_dispatch_pending(d)>=0);
}
static void pump(struct client *c, struct wl_display *parent, int count) {
    for(int n=0;n<count;n++) {
        wl_display_flush(c->display); wl_display_flush(parent);
        assert(wl_event_loop_dispatch(wl_display_get_event_loop(server.wl_display), 2)>=0);
        wl_display_flush_clients(server.wl_display);
        dispatch_ready(parent); dispatch_ready(c->display);
    }
}
int main(void) {
    alarm(15);
    int ready[2], evidence[2]; assert(pipe(ready)==0 && pipe(evidence)==0);
    pid_t child=fork(); assert(child>=0);
    if (!child) { assert(prctl(PR_SET_PDEATHSIG,SIGTERM)==0); if(getppid()==1)_exit(1); close(ready[0]);close(evidence[0]);parent_server(ready[1],evidence[1]); }
    close(ready[1]);close(evidence[1]);
    char name[128]={0}; assert(read(ready[0],name,sizeof(name))>0);close(ready[0]);
    struct client host={.display=wl_display_connect(name)}; assert(host.display);
    host.registry=wl_display_get_registry(host.display);
    wl_registry_add_listener(host.registry,&registry_listener,&host);
    assert(wl_display_roundtrip(host.display)>=0);
    host.surface=wl_compositor_create_surface(host.compositor);wl_surface_commit(host.surface);
    server.wl_display=wl_display_create();assert(server.wl_display);
    struct cg_seat seat={.server=&server};server.seat=&seat;
    seat.seat=wlr_seat_create(server.wl_display,"fixture-inner");assert(seat.seat);
    wl_list_init(&server.outputs);server.scene=wlr_scene_create();assert(server.scene);
    struct wlr_compositor *compositor=wlr_compositor_create(server.wl_display,4,NULL);assert(compositor);
    inner_surface_new.notify=new_surface;wl_signal_add(&compositor->events.new_surface,&inner_surface_new);
    struct cg_host_text_input *bridge=host_text_input_create(&server,host.display,host.surface);assert(bridge);
    int pair[2];assert(socketpair(AF_UNIX,SOCK_STREAM,0,pair)==0);
    assert(wl_client_create(server.wl_display,pair[0]));
    struct client app={.display=wl_display_connect_to_fd(pair[1])};assert(app.display);
    app.registry=wl_display_get_registry(app.display);
    wl_registry_add_listener(app.registry,&registry_listener,&app);
    pump(&app,host.display,20);assert(app.compositor && app.seat && app.manager);
    app.surface=wl_compositor_create_surface(app.compositor);wl_surface_commit(app.surface);
    app.input=zwp_text_input_manager_v3_get_text_input(app.manager,app.seat);
    zwp_text_input_v3_add_listener(app.input,&input_listener,&app);
    pump(&app,host.display,20);assert(inner_surface);
    struct wlr_keyboard_modifiers mods={0};
    wlr_seat_keyboard_notify_enter(seat.seat,inner_surface,NULL,0,&mods);
    pump(&app,host.display,20);assert(app.enters==1);
    zwp_text_input_v3_enable(app.input);
    zwp_text_input_v3_set_surrounding_text(app.input,"hello",5,2);
    zwp_text_input_v3_set_content_type(app.input,0x80,8);
    zwp_text_input_v3_set_cursor_rectangle(app.input,17,23,4,19);
    zwp_text_input_v3_commit(app.input);app.serial++;
    pump(&app,host.display,40);
    assert(app.preedits==1 && app.commits==1 && app.deletes==1 && app.dones==1);
    wlr_seat_keyboard_notify_clear_focus(seat.seat);
    pump(&app,host.display,20);assert(app.leaves==1);
    zwp_text_input_v3_commit(app.input);app.serial++;
    pump(&app,host.display,20);assert(app.dones==1);
    zwp_text_input_v3_destroy(app.input);wl_surface_destroy(app.surface);
    pump(&app,host.display,10);
    host_text_input_destroy(bridge);
    wl_display_disconnect(app.display);wl_display_destroy_clients(server.wl_display);
    wl_list_remove(&inner_surface_new.link);
    wl_display_destroy(server.wl_display);
    kill(child,SIGTERM);int status;waitpid(child,&status,0);
    wl_display_disconnect(host.display);
    assert(WIFSIGNALED(status) && WTERMSIG(status)==SIGTERM);
    char evidence_text[64];assert(read(evidence[0],evidence_text,sizeof(evidence_text))==1);
    puts("PASS: real v3 state, UTF-8 commit/preedit byte offsets, delete, serial, focus leave and destroy");
    return 0;
}
