/* Real X11 lifecycle regression: an unmapped dialog can still request geometry. */
#define _POSIX_C_SOURCE 200809L
#include <X11/Xlib.h>
#include <stdio.h>
#include <stdlib.h>
#include <time.h>
#include <unistd.h>
static void settle(Display *d) {
    XSync(d, False);
    struct timespec wait = {.tv_nsec=150000000}; nanosleep(&wait, NULL);
    while (XPending(d)) { XEvent event; XNextEvent(d, &event); }
}
int main(int argc, char **argv) {
    if (argc != 2) return 2;
    Display *d = XOpenDisplay(NULL); if (!d) return 3;
    Window root=DefaultRootWindow(d);
    Window main=XCreateSimpleWindow(d,root,0,0,700,500,0,0,0xffffff);
    XStoreName(d,main,"AgentSeat X11 remap regression");
    XMapWindow(d,main); settle(d);
    sleep(2);
    Window dialog=XCreateSimpleWindow(d,root,60,60,320,200,0,0,0xccffee);
    XStoreName(d,dialog,"Retained dialog"); XSetTransientForHint(d,dialog,main);
    XMapWindow(d,dialog); settle(d);
    for (int i=0;i<20;i++) {
        XUnmapWindow(d,dialog); settle(d);
        XResizeWindow(d,dialog,320+i,200+i); settle(d);
        XMapWindow(d,dialog); settle(d);
    }
    XWindowAttributes geometry; XGetWindowAttributes(d,dialog,&geometry);
    FILE *out=fopen(argv[1],"w"); if (!out) return 4;
    fprintf(out,"{\"cycles\":20,\"width\":%d,\"height\":%d,\"mapped\":%s}\n",geometry.width,geometry.height,geometry.map_state==IsViewable?"true":"false");
    fclose(out);
    for (;;) { XEvent event; XNextEvent(d,&event); }
}
