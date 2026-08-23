"""Hosted (containerized) energy-supplier customer agent.

Runs the same image in each consumer project. It answers using the central model served
through the shared APIM gateway (BYOM route "<connection>/<model>", e.g. apim-shared/gpt-4.1),
and looks up real customer data via the energy MCP server — also fronted by the same gateway,
so both the model and the tools go consumer project -> APIM -> backend, keyless.

Identity: the agent authenticates with its Foundry-injected Microsoft Entra Agent ID; the
gateway allows its blueprint appid (wired by infra/hooks/create_hosted_agents.py).

Env (injected by the deploy hook via the hosted-agent definition):
  AZURE_AI_PROJECT_ENDPOINT       this project's Foundry endpoint
  AZURE_AI_MODEL_DEPLOYMENT_NAME  the gateway model route, e.g. apim-shared/gpt-4.1
  MCP_GATEWAY_URL                 the energy MCP route on the gateway (optional)
"""

import base64
import json
import os
import sys

from agent_framework import MCPStreamableHTTPTool
from agent_framework_foundry import FoundryChatClient
from agent_framework_foundry_hosting import ResponsesHostServer
from azure.identity import DefaultAzureCredential, get_bearer_token_provider

credential = DefaultAzureCredential()  # the agent's Foundry-injected Microsoft Entra Agent ID

_seen_appids = set()


def _log_token_identity(token):
    """Log the identity claims of the token we send to the gateway, once per distinct appid.

    The gateway allowlists by `appid`, so this reveals exactly which identity each invocation path
    presents — e.g. the Foundry-injected Agent ID (blueprint) via the Responses endpoint vs. a
    different managed identity via the portal playground. Claims (appid/aud/oid/sub) aren't secrets;
    the raw token is never logged. Never breaks the request over a log line.
    """
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)  # pad base64url
        claims = json.loads(base64.urlsafe_b64decode(payload))
        appid = claims.get("appid") or claims.get("azp") or "unknown"
        if appid in _seen_appids:
            return
        _seen_appids.add(appid)
        print(f"[gateway-agent] MCP token identity: appid={appid} aud={claims.get('aud')} "
              f"oid={claims.get('oid')} sub={claims.get('sub')} "
              f"(allow this appid in gateway-agent-appid to pass validate-azure-ad-token)",
              file=sys.stderr, flush=True)
    except Exception:
        pass


def _mcp_headers(_kwargs):
    token = gateway_token()
    _log_token_identity(token)
    return {"Authorization": f"Bearer {token}"}


# The energy MCP server, reached through the SAME APIM gateway as the model. The gateway validates
# an Entra token (audience cognitiveservices) and runs the governance kill switch, so inject the
# agent's Agent ID bearer token on every MCP call. The host server connects the tool lazily on the
# first request. No MCP_GATEWAY_URL => no tools (plain chat agent).
tools = []
mcp_url = os.environ.get("MCP_GATEWAY_URL")
if mcp_url:
    gateway_token = get_bearer_token_provider(credential, "https://cognitiveservices.azure.com/.default")
    tools.append(
        MCPStreamableHTTPTool(
            name="energy",
            url=mcp_url,
            description="Energy supplier customer profiles, meters, readings and consumption.",
            header_provider=_mcp_headers,
        )
    )

agent = FoundryChatClient(
    project_endpoint=os.environ["AZURE_AI_PROJECT_ENDPOINT"],
    model=os.environ["AZURE_AI_MODEL_DEPLOYMENT_NAME"],
    credential=credential,
).as_agent(
    name="gateway-hosted",
    instructions=(
        "You are a customer service agent for an energy supplier. Help customers with their "
        "account, meters, tariffs and energy consumption. Use the energy tools to look up "
        "customer profiles, meters, meter readings and consumption summaries by id (e.g. C-1001). "
        "Base answers on tool results; if the data isn't available, say so plainly."
    ),
    tools=tools,
)

if __name__ == "__main__":
    ResponsesHostServer(agent).run()  # serves the Responses protocol on :8088
