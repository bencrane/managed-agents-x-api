"""Database connection helper.

Uses MAGS_DB_URL_POOLED (Supabase transaction pooler DSN) from Doppler.
MAGS_DB_URL_DIRECT is intentionally not read here — it is reserved for
future migration scripts. Keeps psycopg usage explicit so the app stays
Supabase-port-agnostic.
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator

import psycopg

from app.config import require


@contextmanager
def connect() -> Iterator[psycopg.Connection]:
    dsn = require("mags_db_url_pooled")
    with psycopg.connect(dsn, autocommit=False) as conn:
        yield conn
