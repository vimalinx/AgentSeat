#pragma once
#include <stdbool.h>
#include <stdint.h>
#include <sys/types.h>
#include <poll.h>
#include <json-c/json.h>
#define AS_SEMANTIC_POLL_MAX 16
struct as_semantic;
struct as_semantic_status {
    const char *stage, *error;
    bool busy, ready, sent, replied, succeeded, cancelled;
};
struct as_semantic* as_semantic_new(const char* control_path, const char* session_id, pid_t microhost_pid);
bool as_semantic_start(struct as_semantic*, const char* method, json_object* params);
void as_semantic_submit(struct as_semantic*);
void as_semantic_cancel(struct as_semantic*);
void as_semantic_status(struct as_semantic*, struct as_semantic_status*);
int as_semantic_prepare_poll(struct as_semantic*, struct pollfd*, int* timeout);
void as_semantic_dispatch(struct as_semantic*, const struct pollfd*);
void as_semantic_free(struct as_semantic*);
