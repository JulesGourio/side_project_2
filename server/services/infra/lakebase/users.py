"""Registration of identities in the shared `users` table."""


async def upsert_user(
    conn,
    *,
    user_id: str,
    workspace_id: str | None = None,
    email: str | None = None,
    workspace_url: str | None = None,
    display_name: str | None = None,
) -> None:
    """Register (or update) an identity in the shared ``users`` table, for the compare and the chat flows alike.

    Every identity that uses the app gets exactly one row, chat-only users included, so that the user ids of
    chat sessions and messages always match a row.

    Deduplication key is the email (stable across workspaces); falls back to
    user_id when no email could be resolved.  Must run inside an existing
    connection/transaction (takes an acquired `conn`).

    :param conn: open database connection, inside a transaction
    :param user_id: numeric Databricks user id
    :param workspace_id: workspace id of the user, None when unknown
    :param email: email of the user, None when it could not be resolved
    :param workspace_url: URL of the user's workspace, None when unknown
    :param display_name: name of the user, None when unknown; an existing name is kept when this is None
    """
    if not user_id:
        return
    if email:
        # A user_id known without an email gets its email now: the upsert below would otherwise conflict on user_id.
        await conn.execute(
            'UPDATE users SET email = $2, display_name = COALESCE($3, display_name), updated_at = NOW()'
            ' WHERE user_id = $1 AND email IS NULL',
            user_id, email, display_name,
        )
        # Upsert on email — handles cross-workspace dedup (same person, new workspace)
        await conn.execute(
            '''
            INSERT INTO users (user_id, workspace_id, email, workspace_url, display_name)
            VALUES ($1, $2, $3, $4, $5)
            ON CONFLICT (email) WHERE email IS NOT NULL DO UPDATE SET
                user_id       = EXCLUDED.user_id,
                workspace_id  = EXCLUDED.workspace_id,
                workspace_url = COALESCE(EXCLUDED.workspace_url, users.workspace_url),
                display_name  = COALESCE(EXCLUDED.display_name, users.display_name),
                updated_at    = NOW()
            ''',
            user_id, workspace_id, email, workspace_url, display_name,
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
