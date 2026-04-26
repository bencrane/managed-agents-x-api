"""managed-agents-x — FastAPI entrypoint.

Product surface for managed agents. Wraps Anthropic's managed-agents API and
adds per-agent default config (environment_id + vault_ids + task_instruction)
plus a DB-backed mirror of agent state with version history.

The app must boot successfully with zero secrets configured. Any feature that
requires a secret reads it lazily via `app.config.require(...)` (or via the
relevant FastAPI `Depends()`), so `/health` stays green even when Doppler is
unreachable or individual secrets are unset.

Inbound auth has two surfaces, both backed by `aux_m2m_server`:

- Operator-facing routes (`/admin/*`, `/agents*`) use `require_session`,
  which verifies an EdDSA session JWT against `auth-engine-x`'s JWKS.
- Internal server-to-server routes (`/internal/*`) use `require_m2m`,
  which verifies an EdDSA M2M JWT (caller mints via auth-engine-x with its
  own AUX_M2M_API_KEY).

There is no static bearer fallback. Both gates require valid JWTs.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime  # noqa: F401  (imported per port spec; used by future handlers)

import httpx
from fastapi import Depends, FastAPI, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from app import agent_defaults as agent_defaults_store
from app import invocation_log as invocation_log_store
from app.anthropic_client import create_session, get_agent, list_agents, send_user_message
from aux_m2m_client import AsyncM2MTokenClient
from aux_m2m_server import build_health_router, require_m2m, require_session

from app.config import settings
from app.sync import sync_from_anthropic


APP_VERSION = "0.1.0"


# ----- Pydantic models ------------------------------------------------------

class AgentDefaultsPayload(BaseModel):
    environment_id: str = Field(..., min_length=1)
    vault_ids: list[str] = Field(default_factory=list)
    task_instruction: str | None = Field(
        default=None,
        description=(
            "Optional per-agent kickoff preamble prepended to the user.message "
            "sent when /sessions/from-event fires. Use this to give the agent "
            "a short, durable job description that sits above the event payload."
        ),
    )


class AgentDefaults(BaseModel):
    agent_id: str
    environment_id: str
    vault_ids: list[str]
    task_instruction: str | None = None


class AgentDefaultsList(BaseModel):
    data: list[AgentDefaults]
    count: int


class DeleteResult(BaseModel):
    deleted: bool


class EventRef(BaseModel):
    store: str = Field(..., min_length=1, description="Caller-side table/store name, e.g. 'oex_webhook_events'")
    id: str = Field(..., min_length=1, description="Row id in that store (usually a UUID)")


class InvokeAgentPayload(BaseModel):
    source: str = Field(..., min_length=1, description="e.g. 'emailbison', 'cal.com'")
    event_name: str = Field(..., min_length=1, description="e.g. 'lead_replied', 'BOOKING_CREATED'")
    event_ref: EventRef = Field(..., description="Pointer to the stored raw payload in the caller's DB")
    title: str | None = Field(default=None, description="Optional session title override")
    idempotency_key: str | None = Field(
        default=None,
        description=(
            "Optional caller-supplied key. If supplied and seen before, the "
            "original InvokeAgentResult is replayed and no new Anthropic "
            "session is created. Callers retrying after a transient failure "
            "should pass a stable key (e.g. the webhook-ingest row id) to "
            "avoid duplicate agent sessions."
        ),
    )


class InvokeAgentResult(BaseModel):
    session_id: str
    agent_id: str
    environment_id: str
    vault_ids: list[str]
    status: str


# ----- App ------------------------------------------------------------------

app = FastAPI(
    title="managed-agents-x",
    version=APP_VERSION,
    description=(
        "Managed-agents product surface. Wraps Anthropic's managed-agents API "
        "and stores per-agent defaults plus version history. Future home of "
        "CRUD, system-prompt versioning, drafts/templates, A/B tests, and "
        "analytics. Operator routes require an EdDSA session JWT; internal "
        "server-to-server routes require an EdDSA M2M JWT — both verified "
        "against auth-engine-x's JWKS."
    ),
)


# ----- Health router --------------------------------------------------------
#
# Library-provided depth-health router (aux_m2m_server.build_health_router).
# Exposes:
#   GET /api/health        — liveness, always 200, no checks
#   GET /api/health/deep   — depth check (200 healthy / 503 unhealthy)
#
# Deep check exercises:
#   1. JWKS reachability (receiver-side trust)
#   2. Local M2M mintability (caller-side trust — proves AUX_M2M_API_KEY works
#      even though MAGS is largely receive-only)
#   3. Each declared peer's /api/health reachability
#
# MAGS is receive-only with respect to AUX peers — its only outbound traffic is
# to Anthropic's managed-agents API (see app/anthropic_client.py). It is the
# *target* of /internal/agents/{id}/invoke calls from ops-engine-x and friends,
# not the caller. Therefore peers={}.

_peer_token_client = AsyncM2MTokenClient(settings.to_m2m_config())

app.include_router(
    build_health_router(
        service_name="mags",
        version=APP_VERSION,
        token_client=_peer_token_client,
        peers={},
    )
)


@app.get("/")
def root() -> dict[str, str]:
    """Service identity probe."""
    return {"service": "managed-agents-x", "status": "ok"}


@app.get("/admin/status", dependencies=[Depends(require_session)])
def admin_status() -> dict[str, object]:
    """Authenticated secret-load probe.

    Reports which configured secrets Doppler has successfully injected. Values
    are never returned, only presence booleans. Useful immediately after a
    deploy or DOPPLER_TOKEN rotation to verify the process actually loaded
    what you expect. Tier-1 (boot-required) secrets are guaranteed present
    — if they were not, the process would not be answering this request.
    """
    return {
        "service": "managed-agents-x",
        "status": "ok",
        "secrets_loaded": {
            "aux_jwks_url": bool(settings.aux_jwks_url),
            "aux_issuer": bool(settings.aux_issuer),
            "aux_audience": bool(settings.aux_audience),
            "aux_api_base_url": bool(settings.aux_api_base_url),
            "aux_m2m_api_key": bool(settings.aux_m2m_api_key),
            "anthropic_managed_agents_api_key": bool(settings.anthropic_managed_agents_api_key),
            "mags_db_url_pooled": bool(settings.mags_db_url_pooled),
        },
    }


# ----- Anthropic passthrough error helper -----------------------------------

def _passthrough_upstream_error(exc: httpx.HTTPStatusError) -> JSONResponse:
    try:
        body = exc.response.json()
    except ValueError:
        body = {"detail": exc.response.text or "Upstream Anthropic error"}
    return JSONResponse(status_code=exc.response.status_code, content=body)


# ----- Admin sync -----------------------------------------------------------

@app.post("/admin/sync/anthropic", dependencies=[Depends(require_session)])
def admin_sync_anthropic() -> dict[str, object]:
    """Pull all managed agents from Anthropic and reconcile into the DB."""
    return sync_from_anthropic().as_dict()


# ----- Agent defaults (DB-backed) -------------------------------------------
#
# NOTE: `/agents/defaults` MUST be registered before `/agents/{agent_id}` so
# the literal-path route wins over the path-param route. Starlette matches in
# declaration order.

@app.get(
    "/agents/defaults",
    dependencies=[Depends(require_session)],
    response_model=AgentDefaultsList,
)
def list_agent_defaults() -> AgentDefaultsList:
    """List every agent_defaults row (frontend merges with /agents client-side)."""
    rows = agent_defaults_store.list_all()
    return AgentDefaultsList(data=[AgentDefaults(**r) for r in rows], count=len(rows))


@app.get(
    "/agents/{agent_id}/defaults",
    dependencies=[Depends(require_session)],
    response_model=AgentDefaults,
)
def get_agent_defaults(agent_id: str) -> AgentDefaults:
    row = agent_defaults_store.get(agent_id)
    if row is None:
        raise HTTPException(status_code=404, detail="No defaults configured for this agent")
    return AgentDefaults(**row)


@app.put(
    "/agents/{agent_id}/defaults",
    dependencies=[Depends(require_session)],
    response_model=AgentDefaults,
)
def put_agent_defaults(agent_id: str, payload: AgentDefaultsPayload) -> AgentDefaults:
    row = agent_defaults_store.upsert(
        agent_id,
        payload.environment_id,
        payload.vault_ids,
        payload.task_instruction,
    )
    return AgentDefaults(**row)


@app.delete(
    "/agents/{agent_id}/defaults",
    dependencies=[Depends(require_session)],
    response_model=DeleteResult,
)
def delete_agent_defaults(agent_id: str) -> DeleteResult:
    deleted = agent_defaults_store.delete(agent_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="No defaults configured for this agent")
    return DeleteResult(deleted=True)


# ----- Internal invocation gateway ------------------------------------------
#
# Called by ops-engine-x once it has routed an event to a specific agent_id.
# ops-engine-x already decided which agent fires; this endpoint does not
# inspect (source, event_name) to pick an agent — it just invokes the agent
# in the URL. See MANAGED-AGENTS-BRIEF.md §"Hold-the-line rules" #1.
#
# Auth: `require_m2m` (EdDSA M2M JWT). The `/internal/*` prefix is intentionally
# distinct from operator routes so the auth surface stays cleanly split:
# operator session JWT vs system-service M2M JWT. Both verified by aux_m2m_server.

def _format_event_message(
    source: str,
    event_name: str,
    event_ref: EventRef,
    task_instruction: str | None = None,
) -> str:
    """Compose the first user.message sent into the new Anthropic session.

    Byte-for-byte identical to ops-engine-x's `_format_event_message` so the
    agent-side kickoff-format contract is stable across the cutover.
    """
    body = (
        f"source: {source}\n"
        f"event_name: {event_name}\n"
        f"event_ref: {json.dumps(event_ref.model_dump())}\n"
    )
    if task_instruction:
        return f"{task_instruction.rstrip()}\n\n{body}"
    return body


@app.post(
    "/internal/agents/{agent_id}/invoke",
    dependencies=[Depends(require_m2m)],
    response_model=InvokeAgentResult,
)
def invoke_agent(agent_id: str, payload: InvokeAgentPayload) -> InvokeAgentResult:
    """Server-to-server invocation. Creates an Anthropic session against the
    given `agent_id` (resolving environment_id + vault_ids + task_instruction
    from agent_defaults) and posts the formatted event kickoff as the first
    user message.

    Idempotency: if `payload.idempotency_key` is supplied and a prior
    invocation with the same key succeeded, the stored InvokeAgentResult is
    returned verbatim with no new Anthropic session. See `invocation_log`.
    """
    if payload.idempotency_key:
        cached = invocation_log_store.get_response(payload.idempotency_key)
        if cached is not None:
            return InvokeAgentResult(**cached)

    defaults = agent_defaults_store.get(agent_id)
    if defaults is None:
        raise HTTPException(
            status_code=409,
            detail=f"No agent_defaults configured for agent_id={agent_id}",
        )

    metadata = {
        "source": payload.source,
        "event_name": payload.event_name,
        "event_ref_store": payload.event_ref.store,
        "event_ref_id": payload.event_ref.id,
    }
    title = payload.title or f"{payload.source}:{payload.event_name}"

    try:
        session = create_session(
            agent_id=agent_id,
            environment_id=defaults["environment_id"],
            vault_ids=defaults["vault_ids"],
            title=title,
            metadata=metadata,
        )
    except httpx.HTTPStatusError as exc:
        return _passthrough_upstream_error(exc)  # type: ignore[return-value]
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail=f"create_session failed: {exc}") from exc

    try:
        send_user_message(
            session_id=session["id"],
            text=_format_event_message(
                payload.source,
                payload.event_name,
                payload.event_ref,
                defaults.get("task_instruction"),
            ),
        )
    except httpx.HTTPStatusError as exc:
        return _passthrough_upstream_error(exc)  # type: ignore[return-value]
    except httpx.HTTPError as exc:
        raise HTTPException(
            status_code=502,
            detail=f"send_user_message failed: {exc}",
        ) from exc

    result = InvokeAgentResult(
        session_id=session["id"],
        agent_id=agent_id,
        environment_id=defaults["environment_id"],
        vault_ids=list(defaults["vault_ids"]),
        status=session.get("status", "unknown"),
    )

    if payload.idempotency_key:
        # ON CONFLICT DO NOTHING — a race loser silently proceeds and returns
        # its own freshly-created session's result. V1 trade-off: the losing
        # racer leaves a duplicate Anthropic session behind (see migration
        # comment on invocation_log).
        invocation_log_store.insert(
            payload.idempotency_key,
            result.session_id,
            agent_id,
            result.model_dump(),
        )

    return result


# ----- Anthropic passthrough (live reads) -----------------------------------

@app.get("/agents", dependencies=[Depends(require_session)], response_model=None)
def get_agents() -> JSONResponse | dict[str, object]:
    """List all managed agents (live passthrough to Anthropic, paginated server-side)."""
    try:
        agents = list(list_agents())
    except httpx.HTTPStatusError as exc:
        return _passthrough_upstream_error(exc)
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail=f"Upstream request failed: {exc}") from exc
    return {"data": agents, "count": len(agents)}


@app.get("/agents/{agent_id}", dependencies=[Depends(require_session)], response_model=None)
def get_agent_by_id(agent_id: str) -> JSONResponse | dict:
    """Single agent (live passthrough to Anthropic)."""
    try:
        return get_agent(agent_id)
    except httpx.HTTPStatusError as exc:
        return _passthrough_upstream_error(exc)
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail=f"Upstream request failed: {exc}") from exc
