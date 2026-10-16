#include "request-ledger.h"
#include <stdio.h>
#include <string.h>

static void forget(struct as_request_ledger* ledger, struct as_request_record* record) {
    if (!record->response)
        return;
    ledger->bytes -= record->bytes;
    json_object_put(record->params);
    json_object_put(record->response);
    *record = (struct as_request_record){0};
}

const char* as_request_begin(struct as_request_ledger* ledger, const char* method,
    json_object* params, json_object** replay, size_t* controller, uint64_t* sequence) {
    json_object *owner = NULL, *seq = NULL;
    *replay = NULL;
    if (!json_object_object_get_ex(params, "controller_id", &owner) ||
        !json_object_is_type(owner, json_type_string) ||
        json_object_get_string_len(owner) < 1 || json_object_get_string_len(owner) > 128 ||
        strlen(json_object_get_string(owner)) != (size_t)json_object_get_string_len(owner) ||
        !json_object_object_get_ex(params, "request_id", &seq) ||
        !json_object_is_type(seq, json_type_int) || json_object_get_int64(seq) <= 0)
        return "REQUEST_ID_REQUIRED";
    if (strlen(method) >= sizeof(ledger->records[0].method) ||
        strlen(json_object_to_json_string_ext(params, JSON_C_TO_STRING_PLAIN)) > 65536)
        return "RESOURCE_LIMIT";
    const char* id = json_object_get_string(owner);
    *sequence = (uint64_t)json_object_get_int64(seq);
    size_t index = 0;
    while (index < ledger->controller_count && strcmp(ledger->controllers[index].id, id) != 0)
        index++;
    if (index == ledger->controller_count) {
        if (index == AS_MAX_CONTROLLERS)
            return "RESOURCE_LIMIT";
        snprintf(ledger->controllers[index].id, sizeof(ledger->controllers[index].id), "%s", id);
        ledger->controller_count++;
    }
    *controller = index;
    for (size_t i = 0; i < AS_REQUEST_HISTORY; ++i) {
        struct as_request_record* record = &ledger->records[i];
        if (record->response && record->controller == index && record->sequence == *sequence) {
            if (strcmp(record->method, method) != 0 || !json_object_equal(record->params, params))
                return "REQUEST_ID_CONFLICT";
            *replay = record->response;
            return NULL;
        }
    }
    /* Receipt eviction cannot make an old request executable again. Keep the
     * small per-controller high-water mark for the entire session. */
    if (*sequence <= ledger->controllers[index].highest)
        return "REQUEST_EXPIRED";
    ledger->controllers[index].highest = *sequence;
    return NULL;
}

void as_request_finish(struct as_request_ledger* ledger, size_t controller,
    uint64_t sequence, const char* method, json_object* params, json_object* response) {
    size_t bytes = strlen(json_object_to_json_string_ext(params, JSON_C_TO_STRING_PLAIN)) +
        strlen(json_object_to_json_string_ext(response, JSON_C_TO_STRING_PLAIN));
    /* A missing receipt is still protected by the watermark. */
    if (bytes > AS_LEDGER_BYTES)
        return;
    struct as_request_record* record = NULL;
    do {
        record = &ledger->records[ledger->next_record++ % AS_REQUEST_HISTORY];
        forget(ledger, record);
    } while (ledger->bytes + bytes > AS_LEDGER_BYTES);
    *record = (struct as_request_record){.controller = controller, .sequence = sequence,
        .params = json_object_get(params), .response = json_object_get(response), .bytes = bytes};
    snprintf(record->method, sizeof(record->method), "%s", method);
    ledger->bytes += bytes;
}

json_object* as_request_get(struct as_request_ledger* ledger, const char* controller,
    uint64_t sequence) {
    for (size_t i = 0; i < AS_REQUEST_HISTORY; ++i) {
        struct as_request_record* record = &ledger->records[i];
        if (record->response && record->sequence == sequence &&
            strcmp(ledger->controllers[record->controller].id, controller) == 0)
            return record->response;
    }
    return NULL;
}

void as_request_ledger_finish(struct as_request_ledger* ledger) {
    for (size_t i = 0; i < AS_REQUEST_HISTORY; ++i)
        forget(ledger, &ledger->records[i]);
}
