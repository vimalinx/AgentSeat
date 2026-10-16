#pragma once
#include <json-c/json.h>
#include <stddef.h>
#include <stdint.h>

#define AS_MAX_CONTROLLERS 128
#define AS_REQUEST_HISTORY 128
#define AS_LEDGER_BYTES (1024U * 1024U)

struct as_controller_watermark {
    char id[129];
    uint64_t highest;
};

struct as_request_record {
    size_t controller;
    uint64_t sequence;
    char method[96];
    json_object *params, *response;
    size_t bytes;
};

struct as_request_ledger {
    struct as_controller_watermark controllers[AS_MAX_CONTROLLERS];
    struct as_request_record records[AS_REQUEST_HISTORY];
    size_t controller_count, next_record, bytes;
};

/* NULL means a new request was admitted. A replay response is borrowed.
 * The caller must finish every admitted request, including rejected effects. */
const char* as_request_begin(struct as_request_ledger* ledger, const char* method,
    json_object* params, json_object** replay, size_t* controller, uint64_t* sequence);
void as_request_finish(struct as_request_ledger* ledger, size_t controller,
    uint64_t sequence, const char* method, json_object* params, json_object* response);
json_object* as_request_get(struct as_request_ledger* ledger, const char* controller,
    uint64_t sequence);
void as_request_ledger_finish(struct as_request_ledger* ledger);
