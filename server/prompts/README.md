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
| `chat/exact_citations.md` | `exact_citations` | `engine/references.py` (`exact_citations_block`) | A question that asks which documents cite a code: the instructions before the complete list of the citing documents |
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

## Compare prompts

The prompts of the document comparison are files under `compare/`, read by `load_prompt(name)` in
[services/compare/prompts.py](../services/compare/prompts.py) (cached, returned exactly as stored: no stripping, so the
files have no trailing newline). None has a placeholder: the text is sent as is, and the braces in `impact_judge.md` and
`analysis_structured.md` are literal JSON.

| File | Loaded by | Used when |
|---|---|---|
| `compare/analysis_default.md` | `routers/compare/config.py` (`DEFAULT_ANALYSIS_PROMPT`) | The comparative method, when `COMPARE_ANALYSIS_SYSTEM_PROMPT` is not set (the variable overrides the file) |
| `compare/analysis_standard.md` | `processors/_diff_engines/prompts.py` (`SYSTEM_PROMPT_STANDARD`) | The standard method: paragraph diff, Markdown output |
| `compare/analysis_structured.md` | `processors/_diff_engines/prompts.py` (`SYSTEM_PROMPT_STRUCTURED`) | The structured method: paragraph diff, JSON array output |
| `compare/summary.md` | `services/compare/summarize.py` (`SUMMARY_SYSTEM_PROMPT`) | Summary of one document's text |
| `compare/summary_image.md` | `services/compare/summarize.py` (`SUMMARY_IMAGE_SYSTEM_PROMPT`) | Description of one image |
| `compare/impact_judge.md` | `services/infra/vector_search.py` (`_JUDGE_SYSTEM_PROMPT`) | Impact search: the verdict on one candidate document |

## See also

- [services/chat/engine](../services/chat/engine/README.md): the code that sends these prompts.
- [server](../README.md): the package overview.
