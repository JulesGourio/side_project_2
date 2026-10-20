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
| `chat/system/division.md` | `system_division` | `engine/config.py` (`division_prompt`) | Divisions `AS` and `IS`: one template, filled with the names in `DIVISION_NAMES` |
| `chat/system/as_reference_ordering.md` | `system_as_references` | `engine/config.py` (`division_prompt`) | Division `AS` only: the order of the references in a list (appended to the template) |
| `chat/rewrite.md` | `rewrite` | `engine/queries.py` (`REWRITE_PROMPT`) | The bilingual search rewrite: follow-up messages and other or unsure languages |
| `chat/translate_query.md` | `translate_query` | `engine/queries.py` (`translate_query`) | An opening message in French or English, when `CHAT_TRANSLATE_ONLY` is on |
| `chat/language_reminder.md` | `language_reminder` | `engine/answer.py` (`LANGUAGE_REMINDER`) | Appended to the last user message: the answer language, or the language of the question when it is not known |

## How they are read

- `services/chat/prompts.py` maps each key to its file in `FILES`. `read(key)` returns the stripped text, cached for the
  life of the process. `digest(key)` is its hash, which links the registered MLflow version of the prompt to a trace.
- `keys_for_turn` lists the prompt keys of a turn, for the trace.
- The division part and the base are joined in that order, with a blank line between them. For `AS` and `IS` the division
  part is `division.md` with `{code}`, `{name}`, `{other_code}` and `{other_name}` filled in, plus `as_reference_ordering.md`
  for `AS`.

## Rules

- The placeholders are `{language}`, in `translate_query.md` and `language_reminder.md`, and `{code}`, `{name}`,
  `{other_code}`, `{other_name}` in `system/division.md`. The code fills them: a replacement in `translate_query`, a `format`
  call for the others. Any other brace in these files would break a call.
- The prompts stay files, not Python settings: the registration job hashes the files, and a trace links a registered
  version only when the hash matches. What varies between the divisions is in `DIVISION_NAMES`, in `engine/config.py`.
- A prompt is edited in its file only. A trace links a registered prompt version only when the file's hash equals the
  registered hash. After an edit, the registration job runs again (`utils/evaluation/register_chat_prompts.py`).
- The compare prompts are not in this folder. The default analysis prompt is a constant of `routers/compare/config.py`,
  overridden by `COMPARE_ANALYSIS_SYSTEM_PROMPT`. The summary prompts are in `services/compare/summarize.py`, the diff
  prompts in `services/compare/processors/_diff_engines/prompts.py`, and the impact judge prompt in
  `services/infra/vector_search.py`. All of them are Python constants.

## See also

- [services/chat/engine](../services/chat/engine/README.md): the code that sends these prompts.
- [server](../README.md): the package overview.
