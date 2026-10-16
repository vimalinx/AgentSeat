#define _GNU_SOURCE
#include <stdlib.h>
#include <unistd.h>
#include <sys/syscall.h>
#include <wayland-server-core.h>
#include "application-lifetime.h"
#include "server.h"
#include "view.h"
#if CAGE_HAS_XWAYLAND
#include "xwayland.h"
#endif

struct cg_application_process {
    struct cg_application_process *next;
    struct cg_server *server;
    struct wl_event_source *source;
    pid_t pid;
    int fd;
};

bool application_lifetime_alive(struct cg_server *server)
{
    return view_has_application_views(server) || server->application_processes != NULL;
}

static int process_exited(int fd, uint32_t mask, void *data)
{
    struct cg_application_process *process = data;
    struct cg_server *server = process->server;
    struct cg_application_process **link = &server->application_processes;
    while (*link && *link != process)
        link = &(*link)->next;
    if (*link)
        *link = process->next;
    wl_event_source_remove(process->source);
    close(process->fd);
    free(process);
    if (server->primary_client_exited && !application_lifetime_alive(server))
        server_terminate(server);
    return 0;
}

void application_lifetime_track(struct cg_view *view)
{
    pid_t pid = 0;
    if (view->type == CAGE_XDG_SHELL_VIEW)
        wl_client_get_credentials(wl_resource_get_client(view->wlr_surface->resource), &pid, NULL, NULL);
#if CAGE_HAS_XWAYLAND
    else
        pid = xwayland_view_from_view(view)->xwayland_surface->pid;
#endif
    if (pid <= 0 || pid == getpid())
        return;
    struct cg_server *server = view->server;
    for (struct cg_application_process *p = server->application_processes; p; p = p->next)
        if (p->pid == pid)
            return;
    int fd = syscall(SYS_pidfd_open, pid, 0);
    if (fd < 0)
        return;
    struct cg_application_process *process = calloc(1, sizeof(*process));
    if (!process) {
        close(fd);
        return;
    }
    process->server = server;
    process->pid = pid;
    process->fd = fd;
    process->source = wl_event_loop_add_fd(wl_display_get_event_loop(server->wl_display), fd,
                                           WL_EVENT_READABLE, process_exited, process);
    if (!process->source) {
        close(fd);
        free(process);
        return;
    }
    process->next = server->application_processes;
    server->application_processes = process;
}

void application_lifetime_finish(struct cg_server *server)
{
    while (server->application_processes) {
        struct cg_application_process *process = server->application_processes;
        server->application_processes = process->next;
        wl_event_source_remove(process->source);
        close(process->fd);
        free(process);
    }
}
