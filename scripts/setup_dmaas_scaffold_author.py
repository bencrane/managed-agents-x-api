"""Scaffold the DMaaS scaffold-authoring managed agent.

Operator-supervised authoring agent. Triggered manually (operator runs a
session against a brief or a batch of briefs). Reads the DMaaS MCP at
api.opsengine.run/mcp/dmaas/ via Bearer token from the production vault.

Tool surface is intentionally narrow:
  * READ:  list_specs, get_spec, list_scaffolds, get_scaffold,
           validate_constraints, preview_scaffold
  * WRITE: create_scaffold, update_scaffold
  * BLOCKED: create_design, update_design_content, validate_design,
             get_design — those are runtime/content tools owned by
             the user-facing chat agent.

No agent_defaults row is seeded. Sessions are started by the operator
with explicit (environment_id, vault_ids).

Usage:
  ./scripts/doppler run -- python -m scripts.setup_dmaas_scaffold_author

Re-running creates a new agent each time (Anthropic doesn't dedupe by name).
Archive the old one on the platform first if re-scaffolding.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request

from app.config import require

NAME = "dmaas-scaffold-author"
MODEL = "claude-opus-4-6"

# Production vault holds the DMAAS_MCP_BEARER_TOKEN secret bound to the
# DMaaS MCP URL. Operator passes this vault_id when starting a session.
VAULT_ID = "vlt_011CZtjQ5LjLrbAd4gX7xA6E"
ENVIRONMENT_ID = "env_01T3cywTrvvtZoUQYAzxMA1D"  # "all" (unrestricted)

DMAAS_MCP = {
    "type": "url",
    "name": "dmaas-mcp",
    "url": "https://api.opsengine.run/mcp/dmaas/",
}

# DMaaS MCP tools the agent is permitted to call. Tools NOT in this list
# (create_design / update_design_content / validate_design / get_design)
# are explicitly disabled below — they're runtime/content tools owned by
# the user-facing chat agent.
ALLOWED_DMAAS_TOOLS = (
    "list_specs",
    "get_spec",
    "list_scaffolds",
    "get_scaffold",
    "validate_constraints",
    "preview_scaffold",
    "create_scaffold",
    "update_scaffold",
)
BLOCKED_DMAAS_TOOLS = (
    "create_design",
    "update_design_content",
    "validate_design",
    "get_design",
)

_ALLOW = {"enabled": True, "permission_policy": {"type": "always_allow"}}

# Verbatim from app/dmaas/dsl.py face-consistency contract. The agent
# must understand which zones it can reference for its declared face.
FACE_CONSISTENCY_CONTRACT = (
    "ConstraintSpecification.face accepts one of front | back | outside | inside, "
    "or None for legacy face-agnostic scaffolds. When face is set, every zone "
    "reference in constraints must either start with that face's prefix "
    "(front_, back_, outside_, inside_) or be one of the legacy face-agnostic "
    "zones (safe_zone, canvas, trim). The DSL rejects anything else via "
    "validate_references, naming the offending zone and the declared face."
)

# Verbatim from data/dmaas_strategies.json. Re-pasted here so the agent
# is self-contained at session time.
STRATEGY_HERO = (
    "Single dominant message anchored top of canvas. Use when the message itself "
    "is the lead — bold positioning, big news, a confident assertion. Headline is "
    "the visual anchor; subhead and CTA support but never compete. No proof "
    "artifacts, no offer block."
)
STRATEGY_PROOF = (
    "Trust-building via credentials. Use when the audience needs to believe "
    "before they care. Headline frames; proof points (logos, stats, or "
    "testimonial) carry the credibility load. CTA is restrained, not the visual "
    "focus."
)
STRATEGY_OFFER = (
    "Big-number offer is the visual anchor. Use when the offer itself is the "
    "most compelling thing — discounts, rebates, fixed-price, time-bound. Offer "
    "block dominates by area; headline frames it; CTA closes."
)
STRATEGY_TRUST = (
    "Authority and tenure signal. Use when 'we've been around' or 'we know your "
    "industry' matters more than features. Established voice, calm hierarchy, "
    "restrained palette. No urgency artifacts, no discount language. A "
    "credential strip (years in business, industry tenure) is mandatory."
)


SYSTEM_PROMPT = f"""You are an authoring agent for direct-mail scaffolds. You design layout templates that other agents and humans will fill with content. You author one scaffold per brief, full stop.

# Face-consistency contract

{FACE_CONSISTENCY_CONTRACT}

# Strategy theses

The four strategies you author against are normative. Treat them as the source of truth for what each archetype is FOR; deviating is how scaffolds become indistinguishable.

- **hero**: {STRATEGY_HERO}
- **proof**: {STRATEGY_PROOF}
- **offer**: {STRATEGY_OFFER}
- **trust**: {STRATEGY_TRUST}

# Authoring loop

You author exactly one scaffold per brief by following these steps. Do not skip a step. Do not author multiple scaffolds in one session unless the operator explicitly batches you.

1. Read the brief. Note: slug, strategy, face, format, compatible_specs, thesis, required_slots, optional_slots, acceptance_rules, placeholder_content.
2. For every entry in `compatible_specs`, call `get_spec(category, variant)` to learn the exact zone catalog. Do this once per session — caching what you read.
3. Draft three artifacts:
   - `prop_schema`: a JSON Schema for content_config. Required slots from the brief MUST be in `required`; optional slots MAY be in `properties` but not `required`. Each slot value is `{{type: "object", required: ["text"], properties: {{text: {{type: "string", minLength: 1, maxLength: ...}}, color: ..., intrinsic: ...}}}}`.
   - `constraint_specification`: the layout DSL with `face` set per the brief. Use face-prefixed zone names (front_, back_, outside_, inside_) or legacy zones (safe_zone, canvas, trim). Every constraint references a zone in the same face or a legacy zone — anything else is a parse error.
   - `placeholder_content`: one example matching the prop_schema. Each element has an `intrinsic` block with `min_width`, `max_width`, `preferred_width`, `preferred_height` so the solver has size hints.
4. Iterate to a valid spec: call `validate_constraints(category, variant, constraint_specification, placeholder_content)`. Maximum 12 attempts per brief. On conflict, refine your DSL — do not retry the same DSL twice.
5. When `validate_constraints` returns `is_valid: true`, evaluate the brief's `acceptance_rules` against `positions`:
   - `area_dominance`: target element's bbox area ≥ ratio × every other element's bbox area.
   - `size_hierarchy`: larger element's height ≥ ratio × smaller element's height.
   - `slot_present`: named slot exists in `prop_schema.properties`.
   - `min_slot_count`: at least N slots in `prop_schema.properties` start with the named category.
   If any rule fails, the DSL is wrong — refine and retry. The 12-attempt budget covers all attempts (validate + acceptance combined).
6. For every entry in `compatible_specs`, call `preview_scaffold(slug, category, variant, placeholder_content)` to confirm the saved scaffold renders. (Strictly speaking this runs AFTER create; if you're authoring greenfield, skip step 6 and go to step 7. Use step 6 when refining an existing scaffold via `update_scaffold`.)
7. Call `create_scaffold(...)` with `strategy` and `face` set per the brief, the validated DSL, the prop_schema, the placeholder_content, and `compatible_specs` from the brief. `create_scaffold` re-runs the solver against every compatible_spec and refuses to save if any combination fails. If it does fail (because acceptance rules in placeholder_content didn't translate to all variants), refine and retry — that counts toward the 12-attempt budget.
8. On successful create, log `<slug>: created (N attempts)`. Move to the next brief if batched, else end the session.

# Failure handling

If a brief exhausts the 12-attempt budget, log `<slug>: failed after 12 attempts — <reason>` and continue to the next brief. The operator inspects the audit trail (`dmaas_scaffold_authoring_sessions`) afterward; do not block the queue on one bad brief.

If `create_scaffold` fails for a non-solve reason (invalid_strategy, constraint_references, unknown_spec), the DSL or compatible_specs is wrong — fix the input rather than retrying.

If you can't find a zone you need in `get_spec`'s output, the brief is targeting a face / format that the spec catalog doesn't yet cover. Stop, log the gap, do not invent zone names — the DSL will reject unknown zones.

# Tool budget

Each session is hard-capped at 200 tool calls. Eight briefs at ~25 calls each is the design point; do not waste calls on speculative reads.

# Tone

Terse. Internal reasoning is not user-facing. Output is one line per brief: `<slug>: created (N attempts)` or `<slug>: failed after 12 attempts — <reason>`. The scaffold record itself is the artifact; do not narrate.

# Permitted tools

You can read: list_specs, get_spec, list_scaffolds, get_scaffold, validate_constraints, preview_scaffold.
You can write: create_scaffold, update_scaffold.
You CANNOT call: create_design, update_design_content, validate_design, get_design — those are runtime tools owned by the user-facing chat agent.
"""


def _build_tools() -> list[dict]:
    """agent_toolset (native) + mcp_toolset for dmaas-mcp with explicit
    per-tool enable/disable so the WRITE tools are gated and the design
    tools are blocked."""
    dmaas_configs = [
        {"name": t, "enabled": True, "permission_policy": {"type": "always_allow"}}
        for t in ALLOWED_DMAAS_TOOLS
    ] + [
        {"name": t, "enabled": False}
        for t in BLOCKED_DMAAS_TOOLS
    ]
    return [
        {"type": "agent_toolset_20260401", "default_config": _ALLOW},
        {
            "type": "mcp_toolset",
            "mcp_server_name": DMAAS_MCP["name"],
            # Default: deny anything not explicitly enabled below.
            "default_config": {"enabled": False},
            "configs": dmaas_configs,
        },
    ]


def create_agent() -> dict:
    body = {
        "name": NAME,
        "model": MODEL,
        "system": SYSTEM_PROMPT,
        "mcp_servers": [DMAAS_MCP],
        "tools": _build_tools(),
    }
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/agents",
        data=json.dumps(body).encode(),
        method="POST",
        headers={
            "x-api-key": require("anthropic_managed_agents_api_key"),
            "anthropic-version": "2023-06-01",
            "anthropic-beta": "managed-agents-2026-04-01",
            "content-type": "application/json",
        },
    )
    try:
        return json.loads(urllib.request.urlopen(req).read())
    except urllib.error.HTTPError as e:
        raise SystemExit(f"create_agent failed: {e.code} {e.read().decode()}")


def main() -> None:
    agent = create_agent()
    print(f"agent_id:       {agent['id']}")
    print(f"name:           {agent['name']}")
    print(f"version:        {agent.get('version')}")
    print(f"model:          {agent.get('model')}")
    print(f"mcp_servers:    {[m['name'] for m in agent.get('mcp_servers', [])]}")
    print(f"environment_id: {ENVIRONMENT_ID}  (pass at session-create time)")
    print(f"vault_ids:      [{VAULT_ID}]      (pass at session-create time)")
    print()
    print("Operator workflow:")
    print(
        f"  1. Confirm `dmaas-mcp-token` exists in vault {VAULT_ID} bound to "
        f"`{DMAAS_MCP['url']}`."
    )
    print(
        "  2. Start a session with this agent, environment_id, vault_ids; "
        "send brief JSON as the first user message."
    )
    print(
        "  3. Inspect `dmaas_scaffold_authoring_sessions` for audit trail; "
        "the agent writes one row per attempt."
    )


if __name__ == "__main__":
    main()
