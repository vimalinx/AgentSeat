/* Independent X11 drawable and input oracle: single-pixel stripes plus clicks. */
#define _POSIX_C_SOURCE 200809L
#include <X11/Xlib.h>
#include <X11/Xutil.h>
#include <stdio.h>
#include <stdlib.h>
#include <unistd.h>
int main(int argc, char **argv) {
    if (argc != 2) return 2;
    Display *d = XOpenDisplay(NULL); if (!d) return 3;
    Window w = XCreateSimpleWindow(d, DefaultRootWindow(d), 0, 0, 800, 600, 0, 0, 0xffffff);
    XStoreName(d, w, "AgentSeat physical pixel and click oracle");
    XSelectInput(d, w, ExposureMask | StructureNotifyMask | ButtonPressMask | PointerMotionMask);
    XMapWindow(d, w);
    GC gc = XCreateGC(d, w, 0, NULL);
    int clicks=0, x=-1, y=-1, motion_x=-1, motion_y=-1, root_x=-1, root_y=-1;
    for (;;) {
        XEvent ev; XNextEvent(d, &ev);
        if (ev.type == ButtonPress) { clicks++; x=ev.xbutton.x; y=ev.xbutton.y; root_x=ev.xbutton.x_root; root_y=ev.xbutton.y_root; }
        if (ev.type == MotionNotify) { motion_x=ev.xmotion.x; motion_y=ev.xmotion.y; }
        XWindowAttributes a; XGetWindowAttributes(d, w, &a);
        if (a.map_state != IsViewable) continue;
        if (ev.type == Expose || ev.type == ConfigureNotify) {
            XSetForeground(d, gc, 0xffffff); XFillRectangle(d,w,gc,0,0,a.width,a.height);
            XSetForeground(d, gc, 0);
            for (int i=0; i<a.width; i+=2) XDrawLine(d,w,gc,i,0,i,a.height-1);
            XSync(d, False);
        }
        XImage *im = XGetImage(d,w,0,0,a.width,a.height,AllPlanes,ZPixmap);
        if (!im) return 4;
        char temp[4096]; snprintf(temp,sizeof(temp),"%s.tmp",argv[1]);
        FILE *f=fopen(temp,"w"); if (!f) return 5;
        fprintf(f,"{\"width\":%d,\"height\":%d,\"image_width\":%d,\"image_height\":%d,\"clicks\":%d,\"x\":%d,\"y\":%d,\"motion_x\":%d,\"motion_y\":%d,\"root_x\":%d,\"root_y\":%d,\"window_x\":%d,\"window_y\":%d}\n",a.width,a.height,im->width,im->height,clicks,x,y,motion_x,motion_y,root_x,root_y,a.x,a.y);
        fclose(f); rename(temp,argv[1]); XDestroyImage(im);
    }
}
