/* Application requested placement, through the real private XWayland WM. */
#define _POSIX_C_SOURCE 200809L
#include <X11/Xlib.h>
#include <assert.h>
#include <stdio.h>
#include <stdlib.h>
#include <time.h>
#include <unistd.h>
static void settle(Display *d) {
    XSync(d,False); struct timespec t={.tv_nsec=250000000};nanosleep(&t,NULL);
    while(XPending(d)){XEvent e;XNextEvent(d,&e);}
}
static void check(Display *d,Window w,int x,int y,int width,int height) {
    settle(d);Window child;int rx,ry;XWindowAttributes a;XGetWindowAttributes(d,w,&a);
    XTranslateCoordinates(d,w,DefaultRootWindow(d),0,0,&rx,&ry,&child);
    if(rx!=x || ry!=y || a.width!=width || a.height!=height) {
        fprintf(stderr,"geometry expected %d,%d %dx%d actual %d,%d %dx%d\n",x,y,width,height,rx,ry,a.width,a.height);exit(4);
    }
}
int main(int argc,char **argv){
    if(argc!=2)return 2;
    Display *d=XOpenDisplay(NULL);assert(d);Window root=DefaultRootWindow(d);
    Window main=XCreateSimpleWindow(d,root,0,0,700,500,0,0,0xffffff);
    XStoreName(d,main,"Popup positioning acceptance");XMapWindow(d,main);settle(d);sleep(2);
    Window dialog=XCreateSimpleWindow(d,root,41,73,240,130,0,0,0x99ccff);
    XStoreName(d,dialog,"Application placed dialog");XSetTransientForHint(d,dialog,main);
    XMapWindow(d,dialog);check(d,dialog,41,73,240,130);
    XMoveWindow(d,dialog,93,117);check(d,dialog,93,117,240,130);
    XResizeWindow(d,dialog,250,140);check(d,dialog,93,117,250,140);
    XUnmapWindow(d,dialog);settle(d);XMoveResizeWindow(d,dialog,61,91,230,120);settle(d);
    XMapWindow(d,dialog);check(d,dialog,61,91,230,120);
    Window menu=XCreateSimpleWindow(d,root,305,211,180,90,0,0,0x99ffcc);
    XSetWindowAttributes wa={.override_redirect=True};XChangeWindowAttributes(d,menu,CWOverrideRedirect,&wa);
    XStoreName(d,menu,"Anchored menu");XMapWindow(d,menu);check(d,menu,305,211,180,90);
    XMoveWindow(d,menu,287,189);check(d,menu,287,189,180,90);
    Window origin=XCreateSimpleWindow(d,root,0,0,90,45,0,0,0xffcc99);
    XSetTransientForHint(d,origin,main);XStoreName(d,origin,"Explicit origin");XMapWindow(d,origin);check(d,origin,0,0,90,45);
    FILE *f=fopen(argv[1],"w");assert(f);fputs("{\"status\":\"PASS\",\"checks\":7}\n",f);fclose(f);
    for(;;){XEvent e;XNextEvent(d,&e);}
}
