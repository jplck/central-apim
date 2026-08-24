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

AGENT_NAME = "gateway-hosted"
# App Config feature flag that turns on Defender for Cloud UserSecurityContext enrichment.
FLAG_NAME = os.environ.get("DEFENDER_USERCONTEXT_FLAG", "defender-usercontext")

_seen_appids = set()


def _token_claims(token):
    """Decode a JWT's payload claims (unverified — for identity logging/enrichment only)."""
    payload = token.split(".")[1]
    payload += "=" * (-len(payload) % 4)  # pad base64url
    return json.loads(base64.urlsafe_b64decode(payload))


def _log_token_identity(token):
    """Log the identity claims of the token we send to the gateway, once per distinct appid.

    The gateway allowlists by `appid`, so this reveals exactly which identity each invocation path
    presents — e.g. the Foundry-injected Agent ID (blueprint) via the Responses endpoint vs. a
    different managed identity via the portal playground. Claims (appid/aud/oid/sub) aren't secrets;
    the raw token is never logged. Never breaks the request over a log line.
    """
    try:
        claims = _token_claims(token)
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


def _feature_enabled(name):
    """True iff the App Config feature flag `name` is on. Fail-safe to False — enrichment is
    best-effort observability and must never gate a request. No APP_CONFIG_ENDPOINT => off."""
    endpoint = os.environ.get("APP_CONFIG_ENDPOINT", "").rstrip("/")
    if not (endpoint and name):
        return False
    try:
        from azure.appconfiguration import AzureAppConfigurationClient
        client = AzureAppConfigurationClient(base_url=endpoint, credential=credential)
        setting = client.get_configuration_setting(key=f".appconfig.featureflag/{name}")
        return bool(json.loads(setting.value).get("enabled"))
    except Exception as e:  # unset key, RBAC not yet propagated, transient — treat as off
        print(f"[gateway-agent] feature flag '{name}' unreadable ({e!r}); enrichment off",
              file=sys.stderr, flush=True)
        return False


def _build_context(app_name, claims):
    """Azure OpenAI UserSecurityContext body from the agent's own token claims. `end_user_id` is
    the blueprint appid the governance proxy revokes on — no PII, exactly what Defender should
    stamp onto the alert it raises for this call."""
    ctx = {"application_name": app_name}
    claims = claims or {}
    uid = claims.get("appid") or claims.get("azp") or claims.get("oid")
    if uid:
        ctx["end_user_id"] = uid
    return ctx


def _user_security_context():
    """Defender for Cloud enrichment: identify the agent behind each model call so its
    jailbreak/prompt-injection alerts carry the caller identity the governance proxy revokes on.
    Gated by the App Config feature flag `defender-usercontext`; off => None (no enrichment).

    ponytail: the flag is read once at startup. The agent is long-lived and the demo flips the flag
    then restarts before the run, so live reload isn't worth a poll loop — restart to toggle.
    """
    if not _feature_enabled(FLAG_NAME):
        return None
    claims = {}
    try:
        token = credential.get_token("https://cognitiveservices.azure.com/.default").token
        claims = _token_claims(token)
    except Exception:
        pass  # still enrich with application_name — an unattributed alert beats no signal
    ctx = _build_context(AGENT_NAME, claims)
    print(f"[gateway-agent] Defender UserSecurityContext enrichment ON: {ctx}",
          file=sys.stderr, flush=True)
    return ctx


def _self_test():
    """Offline check of the pure enrichment logic — no Azure, no env. Run: python agent.py --self-test"""
    tok = "h." + base64.urlsafe_b64encode(b'{"appid":"abc","oid":"o"}').decode().rstrip("=") + ".s"
    assert _token_claims(tok)["appid"] == "abc"
    assert _build_context("gw", {"appid": "abc"}) == {"application_name": "gw", "end_user_id": "abc"}
    assert _build_context("gw", {"azp": "z"}) == {"application_name": "gw", "end_user_id": "z"}
    assert _build_context("gw", {"oid": "o"}) == {"application_name": "gw", "end_user_id": "o"}
    assert _build_context("gw", {}) == {"application_name": "gw"}
    assert _build_context("gw", None) == {"application_name": "gw"}
    print("agent self-test ok")


if __name__ == "__main__" and "--self-test" in sys.argv:
    _self_test()
    raise SystemExit(0)


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

_usc = _user_security_context()  # None unless the defender-usercontext feature flag is on
# When enrichment is on, stamp every model call's body with user_security_context so Defender
# attributes the alert to this agent's identity. extra_body -> Responses API request body.
_default_options = {"extra_body": {"user_security_context": _usc}} if _usc else None

agent = FoundryChatClient(
    project_endpoint=os.environ["AZURE_AI_PROJECT_ENDPOINT"],
    model=os.environ["AZURE_AI_MODEL_DEPLOYMENT_NAME"],
    credential=credential,
).as_agent(
    name=AGENT_NAME,
    instructions=(
        "You are a customer service agent for an energy supplier. Help customers with their "
        "account, meters, tariffs and energy consumption. Use the energy tools to look up "
        "customer profiles, meters, meter readings and consumption summaries by id (e.g. C-1001). "
        "Base answers on tool results; if the data isn't available, say so plainly."
    ),
    tools=tools,
    default_options=_default_options,
)

if __name__ == "__main__":
    ResponsesHostServer(agent).run()  # serves the Responses protocol on :8088
