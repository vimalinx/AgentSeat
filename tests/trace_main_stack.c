/* Opt-in diagnostic for an owned test application. Never installed/preloaded
 * during ordinary operation. Prints the receiving thread's stack on SIGUSR2. */
#define _GNU_SOURCE
#include <execinfo.h>
#include <fcntl.h>
#include <signal.h>
#include <stdlib.h>
#include <unistd.h>
static int trace_fd = -1;
static void trace_stack(int signal_number) {
    (void)signal_number;
    void *frames[64];
    const char marker[] = "\n--- owned application stack ---\n";
    if (trace_fd < 0) return;
    (void)write(trace_fd, marker, sizeof(marker) - 1);
    backtrace_symbols_fd(frames, backtrace(frames, 64), trace_fd);
}
__attribute__((constructor)) static void enable_trace(void) {
    const char *path = getenv("AGENTSEAT_DIAG_STACK_FILE");
    if (!path) return;
    trace_fd = open(path, O_WRONLY | O_CREAT | O_APPEND | O_CLOEXEC | O_NOFOLLOW, 0600);
    if (trace_fd < 0) return;
    void *warm[2]; (void)backtrace(warm, 2);
    struct sigaction action = {.sa_handler = trace_stack};
    sigemptyset(&action.sa_mask);
    sigaction(SIGUSR2, &action, NULL);
}
