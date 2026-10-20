# server/prompts

The texts of the chatbot's prompts, one file per prompt, under `chat/`. The code reads them at run time. They are data:
the prompts are not Python strings, and they are edited as plain text.

Parent: [server](../README.md). The loader is [services/chat/prompts.py](../services/chat/prompts.py), and the code that
uses the prompts is in [services/chat/engine](../services/chat/engine/README.md).

## Files

| File | Key | Loaded by | Used when |
|---|---|---|---|
| `chat/system/base.md` | `system_base` | `engine/config.py` (`prompt_parts`) | Every answer. Sent after the division part |
| `chat/system/all.md` | `system_all` | `engine/config.py` (`prompt_parts`) | Division `ALL`: no division filter |
| `chat/system/as.md` | `system_as` | `engine/config.py` (`prompt_parts`) | Division `AS` |
| `chat/system/is.md` | `system_is` | `engine/config.py` (`prompt_parts`) | Division `IS` |
| `chat/rewrite.md` | `rewrite` | `engine/queries.py` (`REWRITE_PROMPT`) | The bilingual search rewrite: follow-up messages and other or unsure languages |
| `chat/translate_query.md` | `translate_query` | `engine/queries.py` (`translate_query`) | An opening message in French or English, when `CHAT_TRANSLATE_ONLY` is on |
| `chat/language_reminder.md` | `language_reminder` | `engine/answer.py` (`LANGUAGE_REMINDER`) | Appended to the last user message: the answer language, or the language of the question when it is not known |

## How they are read

- `services/chat/prompts.py` maps each key to its file in `FILES`. `read(key)` returns the stripped text, cached for the
  life of the process. `digest(key)` is its hash, which links the registered MLflow version of the prompt to a trace.
- `keys_for_turn` lists the prompt keys of a turn, for the trace.
- The division part and the base are joined in that order, with a blank line between them.

## Rules

- The only placeholder is `{language}`, in `translate_query.md` and `language_reminder.md`. The code fills it: a
  replacement in `translate_query`, a `format` call for the reminder. Any other brace in these files would break a call.
- A prompt is edited in its file only. A trace links a registered prompt version only when the file's hash equals the
  registered hash. After an edit, the registration job runs again (`utils/evaluation/register_chat_prompts.py`).
- The compare prompts are not in this folder. The default analysis prompt is a constant of `routers/compare/common.py`,
  overridden by `COMPARE_ANALYSIS_SYSTEM_PROMPT`. The summary prompts are in `services/compare/summarize.py`, the diff
  prompts in `services/compare/processors/_diff_engines/prompts.py`, and the impact judge prompt in
  `services/infra/vector_search.py`. All of them are Python constants.

## See also

- [services/chat/engine](../services/chat/engine/README.md): the code that sends these prompts.
- [server](../README.md): the package overview.
