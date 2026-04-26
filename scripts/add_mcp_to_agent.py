"""Append an MCP server + its toolset to an existing managed agent.

Usage:
  doppler run -p managed-agents-x -c prd -- \\
    python -m scripts.add_mcp_to_agent <agent-name-or-id> <mcp-name>

<mcp-name> must be a key in scripts.setup_orchestrator._MCP. The MCP's vault
credential must already exist in whichever vault the agent uses at session time
(we don't touch agent_defaults here).

The update endpoint (POST /v1/agents/{id}) replaces the top-level fields it
receives, so we fetch the current mcp_servers + tools, append, and send both
full arrays back. Name/system/model/skills/metadata are left untouched.
"""

from __future__ import annotations

import argparse
import json
import sys

from app import anthropic_client
from scripts.setup_orchestrator import _MCP, _ALLOW


def _resolve_agent(agent_ref: str) -> dict:
    if agent_ref.startswith("agent_"):
        return anthropic_client.get_agent(agent_ref)
    matches = [
        a for a in anthropic_client.list_agents(include_archived=False)
        if a.get("name") == agent_ref
    ]
    if not matches:
        raise SystemExit(f"no live agent named {agent_ref!r}")
    if len(matches) > 1:
        ids = ", ".join(a["id"] for a in matches)
        raise SystemExit(f"multiple agents named {agent_ref!r}: {ids} — pass an id")
    return matches[0]


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("agent", help="agent name or id (agent_...)")
    p.add_argument("mcp", choices=sorted(_MCP.keys()))
    args = p.parse_args()

    agent = _resolve_agent(args.agent)
    agent_id = agent["id"]

    mcp_servers = list(agent.get("mcp_servers") or [])
    tools = list(agent.get("tools") or [])

    if any(m.get("name") == args.mcp for m in mcp_servers):
        print(f"{args.mcp} already on {agent['name']} ({agent_id}); nothing to do")
        sys.exit(0)

    mcp_servers.append(_MCP[args.mcp])
    tools.append({
        "type": "mcp_toolset",
        "mcp_server_name": args.mcp,
        "default_config": _ALLOW,
    })

    updated = anthropic_client.update_agent(
        agent_id,
        {
            "version": agent["version"],
            "mcp_servers": mcp_servers,
            "tools": tools,
        },
    )

    print(f"updated {updated['name']} ({agent_id})")
    print(f"version:     {updated.get('version')}")
    print(f"mcp_servers: {[m['name'] for m in updated.get('mcp_servers', [])]}")
    print()
    print("next: POST /admin/sync/anthropic (or run `python -m app.sync`) to pull the new version into the DB")


if __name__ == "__main__":
    main()
