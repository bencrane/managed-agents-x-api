"""CRUD helpers for the invocation_log table.

Backs the idempotency contract of POST /internal/agents/{agent_id}/invoke.
When a caller supplies `idempotency_key`, we record `(agent_id, session_id,
response)` keyed on that string and return the stored response on any
subsequent call with the same key — instead of firing a second Anthropic
session.
"""

from __future__ import annotations

from typing import Any

from psycopg.types.json import Jsonb

from app.db import connect


def get_response(idempotency_key: str) -> dict[str, Any] | None:
    """Return the stored response JSON for this key, or None if unseen."""
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            "select response from invocation_log where idempotency_key = %s",
            (idempotency_key,),
        )
        row = cur.fetchone()
    if not row:
        return None
    return row[0]


def insert(
    idempotency_key: str,
    session_id: str,
    agent_id: str,
    response: dict[str, Any],
) -> bool:
    """Insert a log row.

    Returns True if the row was inserted, False if a row with the same
    idempotency_key already existed (race condition). Caller can choose to
    treat a False as "we just duplicated an Anthropic session" or silently
    proceed — V1 behaviour is to proceed and return the caller's own result.
    """
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                insert into invocation_log
                    (idempotency_key, session_id, agent_id, response)
                values (%s, %s, %s, %s)
                on conflict (idempotency_key) do nothing
                """,
                (idempotency_key, session_id, agent_id, Jsonb(response)),
            )
            inserted = cur.rowcount > 0
        conn.commit()
    return inserted
