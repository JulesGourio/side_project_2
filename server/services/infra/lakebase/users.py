"""Registration of identities in the shared `users` table."""


async def upsert_user(
    conn,
    *,
    user_id: str,
    workspace_id: str | None = None,
    email: str | None = None,
    workspace_url: str | None = None,
) -> None:
    """Register (or update) an identity in the shared `users` table.

    Used by BOTH the document-comparison flow and the chat flow, so that every
    identity that interacts with the app is registered exactly once — whether
    the user compared a document or only chatted.  Without this, chat-only users
    end up with a user_id in chat_sessions/chat_messages that has no matching
    row in `users`.

    Deduplication key is the email (stable across workspaces); falls back to
    user_id when no email could be resolved.  Must run inside an existing
    connection/transaction (takes an acquired `conn`).
    """
    if not user_id:
        return
    if email:
        # A user_id known without an email gets its email now: the upsert below would otherwise conflict on user_id.
        await conn.execute(
            'UPDATE users SET email = $2, updated_at = NOW()'
            ' WHERE user_id = $1 AND email IS NULL',
            user_id, email,
        )
        # Upsert on email — handles cross-workspace dedup (same person, new workspace)
        await conn.execute(
            '''
            INSERT INTO users (user_id, workspace_id, email, workspace_url)
            VALUES ($1, $2, $3, $4)
            ON CONFLICT (email) WHERE email IS NOT NULL DO UPDATE SET
                user_id       = EXCLUDED.user_id,
                workspace_id  = EXCLUDED.workspace_id,
                workspace_url = COALESCE(EXCLUDED.workspace_url, users.workspace_url),
                updated_at    = NOW()
            ''',
            user_id, workspace_id, email, workspace_url,
        )
    else:
        # No email resolved — fall back to user_id deduplication
        await conn.execute(
            '''
            INSERT INTO users (user_id, workspace_id, email, workspace_url)
            VALUES ($1, $2, $3, $4)
            ON CONFLICT (user_id) DO UPDATE SET
                workspace_id  = COALESCE(EXCLUDED.workspace_id, users.workspace_id),
                workspace_url = COALESCE(EXCLUDED.workspace_url, users.workspace_url),
                updated_at    = NOW()
            ''',
            user_id, workspace_id, None, workspace_url,
        )
