"""Secret contract for the application.

This file is the canonical list of environment variables the app expects.
Values are injected at runtime by `doppler run --` (via the Doppler CLI in
the container) from the Doppler project `managed-agents-x`, config `prd`.

Design rule: every field must be tolerant of being missing at import time.
The app must boot and `/health` must return 200 even if Doppler is
unreachable or a variable is unset. Required secrets are validated lazily
at the call site that actually needs them (see `require()`).

Notes on a few specific fields:
- `mags_auth_token` is the inbound bearer token callers present when reaching
  into this service (future `/agents*` surface, `/admin/status`, etc.). It's
  the one secret the authenticated surface treats as required. Paired on the
  caller side with `MAG_API_URL` (the caller's pointer at this service).
- `anthropic_managed_agents_api_key` **does** belong to this project
  (opposite of `ops-engine-x`, where it is deliberately absent).
  `managed-agents-x` is the designated owner of the Anthropic
  managed-agents product surface (agent CRUD, system-prompt versioning,
  sync), so the Anthropic API key is expected to live in this project's
  Doppler config. Add `ANTHROPIC_MANAGED_AGENTS_API_KEY` to the `prd`
  config before exercising any code path that calls Anthropic.
- `mags_db_url_pooled` is the Postgres DSN the app uses at runtime
  (Supabase transaction pooler). `mags_db_url_direct` is the direct
  connection, exposed as a separate setting for future migration scripts
  and not read by the app at runtime.
- The remaining `mags_supabase_*` fields are reserved for future use; the
  skeleton does not read them yet.
"""

from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        case_sensitive=False,
        extra="ignore",
    )

    mags_auth_token: str | None = None
    anthropic_managed_agents_api_key: str | None = None

    mags_db_url_pooled: str | None = None
    mags_db_url_direct: str | None = None

    mags_supabase_url: str | None = None
    mags_supabase_service_role_key: str | None = None
    mags_supabase_anon_key: str | None = None
    mags_supabase_publishable_key: str | None = None
    mags_supabase_project_ref: str | None = None


settings = Settings()


class MissingSecretError(RuntimeError):
    """Raised when a secret required by a code path is not configured."""


def require(name: str) -> str:
    """Fetch a required secret by attribute name, raising a clear error if unset.

    Use this at the call site of any feature that genuinely needs the secret,
    e.g. `token = require("mags_auth_token")`. This keeps startup tolerant
    while failing loudly and clearly when a feature is exercised without its
    required configuration.
    """
    value = getattr(settings, name, None)
    if not value:
        raise MissingSecretError(
            f"Required secret '{name.upper()}' is not set. "
            "Confirm it exists in Doppler (project: managed-agents-x, "
            "config: prd) and that DOPPLER_TOKEN is valid."
        )
    return value
