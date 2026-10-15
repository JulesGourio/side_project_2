# routers/chat

The chat API: the WebSocket route that answers one turn, the feedback route, and the conversation routes (list, read,
delete, share, duplicate).

Parent: [routers](../README.md). The answering logic is in [services/chat](../../services/chat/README.md), and the
engine in [services/chat/engine](../../services/chat/engine/README.md).

## Files

| Module | Content |
|---|---|
| `__init__.py` | Mounts `ws` and `sessions`. Re-exports the helpers that `utils/evaluation` uses to rebuild what a turn feeds the engine |
| `ws.py` | `/chat/ws`: one turn, from the access check to the turn log |
| `sessions.py` | `POST /chat/feedback`, `GET /chat/sessions`, `GET` and `DELETE /chat/sessions/{id}`, `POST /chat/sessions/{id}/share`, `GET /chat/shared/{token}`, `POST /chat/shared/{token}/duplicate` |
| `helpers.py` | Pure functions: the division prefix, the window of past messages (`CHAT_MAX_HISTORY`), the `[Date: …]` prefix, the citation markers, the stored source chips |
| `persistence.py` | `_save_turn`: the session row, the user and assistant messages, and their source chips (JSON) |

## One turn

1. `ws.py` accepts the socket, checks `CHAT_ENABLED`, reads the host and token with `get_credentials`, and checks the
   `can_chat` capability.
2. The client sends `messages`, `session_id` and `division`. The messages are trimmed to the window, and
   `[Date: YYYY-MM-DD]` is put before the last user message.
3. `stream_chat` yields events. Text deltas, `status` events and keepalive `ping` messages go to the browser as they
   arrive.
4. At the end of the stream, `⟦n⟧` citation markers are placed, catalog sources are added (`augment_sources`), the
   answer is saved, and `done` is sent with the message id and the sources.
5. In the `finally` block the turn log is closed, exported to MLflow and written to Lakebase. This also happens when
   the browser has left.

## Rules

- The browser gets the fixed failure message only. The cause is in the turn log (`chat_turns`, with its detail in
  `app_errors`).
- Failed and aborted turns are saved with their status. Only `ok` messages appear in the session list, in a session
  read and in a shared conversation.
- Deleting a session is soft: its rows are flagged, not deleted.
- A session has one share token. It is created on the first share and reused afterwards, so a link keeps working.
  Anyone with the link and the chat capability reads the conversation. Duplicating it copies the messages into a
  session owned by the caller.
- A vote is stored in `chat_feedbacks`. When the turn has an MLflow trace, the vote is also sent to that trace from a
  background thread.
- The turn log is written after `done` has been sent: logging does not delay the answer.

## See also

- [routers](../README.md): the other route modules.
- [services/chat](../../services/chat/README.md) and [services/chat/engine](../../services/chat/engine/README.md).
