# Chat

Chatbot interface: one question, one answer written from the document base, with
clickable sources. This folder holds the views, the answer stream handling and the
conversation management. The server side (WebSocket route, search engine,
persistence) lives in `server/routers/chat/` and `server/services/chat/`.

## Files

| File | Role |
|---|---|
| `ChatView.tsx` | Layout: sidebar, messages, input, sharing; opens a past conversation from `?session=` |
| `useChatConversation.ts` | State of one conversation: messages, running turn, loading, stop, sharing |
| `useChatSessions.ts` | Conversation list of the user (`/api/chat/sessions`), deletion |
| `chatStream.ts` | WebSocket connection of one turn, frame handling, `TIRED_MESSAGE` text |
| `message.ts` | `Message`, `Source` and `Feedback` types, version label |
| `division.tsx` | `Division` type (ALL / AS / IS), selector, removal of the `[Division: …]` prefix |
| `ChatSidebar.tsx` | Conversation list with relative dates, new conversation, division choice |
| `ChatInput.tsx` | Input area, send button, stop button |
| `ChatMessage.tsx` | One message bubble: rendered text, sources, user feedback, export |
| `ChatSources.tsx` | Chips for the cited documents; language variants grouped |
| `ChatFeedback.tsx` | Thumbs up / down vote with an optional comment; read-only version |
| `DownloadMenu.tsx` | Export of the answer or the conversation as Markdown or PDF |
| `chatExport.ts` | Builds the Markdown and the PDF sections from the messages |
| `WelcomeState.tsx` | Start screen of an empty conversation, with suggested questions |

Pages that use this folder:
[`ChatPage.tsx`](../../pages/ChatPage.tsx) (route `/chat`) and
[`SharedChatPage.tsx`](../../pages/SharedChatPage.tsx) (route `/chat/shared/:token`,
read-only).

## Turn flow

1. `useChatConversation.send` appends the question and an answer bubble that is
   still streaming, then sends the whole conversation history to `streamChat`.
2. `streamChat` opens `/api/chat/ws` (`wss:` or `ws:`, following the page protocol)
   and sends one JSON frame: `{ "messages", "session_id", "division" }`.
3. The server replies with JSON frames, handled in `chatStream.ts`:

   | Frame | Effect on the client |
   |---|---|
   | `{ "type": "delta", "delta": "…" }` | The text is appended to the bubble |
   | `{ "type": "status", "status": "…" }` | Updates the waiting label; `still_searching` shows "Still searching" |
   | `{ "type": "ping" }` | Keepalive during long waits; not handled, ignored |
   | `{ "type": "done", "session_id", "message_id", "content", "sources" }` | The final text and sources replace the streamed text; `onTurnSaved` reloads the conversation list |
   | `{ "type": "error", "error": "…" }` | The bubble shows the received text, or `TIRED_MESSAGE` when it is empty |

4. A turn always ends with exactly one outcome: `done`, `error`, or the stop
   requested by the user. A socket that closes without one of these outcomes shows
   `TIRED_MESSAGE` instead of staying on "Thinking".
5. A socket that fails **before** the question is sent is opened again up to two
   times (after 1 s, then 2 s). After the question is sent, there is no retry.

On `done`, the streamed text is replaced by the content stored by the server, which
contains the `⟦n⟧` citation markers.

## Rules

- **One error message.** The user sees `TIRED_MESSAGE`, identical to the server text.
  The technical cause is logged on the server and is never shown.
- **Division.** The ALL / AS / IS choice of the sidebar is sent with every turn. The
  server filters the document search on the `division` column of the index, so the
  scope does not depend on the question. `stripDivision` removes an optional
  `[Division: …]` prefix before a question is displayed.
- **Citations.** In the text, each `⟦n⟧` marker becomes a clickable superscript that
  links to source `n` (`MarkdownRenderer`; the `citations` table is built by
  `ChatMessage` from the numbered sources). Exports replace the markers with `[n]`.
- **Sources.** One chip per document. A source cited in the text carries its number;
  a source only mentioned in prose does not. A chip with a link opens the document in
  a new tab. Without a link, it is a plain badge whose tooltip gives the full
  reference.
- **Language variants.** REFs that differ only by a language suffix (`_FR`, `_GB`,
  `_EN`, …) are grouped into one chip, with one flag per language (`canonicalDocKey`).
  The label is the shared code, without the suffix. The display order does not mark
  any language as the default.
- **Source details.** The tooltip shows the title, revision and date of the document,
  supplied by the server (`Source.doc_title`, `revision`, `doc_date`).
- **Stop.** The stop button ("Stop generation") closes the socket. The bubble keeps
  the text already received, without an error message. The server records the turn as
  interrupted.
- **New conversation during a turn.** It interrupts the running turn.
- **Sharing.** The share button only works on a conversation already saved on the
  server. It creates the link `/chat/shared/<token>` and copies it to the clipboard.
- **Shared view.** Read-only. The reader sees the owner's vote but cannot vote.
  "Duplicate into my conversations" creates a copy in the reader's conversations and opens it
  (`/chat?session=…`). The shared conversation is not modified.
- **User feedback.** One vote per message, sent once, with an optional comment
  (`/api/chat/feedback`). After sending, the block shows a confirmation.
- **Export.** Answer only or whole conversation, as Markdown or PDF. The PDF is
  rendered by the server (`/api/export/pdf`, [`lib/exportPdf.ts`](../../lib/exportPdf.ts));
  the Markdown is built on the client.
- **Conversation list.** Each conversation shows its relative date ("Today",
  "Yesterday", "N days ago", then a short date), computed in calendar days in the
  user's time zone. The list appears only when the server reports that history is
  available.
- **Document date.** `ChatView` shows the update date of the documents
  (`/api/config/knowledge-base-date`) next to the title.

## Access

- `ChatPage` shows the access-denied panel when the user has no chat right
  (`can_chat`), and a waiting screen when chat is disabled in the configuration
  (`chat.enabled`).
- On the WebSocket, the server replies with an `error` frame (`Chat access not
  granted` or `Chat is disabled`) and closes the socket. The HTTP routes return 403.

## Routes used

| Call | Use |
|---|---|
| `WS /api/chat/ws` | One chat turn (`chatStream.ts`) |
| `GET /api/chat/sessions` | Conversation list (`useChatSessions`) |
| `GET /api/chat/sessions/{id}` | Loads one conversation |
| `DELETE /api/chat/sessions/{id}` | Deletes one conversation |
| `POST /api/chat/sessions/{id}/share` | Creates or returns the share token |
| `GET /api/chat/shared/{token}` | Shared view (`SharedChatPage`) |
| `POST /api/chat/shared/{token}/duplicate` | Copies a shared conversation |
| `POST /api/chat/feedback` | Vote on a message |
| `GET /api/config/knowledge-base-date` | Document date |
| `POST /api/export/pdf` | PDF export |

The matching database tables are described in
[`docs/lakebase_schema.md`](../../../../docs/lakebase_schema.md).
