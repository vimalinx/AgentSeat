/* Test-only private selection consumer with intentionally unread 4 KiB pipes. */
#define _GNU_SOURCE
#include <fcntl.h>
#include <stdbool.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include <wayland-client.h>
#include "ext-data-control-client.h"
static struct wl_display *display;
static struct wl_seat *seat;
static struct ext_data_control_manager_v1 *manager;
static struct ext_data_control_offer_v1 *selection;
static int pipes[20];
static bool requested, closed_readers;
static const char *report;
static void record(bool cleared) {
    char *temporary = NULL;
    if (asprintf(&temporary,"%s.tmp",report)<0) exit(2);
    FILE *file = fopen(temporary, "w");
    if (!file) exit(2);
    fprintf(file,"{\"ready\":%s,\"cleared\":%s,\"readers\":20,\"pipe_capacity\":4096}\n",
        requested ? "true":"false",cleared ? "true":"false");
    if (fclose(file) || rename(temporary,report)) exit(2);
    free(temporary);
}
static void offer(void *data, struct ext_data_control_offer_v1 *object, const char *mime) {
    (void)data;(void)object;(void)mime;
}
static const struct ext_data_control_offer_v1_listener offer_listener = {.offer=offer};
static void data_offer(void *data, struct ext_data_control_device_v1 *device, struct ext_data_control_offer_v1 *object) {
    (void)data;(void)device;
    ext_data_control_offer_v1_add_listener(object,&offer_listener,NULL);
}
static void selected(void *data, struct ext_data_control_device_v1 *device, struct ext_data_control_offer_v1 *object) {
    (void)data;(void)device;
    if (selection) ext_data_control_offer_v1_destroy(selection);
    selection=object;
    if (!object) { if (requested) record(true); return; }
    if (requested) return;
    for (size_t i=0;i<20;i++) {
        int pair[2];
        if (pipe2(pair,O_CLOEXEC)<0 || fcntl(pair[0],F_SETPIPE_SZ,4096)!=4096) exit(3);
        pipes[i]=pair[0];
        if (closed_readers) { close(pipes[i]);pipes[i]=-1; }
        ext_data_control_offer_v1_receive(object,"text/plain;charset=utf-8",pair[1]);
        close(pair[1]);
    }
    requested=true;
}
static void finished(void *data, struct ext_data_control_device_v1 *device) { (void)data;(void)device;exit(4); }
static void primary(void *data, struct ext_data_control_device_v1 *device, struct ext_data_control_offer_v1 *object) {
    (void)data;(void)device;if(object)ext_data_control_offer_v1_destroy(object);
}
static const struct ext_data_control_device_v1_listener device_listener={
    .data_offer=data_offer,.selection=selected,.finished=finished,.primary_selection=primary};
static void global(void *data,struct wl_registry *registry,uint32_t name,const char *interface,uint32_t version) {
    (void)data;(void)version;
    if(!strcmp(interface,"wl_seat"))seat=wl_registry_bind(registry,name,&wl_seat_interface,1);
    if(!strcmp(interface,"ext_data_control_manager_v1"))manager=wl_registry_bind(registry,name,&ext_data_control_manager_v1_interface,1);
}
static void removed(void *data,struct wl_registry *registry,uint32_t name) {(void)data;(void)registry;(void)name;}
static const struct wl_registry_listener registry_listener={.global=global,.global_remove=removed};
int main(int argc,char **argv) {
    if(argc<2 || argc>3)return 2;
    report=argv[1];closed_readers=argc==3&&!strcmp(argv[2],"closed");
    display=wl_display_connect(NULL);if(!display)return 2;
    struct wl_registry *registry=wl_display_get_registry(display);
    wl_registry_add_listener(registry,&registry_listener,NULL);
    if(wl_display_roundtrip(display)<0 || !seat || !manager)return 2;
    struct ext_data_control_device_v1 *device=ext_data_control_manager_v1_get_data_device(manager,seat);
    ext_data_control_device_v1_add_listener(device,&device_listener,NULL);
    if(wl_display_roundtrip(display)<0 || wl_display_roundtrip(display)<0)return 2;
    record(false);
    while(wl_display_dispatch(display)>=0){}
    return 0;
}
