#define _GNU_SOURCE
#include <atk/atk.h>
#include <dbus/dbus.h>
#include <dlfcn.h>
#include <string.h>

/* ATK's D-Bus adaptor acknowledges DoAction, then invokes the action inside
 * dbus_connection_dispatch. A modal action starts a nested GUI loop there;
 * its next accessibility/key event re-enters the same non-reentrant D-Bus
 * dispatch lock. Defer only actions called from that dispatch stack. Ordinary
 * application calls keep their original synchronous behavior. This library is
 * scoped to opt-in accessibility applications, never to the host desktop. */
static _Thread_local unsigned dispatch_depth;
static DBusDispatchStatus (*next_dispatch)(DBusConnection *);
static gboolean (*next_action)(AtkAction *, gint);
static gsize resolved;
static gint pending_actions;

static void resolve_symbols(void) {
    if (g_once_init_enter(&resolved)) {
        void *dispatch = dlsym(RTLD_NEXT, "dbus_connection_dispatch");
        void *action = dlsym(RTLD_NEXT, "atk_action_do_action");
        memcpy(&next_dispatch, &dispatch, sizeof(next_dispatch));
        memcpy(&next_action, &action, sizeof(next_action));
        g_once_init_leave(&resolved, 1);
    }
}

DBusDispatchStatus dbus_connection_dispatch(DBusConnection *connection) {
    resolve_symbols();
    if (!next_dispatch) return DBUS_DISPATCH_NEED_MEMORY;
    ++dispatch_depth;
    DBusDispatchStatus result = next_dispatch(connection);
    --dispatch_depth;
    return result;
}

struct deferred_action { AtkAction *target; gint index; };
static gboolean run_action(gpointer data) {
    /* A different callback can enter a nested loop before dispatch returns. */
    if (dispatch_depth) return G_SOURCE_CONTINUE;
    struct deferred_action *action = data;
    next_action(action->target, action->index);
    return G_SOURCE_REMOVE;
}
static void free_action(gpointer data) {
    struct deferred_action *action = data;
    g_object_unref(action->target);
    g_free(action);
    g_atomic_int_add(&pending_actions, -1);
}

gboolean atk_action_do_action(AtkAction *target, gint index) {
    resolve_symbols();
    if (!next_action) return FALSE;
    if (!dispatch_depth) return next_action(target, index);
    if (!ATK_IS_ACTION(target) || index < 0 || index >= atk_action_get_n_actions(target)) return FALSE;
    if (g_atomic_int_add(&pending_actions, 1) >= 64) {
        g_atomic_int_add(&pending_actions, -1);
        return FALSE;
    }
    struct deferred_action *action = g_new(struct deferred_action, 1);
    action->target = g_object_ref(target);
    action->index = index;
    /* A timeout avoids spinning if an unrelated callback keeps a nested loop
     * alive while D-Bus dispatch is still on the stack. */
    GSource *source = g_timeout_source_new(1);
    g_source_set_callback(source, run_action, action, free_action);
    g_source_attach(source, g_main_context_default());
    g_source_unref(source);
    /* Like AT-SPI's DoAction reply, this acknowledges acceptance, not effect. */
    return TRUE;
}
