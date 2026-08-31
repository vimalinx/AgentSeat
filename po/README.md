# Translating AgentSeat

English source strings live beside the code. Simplified Chinese (`zh_CN`) and
Traditional Chinese (`zh_TW`) are maintained as standard gettext catalogs.

After adding or changing a translatable string, run:

```sh
./scripts/update-translations.sh
./scripts/check-translations.sh
```

Translate the new empty `msgstr` entries without changing placeholders such as
`{path}`, `%s`, or `%d`. `./scripts/build.sh` compiles the catalogs to `build/locale`;
`./scripts/install.sh` installs only the compact binary `.mo` files.

JSON field names, enum values, RPC method names, and error codes are protocol
data and are intentionally not translated.
