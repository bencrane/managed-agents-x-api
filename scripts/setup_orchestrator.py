"""Scaffold a cal.com-event orchestrator managed agent.

Creates the agent on Anthropic's infra with the standard orchestrator stack
(oex-mcp / serx-mcp / resend-mcp, plus cal-mcp when relevant), then seeds an
agent_defaults row so the serx webhook can resolve (environment_id, vault_ids)
at session-create time.

Usage:
  ./scripts/doppler run -- python -m scripts.setup_orchestrator <name>

(Project/config come from doppler.yaml at the repo root.)

Where <name> is one of:
  new-booking-orchestrator
  rescheduled-booking-orchestrator
  canceled-booking-orchestrator

Re-running creates a new agent each time (Anthropic doesn't dedupe by name).
Archive the old one on the platform + delete the DB row first if re-scaffolding.
"""

from __future__ import annotations

import argparse
import json
import urllib.error
import urllib.request

from app.config import require
from app.db import connect

VAULT_ID = "vlt_011CZtjQ5LjLrbAd4gX7xA6E"  # production-vault
ENVIRONMENT_ID = "env_01T3cywTrvvtZoUQYAzxMA1D"  # "all" (unrestricted)

# URLs MUST match vault credential mcp_server_url exactly so bearer tokens inject.
_MCP = {
    "cal-mcp":        {"type": "url", "name": "cal-mcp",        "url": "https://cal-mcp.up.railway.app/sse"},
    "oex-mcp":        {"type": "url", "name": "oex-mcp",        "url": "https://oex-mcp.up.railway.app/mcp"},
    "serx-mcp":       {"type": "url", "name": "serx-mcp",       "url": "https://serx-mcp-production-5552.up.railway.app/mcp"},
    "resend-mcp":     {"type": "url", "name": "resend-mcp",     "url": "https://resend-mcp.up.railway.app/mcp"},
    "emailbison":     {"type": "url", "name": "emailbison",     "url": "https://mcp.emailbison.com/mcp"},
    "github-oex":     {"type": "url", "name": "github-oex",     "url": "https://api.githubcopilot.com/mcp"},
    "exa":            {"type": "url", "name": "exa",            "url": "https://mcp.exa.ai/mcp"},
    "stripe":         {"type": "url", "name": "stripe",         "url": "https://mcp.stripe.com/"},
}

# Autonomous — no human-in-the-loop. Orchestrators fire from webhooks.
_ALLOW = {"enabled": True, "permission_policy": {"type": "always_allow"}}


def _build_tools(mcp_names: list[str]) -> list[dict]:
    tools: list[dict] = [{"type": "agent_toolset_20260401", "default_config": _ALLOW}]
    for n in mcp_names:
        tools.append({"type": "mcp_toolset", "mcp_server_name": n, "default_config": _ALLOW})
    return tools


def _placeholder_system(kind: str) -> str:
    return (
        f"You are the {kind}-booking-orchestrator. You are triggered when a {kind} "
        "cal.com booking event is received. The initial user message will contain "
        "the raw cal.com event payload.\n\n"
        "TODO: flesh out orchestration logic. For now, read the payload and "
        "summarize what you would do — do not take action yet.\n"
    )


# name -> (mcps, system prompt)
AGENTS: dict[str, dict] = {
    "new-booking-orchestrator": {
        "mcps": ["cal-mcp", "oex-mcp", "serx-mcp", "resend-mcp"],
        "system": _placeholder_system("new"),
    },
    "rescheduled-booking-orchestrator": {
        "mcps": ["oex-mcp", "serx-mcp", "resend-mcp"],
        "system": _placeholder_system("rescheduled"),
    },
    "canceled-booking-orchestrator": {
        "mcps": ["oex-mcp", "serx-mcp", "resend-mcp"],
        "system": _placeholder_system("canceled"),
    },
    "deal-orchestrator": {
        "mcps": [
            "serx-mcp",
            "cal-mcp",
            "github-oex",
            "stripe",
            "resend-mcp",
            "oex-mcp",
            "exa",
            "emailbison",
        ],
        "system": (
            "You are the deal-orchestrator-agent. You are triggered after a "
            "sales meeting, once a post-meeting form has been filled out and "
            "stored in the serx-api db. The initial user message will point "
            "you at the stored form row.\n\n"
            "At a high level you will: read the form row from serx-api, draft "
            "proposal/agreement content, create a Stripe checkout/payment "
            "link, write the proposal content and checkout link back to the "
            "db, then look up the prospect and send them an email via Resend.\n\n"
            "TODO: flesh out the detailed playbook (field mapping, proposal "
            "template, Stripe product/price selection, email copy). For now, "
            "read the payload and summarize what you would do — do not take "
            "action yet.\n"
        ),
    },
    "inbox-orchestrator": {
        "mcps": ["oex-mcp", "serx-mcp", "resend-mcp", "emailbison"],
        "system": (
            "You are the inbox-orchestrator. You are triggered by oex-webhook-ingest "
            "for every relevant EmailBison event (replies, interested, unsubscribes, "
            "bounces, etc.). The initial user message will contain the event type and "
            "raw payload.\n\n"
            "TODO: flesh out classification + playbook logic. For now, read the "
            "payload and summarize what you would do — do not take action yet.\n"
        ),
    },
}


def create_agent(name: str, system: str, mcp_names: list[str]) -> dict:
    body = {
        "name": name,
        "model": "claude-sonnet-4-6",
        "system": system,
        "mcp_servers": [_MCP[n] for n in mcp_names],
        "tools": _build_tools(mcp_names),
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


def seed_defaults(agent_id: str) -> None:
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            insert into agent_defaults (agent_id, environment_id, vault_ids)
            values (%s, %s, %s)
            on conflict (agent_id) do update
              set environment_id = excluded.environment_id,
                  vault_ids = excluded.vault_ids
            """,
            (agent_id, ENVIRONMENT_ID, [VAULT_ID]),
        )
        conn.commit()


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("name", choices=sorted(AGENTS.keys()))
    args = p.parse_args()

    cfg = AGENTS[args.name]
    agent = create_agent(args.name, cfg["system"], cfg["mcps"])
    print(f"agent_id:       {agent['id']}")
    print(f"name:           {agent['name']}")
    print(f"version:        {agent.get('version')}")
    print(f"mcp_servers:    {[m['name'] for m in agent.get('mcp_servers', [])]}")
    print(f"environment_id: {ENVIRONMENT_ID}")
    print(f"vault_ids:      [{VAULT_ID}]")

    seed_defaults(agent["id"])
    print("agent_defaults: upserted")


if __name__ == "__main__":
    main()
