# Chatbot

A RAG chat over the company's document base: Vector Search finds the passages, GPT-6 Luna
answers from them only, with inline citations, division scoping (ALL / AS / IS) and a
translation bridge for questions that are neither French nor English. Every choice below
was measured: `docs/chat_vsi_tests.md` (configuration kept, results, what was dropped).
The former Knowledge Assistant (Agent Bricks) was removed on 2026-10-08 (`archive/`).

## How it works

### End-to-end flow (`WS /api/chat/ws`)

`chat_ws` (`server/routers/chat.py`): **trim history → translation bridge → inject today's
date → `stream_chat_vsi` → citation markers → translate back → persist the turn.**

1. **History trimming** (`_trim_history`, `CHAT_MAX_HISTORY`, default 10) — bounds prompt
   growth; the latest question is always kept.
2. **Translation bridge** (see below) — a question in a third language is searched and
   answered in English, then translated back.
3. **Date injection** (`_with_today_date`) — prepends `[Date: YYYY-MM-DD]` to the last user
   message only, so date-relative questions resolve correctly.
4. **The engine** (`server/services/chat_vsi.py::stream_chat_vsi`):
   - bilingual rewrite of the question (French + English, acronyms expanded) by GPT-6 Luna;
   - 3 HYBRID Vector Search queries on `CHAT_VSI_INDEX`, each = 12 reranked passages + 10 raw
     ones, merged by rank; the division is a `filters_json` filter on the `division` column;
   - documents named by REF in the conversation first, documents whose catalogue title matches
     the question last (`chat_vsi_titles.py`), one language variant per document. The catalogue
     (REF, title, link) is the Lakebase table `doc_catalog`, rewritten by the parsing pipeline
     after each daily run and reloaded by the app every 30 min (`doc_catalog.py`);
   - prompt = division instructions (`server/config/chat_vsi/instructions_<div>.md`) + answer
     rules (`answer_rules.md`) + numbered documents + question + a line naming the answer
     language;
   - answer streamed by `chat_vsi_llm.stream_answer`: GPT-6 Luna, GPT-5.6 Luna as fallback,
     retries, continuation of a cut answer, at most 32 answers at once per instance
     (`docs/chat_vsi_robustesse_2026-10.md`);
   - the model writes `[n]` markers; `CitationStreamParser` removes them from the stream and
     emits their positions in a `sources` event.
5. **Citation assembly** — markers use `⟦n⟧` (U+27E6/U+27E7, essentially never present in
   source documents) inserted at each citation's character offset, right-to-left so earlier
   offsets stay valid. Baked into the stored `content` so reloaded conversations show the
   same inline citations without re-computing anything.
6. **Persistence** — one `chat_sessions` row + two `chat_messages` rows (user + assistant)
   per turn, including failed turns (status `'error'`), so a question and its failure reason
   are always traceable. `endpoint_name` = `vsi-all` / `vsi-as` / `vsi-is` (older turns of the
   Knowledge Assistant carry `ka-…` names).

### Translation bridge (`server/services/translation_bridge.py`)

Gated by `CHAT_TRANSLATE_BRIDGE_ENABLED` (`false` in `app.yaml`, `true` on every target in
`utils/deploy/target_env.json`). When enabled:

- A cheap local **fastText** language-ID pass screens the question; if
  confidence > 65% and the language isn't French/English, an LLM call
  (`CHAT_TRANSLATE_ENDPOINT`) confirms the language and produces an English
  translation in one request.
- The English translation (not the original) is what reaches the search
  and the model — the corpus is almost entirely French/English, so retrieval on
  a third language's raw text only matches the sparse same-language slice.
- The final answer is translated back to the user's language in one pass
  before streaming starts (the client shows "Thinking" until that
  translation completes — raw English is never shown). Markdown, `REF`
  codes, dates, and `⟦n⟧` citation markers are instructed to pass through
  unchanged.
- As a safeguard, even when the bridge doesn't trigger (question already
  FR/EN), the answer's language is checked and re-translated if it doesn't
  match the question's — guards against the model answering in the wrong
  language mid-conversation.
- `chat_messages.question_lang` stores the detected ISO 639-1 code (empty
  string when the bridge is disabled or detection wasn't attempted).

### Sharing a conversation

A conversation can be shared read-only via a link: `POST
/api/chat/sessions/{id}/share` (owner only) sets `chat_sessions.share_token`
(a random `secrets.token_urlsafe(20)`, generated once — re-sharing returns the
same token rather than rotating it) and the client copies
`/chat/shared/{token}` to the clipboard. `GET /api/chat/shared/{token}`
(`SharedChatPage.tsx`) serves that conversation read-only to **any**
authenticated Qualibot user who has the link — no ownership check, since a
share link's whole point is to be handed to someone else. It is a **live**
link: the viewer always sees the session's current messages, not a
point-in-time snapshot, and there is deliberately no revocation/expiry.
Feedback is hidden on the shared view — a viewer voting on a question they
didn't ask would pollute the owner's own triage signal.

A viewer who wants to keep asking questions can duplicate it (`POST
/api/chat/shared/{token}/duplicate`) into a brand-new session they own, with
every message copied over — they never write into the original conversation.
`ChatView.tsx` lands them straight into that new session via a `?session=`
query param it consumes and strips on mount.

This is also what closed a pre-existing ownership gap: before this feature,
`GET`/`DELETE /api/chat/sessions/{id}` did not check `user_id` at all — any
authenticated user who knew a `session_id` could already read or delete
someone else's conversation. Both routes are now scoped to `WHERE id = $1 AND
user_id = $2`.

### Persistence model (Lakebase)

- **`chat_sessions`** — one row per conversation thread; `name` derived from
  the first question (truncated); soft-deleted via `deleted`/`deleted_at`
  (never physically removed, so conversations stay traceable for audit);
  `share_token` set once the owner shares it (see "Sharing a conversation").
- **`chat_messages`** — one row per turn side (`role`: `user`/`assistant`),
  storing `content` (with citation markers baked in), `trace_id` (= the turn's
  `chat_turns.trace_id`), `sources_json` (`[{rank, title, url, n}]`), `status`
  (`ok`/`error`/`aborted`, only `ok` is shown), `division`, `question_lang`.
  Same soft-delete pattern as sessions. `tool_name`/`tool_query`/`tool_result`/
  `reasoning_steps` belong to the former Knowledge Assistant: no longer written
  since 2026-10-09, kept for the history of its turns.
- **`chat_turns`** / **`chat_retrieved_chunks`** — one row per turn (every step,
  its duration, the configuration it should have run with, the models that
  answered, `ok`/`degraded`/`error`/`aborted` + warning codes) and one row per
  passage retrieved for it, with its text. Filled by `services/turn_log.py`;
  failures and degraded steps also go to `errors`. Reference:
  `docs/lakebase_schema.md`.
- **`chat_feedbacks`** — thumbs up/down + optional comment per message.
- **`knowledge_base_metadata`** — single-row table holding the "documents as
  of" date shown in the UI toolbar; updated manually after each knowledge
  base refresh (`GET /config/knowledge-base-date`).

### Frontend

`client/src/components/chat/`: `ChatView.tsx` (message list, streaming
state, sidebar toggle, Share button), `ChatSidebar.tsx` (past sessions + "New
conversation" + division selector), `ChatMessage.tsx` (streamed rendering,
citation superscripts, source chips grouping multi-language variants of the
same document, feedback thumbs, Markdown/PDF export), `division.tsx`
(`DivisionSelector`, a 3-way ALL/AS/IS toggle). The read-only shared view
lives outside this folder, at `client/src/pages/SharedChatPage.tsx`
(route `/chat/shared/:token`) — it reuses `ChatMessage` with
`showFeedback={false}` and adds a "Duplicate into my conversations" button.

### Capability gating

Same pattern as Compare/Translate: `require_chat` dependency on every chat
endpoint → `get_capabilities()` reads `users.can_chat` from Lakebase (synced
by `utils/databricks_ops/user_capabilities/sync_user_capabilities.py`) → fails open (grants
access) if Lakebase is unreachable or the user isn't in the table yet.
`ChatPage.tsx` shows `AccessDenied` if `can_chat` is false, or a "coming
soon" placeholder if `CHAT_ENABLED` is false.

## Known issues / quirks

- **Fail-open capabilities**: if Lakebase is down or a user hasn't been
  synced, chat access is granted rather than denied — intentional, to avoid
  outages during DB issues.

## Knowledge base pipeline (chunking, embeddings, image descriptions)

The document knowledge base this chat retrieves from —
and that Compare's impact search also queries — is built offline by
`utils/parsing_pipeline/` (numbered notebooks `00`–`06`, run on Databricks,
not part of the live app):

- `3_Parse_Pipeline.py` parses PDF/Office documents into markdown text +
  extracted images, then chunks and writes the `chunks`/`processed_files`/
  `image_metadata` Delta tables that the Vector Search index syncs from.
- **Chunking** happens inside `3_Parse_Pipeline.py` itself
  (`utils.build_chunks_udf`) via a token-bounded markdown-structure
  splitter (`utils/parsing_pipeline/chunking.py`: 150 / 300 / 450 tokens,
  1,600 characters at most, 12 % overlap, tables of contents and front matter
  marked in `chunk_content_type`).
- `4_Describe_Images_LLM.py` sends each extracted image to the vision
  LLM (`databricks-gpt-5-mini`, `PARSING_LLM_ENDPOINT` in
  `utils/parsing_pipeline/config.py`) to describe tables/figures/diagrams;
  the description is folded into the surrounding chunk text.
  **Never pre-delete `image_metadata` by hand** — doing so has wiped existing
  descriptions before (2026-07-03 and 2026-07-06).
- **Embeddings**: Databricks-managed Vector Search embeddings
  (`databricks-qwen3-embedding-0-6b`, Delta Sync index `chunks_index` on the
  `chunks` table, created by `5_Sync_Vector_Indexes.py`) — powers retrieval for
  both this chat and Compare's impact search.

## Configuration (`app.yaml`)

| Var | Default | Purpose |
|---|---|---|
| `CHAT_ENABLED` | `true` | Feature flag |
| `CHAT_VSI_INDEX` | `dev_landingzone.qualibot.chunks_index` | The Vector Search index (per target in `target_env.json`) |
| `CHAT_VSI_LLM_ENDPOINT` / `CHAT_VSI_LLM_FALLBACK_ENDPOINTS` | `databricks-gpt-6-luna` / `databricks-gpt-5-6-luna` | Answer model and its fallbacks |
| `CHAT_VSI_REWRITE_ENDPOINT` / `CHAT_VSI_REWRITE_FALLBACK_ENDPOINTS` | `databricks-gpt-6-luna` / `databricks-gpt-5-6-luna` | Rewrite model and its fallbacks |
| `CHAT_VSI_ANSWER_MAX_TOKENS` / `CHAT_VSI_REWRITE_MAX_TOKENS` | `8000` / `2000` | Output caps (reasoning included) |
| `CHAT_TRANSLATE_BRIDGE_ENABLED` | `false` (`true` on every target) | Enable the non-FR/EN translation bridge |
| `CHAT_TRANSLATE_ENDPOINT` | `databricks-gpt-5-6-luna` | LLM endpoint for language detection + translation |
| `CHAT_MAX_HISTORY` | `10` | Messages replayed per turn; `0` disables trimming |
| `CAPS_TTL_S` | `600` | Shared with Compare/Translate — capability cache TTL (seconds) |

## Technical reference

Deep-dive material for anyone modifying this feature. The sections above explain
*why* things work the way they do; this is the exact *what* — routes, schemas,
and internals, file:line accurate as of 2026-08-17.

### Route inventory (`server/routers/chat.py`)

| Route | Function | Auth | Notes |
|---|---|---|---|
| `WS /api/chat/ws` | `chat_ws` (527) | Manual capability check (WebSockets can't return an HTTP 403) | The only chat transport — a WebSocket dodges Databricks Apps' reverse-proxy response buffering, which an SSE `X-Accel-Buffering: no` header can't defeat on this platform. |
| `POST /api/chat/feedback` | `chat_feedback` (714) | none (any authenticated user) | Inserts into `chat_feedbacks`; 400 on invalid `vote`, 503 with no DB pool. |
| `GET /api/chat/sessions` | `list_sessions` (756) | — | Returns `{sessions, available}`; `available:false` (not an error) when Lakebase is down. |
| `GET /api/chat/sessions/{id}` | `get_session` (801) | Scoped to `user_id` | 404 if missing **or not owned by the caller**; per-message `sources` deserialized from the `sources_json` blob column via the inner `_sources_for` helper. |
| `DELETE /api/chat/sessions/{id}` | `delete_session` (867) | Scoped to `user_id` | **Soft-delete** — sets `deleted`/`deleted_at` on both the session and its messages, never a hard `DELETE`. 404 if not owned by the caller. |
| `POST /api/chat/sessions/{id}/share` | `share_session` (908) | Owner only | Generates (or returns the existing) `share_token`; 404 if not owned by the caller. |
| `GET /api/chat/shared/{token}` | `get_shared_session` (946) | none (any authenticated user with the token) | Same shape as `get_session`, but looked up by `share_token` instead of `id`/ownership — the deliberate public-within-the-app read path. |
| `POST /api/chat/shared/{token}/duplicate` | `duplicate_shared_session` (1005) | none (any authenticated user) | Copies the shared session's messages into a brand-new session owned by the caller; returns `{session_id}`. |

WS message: `{messages, session_id?, division='ALL'}`. Pydantic request model:
`ChatFeedbackRequest{vote, comment?, message_id?, session_id?}`.

### Turn pipeline internals

`normalize_division` (`chat_vsi.py`) maps anything but `AS`/`IS` to `ALL`. `_trim_history` (142) keeps the last `CHAT_MAX_HISTORY`
messages, always preserving the latest question. `_with_today_date` (153) returns a
**copy** of the message list with `[Date: YYYY-MM-DD]` prepended to the last user
message only — the model never sees a stale date from history replay.

`_apply_citation_markers` (88) inserts `⟦n⟧` at each citation's character offset
**right-to-left**, so inserting one marker never invalidates the offsets of markers
still queued behind it. `_number_sources` (122) numbers only the sources actually
cited inline; prose-resurfaced sources (see `augment_sources` below) stay numberless.

`_save_turn` is the single persistence choke-point: it registers chat-only users into the shared `users` table via `upsert_user`
(so a user who only ever uses Chat still exists for capability sync), de-duplicates
sources by title, and — critically — **persists the user's question even when the
engine fails entirely**, with `status='error'` and the failure reason in `error_msg`.
Nothing about a turn is ever silently dropped; a failed turn is still fully traceable.
This is a deliberate, tested invariant (`test_save_turn_records_error_status`).

### `stream_chat_vsi` (`server/services/chat_vsi.py`) — the engine

Events yielded (SSE-shaped strings, relayed by the router over the WebSocket):
`: keepalive` (first byte, then while waiting), `response.output_text.delta` (answer text, `[n]`
markers removed), `sources` (cited documents + marker positions), `metadata` (`trace_id`,
`tool_name`, `usage`, `llm`, `llm_fallback`, `llm_attempts`), `error` (typed: `VectorSearchError`,
`RewriteError`, `InputError`, …), `[DONE]`.

**Search failures**: each Vector Search query is retried on 429 / 5xx / timeout (0.5 s, 1 s, 2 s +
jitter, `CHAT_VSI_SEARCH_RETRIES`); one failed query out of three is tolerated, and if the reranker
is refused the raw side still answers. A search that fails entirely ends the turn with a clear
error — answering without documents would break the "documents only" rule.

**Rewrite failures** fall back to the next model of the chain, then to the question alone.
**Answer failures** go through `chat_vsi_llm` (fallback model, cooldowns, continuation).
The translation bridge follows the same philosophy — fastText load failure, LLM call failure,
non-JSON LLM response and answer-translation failure all fall back to **passing the original
text through unchanged** rather than erroring the turn.

### Lakebase schema (`server/services/lakebase.py`)

```sql
CREATE TABLE chat_sessions (
    id            TEXT PRIMARY KEY,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    user_id       TEXT,
    workspace_id  TEXT,
    workspace_url TEXT,
    name          TEXT NOT NULL DEFAULT 'New conversation',
    deleted       BOOLEAN NOT NULL DEFAULT FALSE,   -- added by migration
    deleted_at    TIMESTAMPTZ,                       -- added by migration
    share_token   TEXT                               -- added by migration, unique when set
);

CREATE TABLE chat_messages (
    id              SERIAL PRIMARY KEY,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    session_id      TEXT REFERENCES chat_sessions(id) ON DELETE CASCADE,
    user_id         TEXT,
    workspace_id    TEXT,
    workspace_url   TEXT,
    role            TEXT NOT NULL CHECK (role IN ('user', 'assistant')),
    content         TEXT NOT NULL,
    trace_id        TEXT,             -- = chat_turns.trace_id (vsi-…)
    tool_name  TEXT, tool_query TEXT, tool_result TEXT, reasoning_steps TEXT,  -- KA only, no longer written
    endpoint_name   TEXT,             -- engine label vsi-all / vsi-as / vsi-is
    sources_json    TEXT,             -- JSON: [{rank, title, url, n}], replaces a since-removed chat_sources table
    status          TEXT NOT NULL DEFAULT 'ok',  -- 'ok' | 'error' | 'aborted'
    error_msg       TEXT,
    division        TEXT NOT NULL DEFAULT 'ALL',
    question_lang   TEXT,             -- ISO 639-1; empty when the bridge is disabled/undetected
    deleted         BOOLEAN NOT NULL DEFAULT FALSE,
    deleted_at      TIMESTAMPTZ
);
CREATE INDEX chat_messages_session_idx ON chat_messages(session_id);

CREATE TABLE chat_feedbacks (
    id            SERIAL PRIMARY KEY,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    message_id    INTEGER REFERENCES chat_messages(id) ON DELETE SET NULL,
    session_id    TEXT REFERENCES chat_sessions(id) ON DELETE SET NULL,
    user_id       TEXT,   workspace_id TEXT,   workspace_url TEXT,
    vote          TEXT NOT NULL CHECK (vote IN ('up', 'down')),
    comment       TEXT,
    resolved      BOOLEAN NOT NULL DEFAULT FALSE,   -- dashboard triage flag
    resolution_reason TEXT
);

CREATE TABLE knowledge_base_metadata (
    id              INTEGER PRIMARY KEY DEFAULT 1,
    documents_as_of DATE,
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT single_row CHECK (id = 1)
);
```

Notes: the feedback FKs are `ON DELETE SET NULL`, not `CASCADE` — a vote/comment
survives message or session deletion, orphaned but preserved for audit.
`chat_sources` (an earlier per-source table) was removed in favour of the single
`sources_json` blob column — don't go looking for it.

### Frontend (`client/src/components/chat/`)

- **`ChatView.tsx`** — `streamChat(...)` opens a raw `WebSocket` to `/api/chat/ws`,
  sends `{messages, session_id, division}` on `onopen`, and switches on
  `parsed.type` (`'delta'` / `'done'` / `'error'`) from `onmessage`. An
  `AbortController` wired to the Stop button closes the socket cleanly. A monotonic
  `selectSeqRef` counter guards session loads — clicking two history entries quickly
  can't let a slow, stale `GET /sessions/{id}` response clobber the session the user
  actually selected last. On `onDone`, the client swaps in the **server's** final
  content (not its own accumulated delta text), since only the server's copy carries
  the baked-in `⟦n⟧` citation markers. Division is a per-turn WebSocket field, not a
  session-scoped switch — changing division mid-conversation keeps the same message
  history and only changes the division filter of the *next* turn.
- **`ChatMessage.tsx`** — source-chip grouping strips a trailing language/locale
  suffix (`-FR`/`-EN`/`-GB`/…) from each source title to find its "canonical document
  key", so the FR/EN/CZ variants of the same document collapse into one chip with a
  language sub-badge row; the variant that was actually inline-cited leads that row.
  `ChatFeedback`'s submit is fire-and-forget from the UI's perspective — the vote is
  always shown as "submitted" even if the POST silently fails server-side, mirroring
  the backend's own best-effort persistence philosophy.
- **`division.tsx`** — `DivisionSelector`, a 3-way `ALL`/`AS`/`IS` toggle;
  `stripDivision`/`DIVISION_PREFIX_RE` mirror the server's legacy `[Division: …]`
  prefix-stripping so old stored messages still render cleanly.

### Capability gating — exact logic

```python
async def require_chat(request: Request) -> None:
    caps = await get_capabilities(request)
    if not caps['can_chat']:
        raise HTTPException(status_code=403, detail='Chat access not granted')
```

`get_capabilities` fails **open** (grants all three capabilities) in every failure or
absence scenario except one: no Lakebase pool, a DB query exception, or the forwarded
user simply not being in the `users` table yet all grant access. The **only**
fail-closed path is the `x-forwarded-user` header being entirely absent (the request
isn't coming through the expected auth proxy at all). Results are cached per user for
`CAPS_TTL_S` seconds (default 600). The WS route (`chat_ws`) can't rely on
`Depends(...)` — it calls `get_capabilities` manually inside a try/except and closes
the socket with a JSON error frame if `can_chat` is false.

### Grep-able log lines

| Log line (substring) | Where | Meaning |
|---|---|---|
| `chat_vsi: division=%s index=%s fr_query=%r en_query=%r passages=%d documents=%d` | `chat_vsi.py` | One line per turn: what was searched and how much was found |
| `chat_vsi_llm:` | `chat_vsi_llm.py` | Any answer-model incident (fallback, retry, continuation) |
| `Capabilities: ... granting all (fail-open)` (3 variants) | `user.py` | Distinguishes *why* fail-open triggered: Lakebase down / user not synced / DB query failed |
| `translation_bridge: ... using original text: %s` (4 variants) | `translation_bridge.py` | Any bridge failure mode falling back to untranslated passthrough |
| `Chat DB save failed: %s` | `chat.py` (`_save_turn`) | Persistence failure, swallowed — the turn itself still completed |

### Tests

`tests/test_chat_route.py` (the WebSocket turn, engine mocked), `tests/test_chat_vsi.py` (the engine:
citation parser on 21 real answers, search, filters, prompt, errors), `tests/test_chat_vsi_llm.py`
(fallbacks, retries, continuation, queue). `tests/test_chat.py` covers session CRUD (including confirming
soft-delete, not hard delete, and that both routes 404 rather than leak/delete
a session the caller doesn't own), feedback validation, the division/history helper
functions, `_save_turn`'s user-registration and error-status behavior, `_trim_history`'s
latest-question guarantee, and both `get_capabilities` fail-open triggers plus an
end-to-end 403 check on `require_chat`. Share/duplicate coverage: token generation is
idempotent on re-share, `share_session` 404s for a non-owner, `get_shared_session` skips
ownership entirely, and `duplicate_shared_session` copies every message under the
*viewer's* `user_id`, not the original owner's. No dedicated test file was found for
`translation_bridge.py`'s LLM-calling paths — its fail-safe behavior is exercised only
indirectly through the chat route tests that happen to run with the bridge disabled.

See the root [README.md](../../../../README.md) for deploy/environment/local-testing instructions shared across all three features.
