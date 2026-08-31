#ifndef AGENTSEAT_I18N_H
#define AGENTSEAT_I18N_H

#define AGENTSEAT_TEXT_DOMAIN "agentseat"
#define AS_TR(message) agentseat_translate(message)

void agentseat_i18n_init(void);
const char* agentseat_translate(const char* message);

#endif
