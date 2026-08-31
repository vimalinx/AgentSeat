#define _GNU_SOURCE

#include "agentseat-i18n.h"

#include <errno.h>
#include <fcntl.h>
#include <limits.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <strings.h>
#include <sys/stat.h>
#include <unistd.h>

#define MO_MAGIC 0x950412deU
#define MO_MAGIC_SWAPPED 0xde120495U
#define MAX_CATALOG_SIZE (16U * 1024U * 1024U)

struct message_catalog {
    unsigned char* data;
    size_t size;
    uint32_t count;
    uint32_t originals;
    uint32_t translations;
    bool swapped;
};

static struct message_catalog catalog = {0};

static uint32_t catalog_word(size_t offset) {
    uint32_t value = 0;
    if (!catalog.data || offset > catalog.size || catalog.size - offset < sizeof(value))
        return 0;
    memcpy(&value, catalog.data + offset, sizeof(value));
    return catalog.swapped ? __builtin_bswap32(value) : value;
}

static bool table_entry(uint32_t table, uint32_t index, const char** text, uint32_t* length) {
    const size_t entry = (size_t)table + (size_t)index * 8U;
    if (entry > catalog.size || catalog.size - entry < 8U)
        return false;
    const uint32_t candidate_length = catalog_word(entry);
    const uint32_t candidate_offset = catalog_word(entry + 4U);
    if ((size_t)candidate_offset >= catalog.size ||
        (size_t)candidate_length >= catalog.size - (size_t)candidate_offset ||
        catalog.data[candidate_offset + candidate_length] != '\0') {
        return false;
    }
    *text = (const char*)catalog.data + candidate_offset;
    *length = candidate_length;
    return true;
}

const char* agentseat_translate(const char* message) {
    if (!message || !catalog.data)
        return message;
    uint32_t low = 0;
    uint32_t high = catalog.count;
    while (low < high) {
        const uint32_t middle = low + (high - low) / 2U;
        const char* original = NULL;
        uint32_t original_length = 0;
        if (!table_entry(catalog.originals, middle, &original, &original_length))
            return message;
        const int relation = strcmp(message, original);
        if (relation < 0) {
            high = middle;
        } else if (relation > 0) {
            low = middle + 1U;
        } else {
            const char* translated = NULL;
            uint32_t translated_length = 0;
            if (table_entry(catalog.translations, middle, &translated, &translated_length) &&
                translated_length > 0) {
                return translated;
            }
            return message;
        }
    }
    return message;
}

static const char* normalized_language(const char* requested) {
    if (!requested || !requested[0] || strcasecmp(requested, "auto") == 0)
        return NULL;
    if (strncasecmp(requested, "zh", 2) == 0) {
        const bool traditional = strcasestr(requested, "TW") || strcasestr(requested, "HK") ||
            strcasestr(requested, "Hant");
        return traditional ? "zh_TW" : "zh_CN";
    }
    return NULL;
}

static const char* selected_language(void) {
    const char* override = getenv("AGENTSEAT_LANGUAGE");
    if (override && override[0] && strcasecmp(override, "auto") != 0)
        return normalized_language(override);

    const char* language_list = getenv("LANGUAGE");
    while (language_list && language_list[0]) {
        const char* separator = strchr(language_list, ':');
        const size_t length = separator ? (size_t)(separator - language_list) : strlen(language_list);
        char candidate[64] = {0};
        if (length > 0 && length < sizeof(candidate)) {
            memcpy(candidate, language_list, length);
            const char* selected = normalized_language(candidate);
            if (selected)
                return selected;
        }
        if (!separator)
            break;
        language_list = separator + 1;
    }

    const char* selected = NULL;
    const char* candidates[] = {
        getenv("LC_ALL"),
        getenv("LC_MESSAGES"),
        getenv("LANG"),
    };
    for (size_t i = 0; i < sizeof(candidates) / sizeof(candidates[0]); ++i) {
        selected = normalized_language(candidates[i]);
        if (selected)
            return selected;
    }
    return NULL;
}

static const char* discovered_locale_dir(char* output, size_t output_size) {
    const char* configured = getenv("AGENTSEAT_LOCALE_DIR");
    if (configured && configured[0])
        return configured;

    char executable[PATH_MAX] = {0};
    const ssize_t length = readlink("/proc/self/exe", executable, sizeof(executable) - 1);
    if (length <= 0)
        return NULL;
    executable[length] = '\0';
    char* slash = strrchr(executable, '/');
    if (!slash)
        return NULL;
    *slash = '\0';
    slash = strrchr(executable, '/');
    if (!slash)
        return NULL;
    *slash = '\0';

    snprintf(output, output_size, "%s/share/locale", executable);
    if (access(output, R_OK) == 0)
        return output;
    snprintf(output, output_size, "%s/build/locale", executable);
    if (access(output, R_OK) == 0)
        return output;
    return NULL;
}

static bool load_catalog(const char* path) {
    int fd = open(path, O_RDONLY | O_CLOEXEC);
    if (fd < 0)
        return false;
    struct stat information = {0};
    if (fstat(fd, &information) != 0 || information.st_size < 28 ||
        information.st_size > MAX_CATALOG_SIZE) {
        close(fd);
        return false;
    }
    unsigned char* data = malloc((size_t)information.st_size);
    if (!data) {
        close(fd);
        return false;
    }
    size_t consumed = 0;
    while (consumed < (size_t)information.st_size) {
        ssize_t count = read(fd, data + consumed, (size_t)information.st_size - consumed);
        if (count < 0 && errno == EINTR)
            continue;
        if (count <= 0) {
            free(data);
            close(fd);
            return false;
        }
        consumed += (size_t)count;
    }
    close(fd);

    catalog.data = data;
    catalog.size = consumed;
    const uint32_t magic = catalog_word(0);
    if (magic == MO_MAGIC) {
        catalog.swapped = false;
    } else if (magic == MO_MAGIC_SWAPPED) {
        catalog.swapped = true;
    } else {
        free(catalog.data);
        memset(&catalog, 0, sizeof(catalog));
        return false;
    }
    catalog.count = catalog_word(8);
    catalog.originals = catalog_word(12);
    catalog.translations = catalog_word(16);
    if (catalog.count > 100000U ||
        (size_t)catalog.originals + (size_t)catalog.count * 8U > catalog.size ||
        (size_t)catalog.translations + (size_t)catalog.count * 8U > catalog.size) {
        free(catalog.data);
        memset(&catalog, 0, sizeof(catalog));
        return false;
    }
    return true;
}

void agentseat_i18n_init(void) {
    const char* language = selected_language();
    if (!language)
        return;
    char locale_dir[PATH_MAX] = {0};
    const char* discovered = discovered_locale_dir(locale_dir, sizeof(locale_dir));
    if (!discovered)
        return;
    char catalog_path[PATH_MAX] = {0};
    const int length = snprintf(
        catalog_path,
        sizeof(catalog_path),
        "%s/%s/LC_MESSAGES/%s.mo",
        discovered,
        language,
        AGENTSEAT_TEXT_DOMAIN);
    if (length <= 0 || (size_t)length >= sizeof(catalog_path))
        return;
    load_catalog(catalog_path);
}
