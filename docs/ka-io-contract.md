# KA black-box I/O contract — what the Chat sends, expects, receives and displays

Purpose: the functional contract the KA fulfils today for the Chat tab, seen as a black box. The custom module that replaces the KA must behave and return the same things, in broad lines.

## Sources

- **[code]** — this repo (local import of `/Workspace/Shared/Qualibot`).
- **[config]** — live KA configuration on the dev workspace: `qualibot_ALL_v2` / `AS_v2` / `IS_v2`, read 2026-10-05/06.
- **[observed]** — one real streaming call, captured 2026-10-06:
  - endpoint `ka-4d15cb32-endpoint` (ALL), same request body as the app;
  - question `[Date: 2026-10-06]\n\nQuelles procédures parlent de qualification CND ?`;
  - sample of 1.

The **Replacement** column is our deduction, not a sourced fact:
- **accept** — the module must accept this input as-is.
- **reproduce** — the module must behave this way itself.
- **equivalent** — the module must provide the same information, in any wire format.
- **app** — handled by the app, outside the module; unchanged.
- **not needed** — KA-specific detail the app ignores.

## 1. Input — what the app sends to the KA, per turn

| # | Item | Detail | Source | Replacement |
|---|---|---|---|---|
| I1 | Target | One KA per division, each with a single index. ALL → `ka-4d15cb32` (`chunks_index_v1`); AS → `ka-2ef8a9ac` (`chunks_as_index_v1`); IS → `ka-710526e7` (`chunks_is_index_v1`). Chosen by `_endpoint_for_division` (`chat.py:45`). | config, code | reproduce (division → index) |
| I2 | Request body | `{"input": [messages], "stream": true, "databricks_options": {"return_trace": true}}`. Fallback `{"messages": …}` (`streaming.py:381-415`). | code | not needed |
| I3 | Messages | The conversation as held by the browser: `{role: user\|assistant, content: text}` (`ChatView.tsx:298-301`). | code | accept |
| I4 | Assistant history | Earlier assistant turns carry their `⟦n⟧` citation markers (content of the previous `done`, `ChatView.tsx:327`). | code | accept |
| I5 | Error turns | A failed assistant turn is sent back as an assistant message whose content is the error text (`ChatView.tsx:338`). | code | accept |
| I6 | History window | Last `CHAT_MAX_HISTORY` = 10 messages, window starts on a user turn (`chat.py:160`). | code | app |
| I7 | Date | `[Date: YYYY-MM-DD]\n\n` prepended to the last user turn (`chat.py:171`). | code | app |
| I8 | Translation | If the bridge is on and the question isn't FR/EN, the last question is replaced by its EN translation (`chat.py:610-618`). On in `target_config.env`. | code | app |
| I9 | Instructions | None sent by the app. The whole expected behaviour (§2) is configured inside the KA, 6,212–6,429–6,724 chars. | config | reproduce (embed the instructions) |

## 2. Expected behaviour — what the black box does

| # | Behaviour | Detail | Source | Replacement |
|---|---|---|---|---|
| B1 | Retrieval | Finds relevant passages in its own index. Observed: 10 passages, each starting with `[Source: REF \| Title \| Division \| Category \| Date de diffusion]`, with the Intraqual URL as `doc_uri`. | observed | reproduce |
| B2 | Scope | AS / IS: only that division's documents plus shared directives; never mention the other division unless a shared directive does. ALL: both divisions; if the answer differs between AS and IS, make it explicit or ask; never refuse just because no division was given. | config | reproduce |
| B3 | Answer language | Answer in the exact language of the question. Quote in the document's language, adding a translated summary if it differs. | config | reproduce |
| B4 | Search language | Always phrase search queries in French (or English), even for questions in another language. | config | reproduce |
| B5 | AS reference order | AS only: list QP documents first, then MI, then the rest. | config | reproduce |
| B6 | Metadata | Search the metadata to list documents. Take program / customer applicability into account. Pick the language variant (`_EN`, `_FR`…) matching the question. | config | reproduce |
| B7 | Answer format | Accurate, structured, no speculation. No footnotes in the body. Say whether the information comes from documents or metadata. Report outdated or awaiting-validation information. | config | reproduce |
| B8 | Final document list | End with every referenced document: REF, title, version, status (+ division for ALL). Observed as a "**Documents référencés :**" list. | config, observed | reproduce |
| B9 | Recency conflicts | Prefer the most recent document. If two current documents disagree, say so, with dates. A past office-holder named in an old document is not a conflict. | config | reproduce |
| B10 | No fabricated links | Never write document URLs in the body; cite REF, title, version, status only. | config | reproduce |
| B11 | Archived documents | Records containing "ARCHIVED DOCUMENT — CONTENT NOT INDEXED" (pre-2018): never answer their substance; mention them only when directly relevant; say the document exists (REF, title, revision, date), that its content isn't in Qualibot, and to open it in Intraqual. | config | reproduce |
| B12 | When unsure | Ask a clarifying question. If no relevant document exists, say so rather than answer from a loosely related one. | config | reproduce |
| B13 | Safety-critical NO | When an operator's answer is NO (can't keep working / lacks the qualification), make it explicit with ⚠️ 🛑 ❌. | config | reproduce |
| B14 | Inline citations | Attaches statements in the answer to source documents. | observed | reproduce |
| B15 | Markdown | Markdown output: numbered lists, bold REFs, `---` separator. | observed | reproduce |

## 3. Output — what the KA returns (streaming)

| # | Item | Detail | Source | Replacement |
|---|---|---|---|---|
| O1 | Transport | HTTP 200, SSE `data:` lines. | observed | not needed (KA wire format) |
| O2 | Reasoning summary | `response.reasoning_summary_text.delta` ×4, before the text. Generic phrases ("Building a quick summary of the available content...", "Selecting the most efficient path to the answer..."). | observed | not needed (only saved to DB, never displayed) |
| O3 | Answer text | `response.output_text.delta` ×200; 1,522 chars; **no citation markers in the text**. | observed | equivalent (streamed text) |
| O4 | Citations | `response.output_text.annotation.added` ×5, interleaved with the text. Shape: `{type: "url_citation", title: <Intraqual URL ?ref=REF>, url: <same URL + #:~:text=<quoted excerpt>>}`. **No offsets**; also `item_id`, `content_index`, `annotation_index`, `sequence_number`. Position = where it arrives in the stream: here 2 inline, 3 at the very end. | observed | equivalent (citation → document + position) |
| O5 | Final message | `response.output_item.done` ×1. `item {type: message, role: assistant, content[0].text}` = the same answer **with GFM footnotes** `[^xxxx-n]` and their definitions (5,633 chars). | observed | not needed (the app ignores it) |
| O6 | Trace | In `output_item.done`: `databricks_output.trace.info.trace_id` (`tr-…`); spans `examples`, `rerank`, `Final_response`, `docs`. | observed | equivalent (a trace id) |
| O7 | Retrieved passages | RETRIEVER span `docs`: 10 × `{page_content, metadata: {doc_uri, doc_source}, id}`. `page_content` starts with the `[Source: REF \| …]` header; the app reads the REF there. | observed | equivalent (URL → REF mapping) |
| O8 | End | `[DONE]`. | observed | equivalent (end of stream) |
| O9 | Not seen in this call | `response.completed`, `function_call` items, `error` events. The code still handles them (`streaming.py:617-625, 675-777`). | observed, code | — |
| O10 | Errors | Not observed. Handled per code: mid-stream `error` after HTTP 200 (e.g. the KA's own 429); HTTP 429 / 5xx retried; 400 / 422 → format fallback; timeout. | code | equivalent (error signal) |

## 4. App processing of the KA output — `stream_chat()` + `chat_ws` [code]

| # | Step | Detail | Replacement |
|---|---|---|---|
| P1 | Sources | One source per annotated URL, numbered by first appearance. Title relabelled to the REF (from `[Source: REF` in the trace); URL without the `#:~:text` fragment (`streaming.py:574-660`). | app (fed by O4 + O7) |
| P2 | Citation positions | `{n, pos}`, with `pos` = text length when the annotation arrived (`streaming.py:605-615`). | app (fed by O4) |
| P3 | Trace id | `trace.info.trace_id`, else the `x-databricks-request-id` header (`streaming.py:502-507, 637-639`). | app (fed by O6) |
| P4 | Markers | `⟦n⟧` inserted at each `pos`, pushed past links (`chat.py:91`). | app |
| P5 | Extra sources | `augment_sources`: documents named in prose but not annotated are added, without a number (`doc_catalog.py`). | app |
| P6 | Numbering | `_number_sources`: `n` kept only on inline-cited sources (`chat.py:140`). | app |
| P7 | Translation back | If the question was translated (`chat.py:654`). | app |
| P8 | Persistence | Lakebase `chat_messages`: content with `⟦n⟧`, `sources_json`, `trace_id`, `reasoning_steps`, `tool_*`, `endpoint_name`, `division`, `question_lang` (`chat.py:265`). | app |
| P9 | WebSocket to browser | `delta` (withheld while translating), `done {session_id, message_id, content, sources}`, `error`, `ping` (`chat.py:644-715`). | app |

## 5. Display [code]

| # | What the user sees | Detail | Replacement |
|---|---|---|---|
| D1 | Waiting | "Thinking" with animated dots until the first text arrives (`ChatMessage.tsx:565-580`). | app |
| D2 | Streaming | Text appears progressively, raw, no markers, with a cursor. | app |
| D3 | Translation mode | Nothing until the translated answer arrives in `done`. | app |
| D4 | Final answer | Replaced by the final content. `⟦n⟧` renders as superscript `[n]` links to the document (`MarkdownRenderer`). | app |
| D5 | Defensive cleanup | Footnotes and stray HTML stripped (`stripFootnotes`). Fallback `linkifyCitations` turns REF mentions into `[n]` links when no `⟦n⟧` is present. | app |
| D6 | Source chips | Under the answer, after streaming: numbered sources first; language variants grouped into one chip with flags; link to Intraqual in a new tab (`ChatMessage.tsx`). | app |
| D7 | Feedback / download | Up/down vote with comment; download of the answer. | app |
| D8 | Error | The error text replaces the answer, in red. | app |
| D9 | Not displayed | Reasoning, trace, retrieved passages (DB only). | — |
| D10 | Reloaded session | Content with `⟦n⟧` and sources `{title, url, n}` from Lakebase, rendered the same way (`chat.py:832-892`). | app |

## 6. Consequence for the replacement module [deduction]

- Only `stream_chat()` reads the KA wire format (§3). The module doesn't have to imitate the KA's SSE.
- It must deliver what `stream_chat()` emits today:
  - streamed text (O3);
  - sources `{title=REF, url}` and citations `{n, pos}` (O4 + O7);
  - a trace id (O6);
  - errors (O10).
- Everything in §4–5 stays as is.
- It must reproduce the behaviours of §2 and accept the inputs of §1 (I3–I5, I9).

## 7. Observations and caveats

- One captured call only. The event counts (4 / 200 / 5) are not a constant.
- In that call, citation 2 is placed after the `Q0197QP_FR` line but points to `Q0451MQ`.
- The live `qualibot_AS_v2` instructions end with a stray authoring note ("▎ Note : j'ai intégré l'ordre de citation QP→MI …") [config].
- The KA's internal retrieval (queries, filters, search type, candidates before reranking) is not visible: the `docs` and `rerank` spans don't expose their inputs.
