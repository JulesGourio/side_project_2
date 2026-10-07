# Chatbot (Knowledge Assistant)

A RAG-style conversational assistant over the company's document knowledge
base, with division scoping (ALL / AS / IS), inline citations, and an
optional non-French/English translation bridge.

The system prompt itself lives on the Databricks serving endpoint, not in
this repo — see [`chat_system_prompt.md`](chat_system_prompt.md) for the
current prompt text and the division-selector integration.

## How it works

### End-to-end flow (`POST /api/chat/stream`, `/api/chat/ws`)

Both the SSE and WebSocket endpoints (`server/routers/chat.py`) run the same
core flow: **route by division → trim history → inject today's date → call
the Knowledge Assistant → assemble citations → optional translation bridge →
persist the turn.**

1. **Division routing** (`_endpoint_for_division`) — the user picks
   ALL/AS/IS from the sidebar; each maps to its own Knowledge Assistant
   endpoint (`CHAT_ENDPOINT_ALL` / `_AS` / `_IS`, falling back to
   `CHAT_ENDPOINT` when unset). Routing by endpoint — rather than injecting
   a "[Division: …]" directive into the question — is what guarantees
   scoping: a single-source KA can only ever return that division's
   documents and never fans out into cross-source parallel sub-queries.
2. **History trimming** (`_trim_history`, `CHAT_MAX_HISTORY`, default 10) —
   bounds prompt growth as a conversation lengthens; the latest question is
   always kept.
3. **Date injection** (`_with_today_date`) — prepends `[Date: YYYY-MM-DD]`
   to the last user message only, so date-relative questions ("documents
   published this year") resolve correctly.
4. **Streaming call** (`server/services/streaming.py::stream_chat`) — opens
   an httpx SSE connection, auto-detecting agent vs. chat-completion format
   per endpoint (cached after the first successful probe). Emits:
   - `response.output_text.delta` — answer tokens
   - `response.output_text.annotation.added` — inline `url_citation`
     annotations, accumulated with their character offset in the answer
   - `response.output_item.done` / `response.completed` — the MLflow trace
     (retrieval spans, tool name/query/result, reasoning steps)
5. **Citation assembly** — citations are numbered in the order their
   annotations arrive; markers use `⟦n⟧` (U+27E6/U+27E7, essentially never
   present in source documents) inserted at each citation's character
   offset, right-to-left so earlier offsets stay valid. Baked into the
   stored `content` so reloaded conversations show the same inline
   citations without re-computing anything.
6. **Translation bridge** (optional, see below).
7. **Persistence** — one `chat_sessions` row + two `chat_messages` rows
   (user + assistant) per turn, including failed turns (status `'error'`),
   so a question and its failure reason are always traceable.

### Translation bridge (`server/services/translation_bridge.py`)

Gated by `CHAT_TRANSLATE_BRIDGE_ENABLED` (default `false`). When enabled:

- A cheap local **fastText** language-ID pass screens the question; if
  confidence > 65% and the language isn't French/English, an LLM call
  (`CHAT_TRANSLATE_ENDPOINT`) confirms the language and produces an English
  translation in one request.
- The English translation (not the original) is what reaches the Knowledge
  Assistant — the corpus is almost entirely French/English, so retrieval on
  a third language's raw text only matches the sparse same-language slice.
- The final answer is translated back to the user's language in one pass
  before streaming starts (the client shows "Thinking" until that
  translation completes — raw English is never shown). Markdown, `REF`
  codes, dates, and `⟦n⟧` citation markers are instructed to pass through
  unchanged.
- As a safeguard, even when the bridge doesn't trigger (question already
  FR/EN), the answer's language is checked and re-translated if it doesn't
  match the question's — guards against the Knowledge Assistant answering
  in the wrong language mid-conversation.
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
  storing `content` (with citation markers baked in), `trace_id`,
  `tool_name`/`tool_query`/`tool_result`, `reasoning_steps`, `sources_json`
  (`[{rank, title, url, n}]`), `status` (`ok`/`error`), `division`,
  `question_lang`. Same soft-delete pattern as sessions.
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

- **Vector Search request-ID collision**: the older single-KA design (no
  division scoping) triggered parallel sub-queries that reused the same
  Databricks request ID, surfacing as `"Request id …-0 already running"` in
  the KA's reasoning trace. Division routing (endpoint-per-division) avoids
  this for scoped questions; if it recurs, it'll show up as a `"Vector
  search failed"` / `"already running"` string inside
  `reasoning_summary_text.delta`, logged as a warning in
  `stream_chat`.
- **Fail-open capabilities**: if Lakebase is down or a user hasn't been
  synced, chat access is granted rather than denied — intentional, to avoid
  outages during DB issues.

## Knowledge base pipeline (chunking, embeddings, image descriptions)

The document knowledge base this chat's Knowledge Assistant retrieves from —
and that Compare's impact search also queries — is built offline by
`utils/parsing_pipeline/` (numbered notebooks `00`–`06`, run on Databricks,
not part of the live app):

- `3_Parse_Pipeline_v2.py` parses PDF/Office documents into markdown text +
  extracted images, then chunks and writes the `chunks`/`processed_files`/
  `image_metadata` Delta tables that the Vector Search index syncs from.
- **Chunking** happens inside `3_Parse_Pipeline_v2.py` itself
  (`utils.build_chunks_udf`) via a token-bounded markdown-structure
  splitter — not a full Docling document chunker. `Test_Chunking.py` is a
  standalone benchmark notebook comparing that approach against Docling's
  `HybridChunker` and a semantic (Qwen-embedding) split, but neither
  alternative is wired into production: `HybridChunker` needs a live Docling
  document object, which isn't available at the point production chunking
  runs (only the parsed markdown text survives to that stage).
- `4_Describe_Images_LLM_v2.py` sends each extracted image to the vision
  LLM (`databricks-gpt-5-mini`, `PARSING_LLM_ENDPOINT` in
  `utils/parsing_pipeline/config.py`) to describe tables/figures/diagrams;
  the description is folded into the surrounding chunk text.
  `Rebuild_Image_Metadata.py` can regenerate image descriptions without a
  full re-parse (**never pre-delete `image_metadata` by hand** — doing so
  before running this notebook has wiped existing descriptions before,
  2026-07-03 and 2026-07-06; let the notebook's own logic handle it).
- **Embeddings**: Databricks-managed Vector Search embeddings (Delta Sync
  index) — powers retrieval for both this chat and Compare's impact search.
  Index-level config, not in this repo.

## Configuration (`app.yaml`)

| Var | Default | Purpose |
|---|---|---|
| `CHAT_ENABLED` | `true` | Feature flag |
| `CHAT_ENDPOINT` | — | Fallback Knowledge Assistant endpoint |
| `CHAT_ENDPOINT_ALL` / `_AS` / `_IS` | empty | Per-division endpoints; empty falls back to `CHAT_ENDPOINT` |
| `CHAT_TRANSLATE_BRIDGE_ENABLED` | `false` | Enable the non-FR/EN translation bridge |
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
| `POST /api/chat/stream` | `chat_stream` (346) | `Depends(require_chat)` | SSE. Exists and is unit-tested, but the current frontend (`ChatView.tsx`) talks to the WS endpoint exclusively — this is the fallback/non-browser transport, not dead code, but not what a browser session actually uses today. |
| `WS /api/chat/ws` | `chat_ws` (527) | Manual capability check (WebSockets can't return an HTTP 403) | The actual transport `ChatView.tsx` uses — chosen specifically to dodge Databricks Apps' own reverse-proxy response buffering, which the SSE route's `X-Accel-Buffering: no` header can't defeat on this platform. |
| `POST /api/chat/feedback` | `chat_feedback` (714) | none (any authenticated user) | Inserts into `chat_feedbacks`; 400 on invalid `vote`, 503 with no DB pool. |
| `GET /api/chat/sessions` | `list_sessions` (756) | — | Returns `{sessions, available}`; `available:false` (not an error) when Lakebase is down. |
| `GET /api/chat/sessions/{id}` | `get_session` (801) | Scoped to `user_id` | 404 if missing **or not owned by the caller**; per-message `sources` deserialized from the `sources_json` blob column via the inner `_sources_for` helper. |
| `DELETE /api/chat/sessions/{id}` | `delete_session` (867) | Scoped to `user_id` | **Soft-delete** — sets `deleted`/`deleted_at` on both the session and its messages, never a hard `DELETE`. 404 if not owned by the caller. |
| `POST /api/chat/sessions/{id}/share` | `share_session` (908) | Owner only | Generates (or returns the existing) `share_token`; 404 if not owned by the caller. |
| `GET /api/chat/shared/{token}` | `get_shared_session` (946) | none (any authenticated user with the token) | Same shape as `get_session`, but looked up by `share_token` instead of `id`/ownership — the deliberate public-within-the-app read path. |
| `POST /api/chat/shared/{token}/duplicate` | `duplicate_shared_session` (1005) | none (any authenticated user) | Copies the shared session's messages into a brand-new session owned by the caller; returns `{session_id}`. |

Pydantic request models: `ChatMessageIn{role, content}`, `ChatRequest{messages, session_id?, division='ALL'}`,
`ChatFeedbackRequest{vote, comment?, message_id?, session_id?}`.

### Turn pipeline internals

`_endpoint_for_division` (44) maps `ALL`/`AS`/`IS` to `CHAT_ENDPOINT_ALL`/`_AS`/`_IS`,
falling back to `CHAT_ENDPOINT`. `_trim_history` (142) keeps the last `CHAT_MAX_HISTORY`
messages, always preserving the latest question. `_with_today_date` (153) returns a
**copy** of the message list with `[Date: YYYY-MM-DD]` prepended to the last user
message only — the KA never sees a stale date from history replay.

`_apply_citation_markers` (88) inserts `⟦n⟧` at each citation's character offset
**right-to-left**, so inserting one marker never invalidates the offsets of markers
still queued behind it. `_number_sources` (122) numbers only the sources actually
cited inline; prose-resurfaced sources (see `augment_sources` below) stay numberless.

`_save_turn` (243) is the single persistence choke-point for both the SSE and WS
routes: it registers chat-only users into the shared `users` table via `upsert_user`
(so a user who only ever uses Chat still exists for capability sync), de-duplicates
sources by title, and — critically — **persists the user's question even when the KA
call fails entirely**, with `status='error'` and the failure reason in `error_msg`.
Nothing about a turn is ever silently dropped; a failed turn is still fully traceable.
This is a deliberate, tested invariant (`test_save_turn_records_error_status`).

### `stream_chat` (`server/services/streaming.py`) — the KA call itself

**Format auto-detection**: the KA endpoint accepts either an `'agent'`-shaped payload
(`{input, stream:True, databricks_options:{return_trace:True}}`) or a `'chat'`-shaped
one (`{messages, stream:True}`); `stream_chat` tries both once per endpoint and caches
the winner in a process-local `_endpoint_format_cache` dict — every later call to that
endpoint skips straight to the format that worked. This cache has no TTL/eviction; it
lives for the process lifetime.

**Keepalive/429 handling**: a producer task reads the HTTP response into an
`asyncio.Queue`; the consumer loop yields `: keepalive\n\n` on a 15s read timeout
without breaking the connection — this is what keeps a multi-minute KA turn alive
through any proxy that would otherwise time out an idle stream. A `429` response is
handled the same way during the backoff wait (`COMPARE_ANALYSIS_RETRIES`, shared with
Compare), so the client-visible connection never drops during a rate-limit retry.

**Citation offset heuristic**: the KA's `response.output_text.annotation.added` event
carries no `start_index`/`end_index` — the annotation simply arrives in the stream
right after the cited span. `stream_chat` uses the running length of the
already-emitted answer text as the citation's position. This is inherently fragile to
any future change in how the KA batches/orders its output relative to annotations.

**MLflow trace harvesting**: on `response.output_item.done` for a `function_call`
item, captures `tool_name`/`tool_query`. On `response.completed`, walks
`databricks_output.trace.data.spans`, pulling `RETRIEVER`-type spans into `sources`
(with chunk content + score) and the first `TOOL`-type span's output into
`tool_result`. `reasoning_summary_text.delta` chunks accumulate into
`reasoning_steps`; any reasoning delta containing `"Vector search failed"` or
`"already running"` is logged as a warning — this is the exact log line to grep for
when chasing the Vector Search request-ID collision described above.

**Every LLM/network failure mode degrades to a safe default rather than blocking the
turn**: a timeout yields a typed `error` SSE chunk (not an exception); if neither
payload format is accepted, the generator yields a `FormatError` chunk instead of
raising. The translation bridge (`translation_bridge.py`) follows the same philosophy
end to end — fastText load failure, LLM call failure, non-JSON LLM response, and
answer-translation failure all fall back to **passing the original text through
unchanged** rather than erroring the turn.

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
    trace_id        TEXT,             tool_name  TEXT,   tool_query TEXT,
    tool_result     TEXT,             reasoning_steps TEXT,
    endpoint_name   TEXT,
    sources_json    TEXT,             -- JSON: [{rank, title, url, n}], replaces a since-removed chat_sources table
    status          TEXT NOT NULL DEFAULT 'ok',  -- 'ok' | 'error'
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
  history and only changes which KA endpoint the *next* turn routes to.
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
| `stream_chat KA retrieval error on %s: %s` | `streaming.py` | The Vector Search request-ID collision — see "Known issues" above |
| `stream_chat: %s format, endpoint=%s` | `streaming.py` | Confirms which payload format won auto-detection for that endpoint |
| `Capabilities: ... granting all (fail-open)` (3 variants) | `user.py` | Distinguishes *why* fail-open triggered: Lakebase down / user not synced / DB query failed |
| `translation_bridge: ... using original text: %s` (4 variants) | `translation_bridge.py` | Any bridge failure mode falling back to untranslated passthrough |
| `Chat DB save failed: %s` | `chat.py` (`_save_turn`) | Persistence failure, swallowed — the turn itself still completed |

### Tests (`tests/test_chat.py`)

Covers: SSE happy path + error forwarding, session CRUD (including confirming
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
