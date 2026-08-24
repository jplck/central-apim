#!/usr/bin/env python3
"""Post-provision hook: build the tiny hosted-agent image and deploy it as a hosted
(containerized) agent into each consumer Foundry project, wired to the central model
served through the shared APIM gateway.

Everything runs as the azd deploy identity (AzureDeveloperCliCredential): the ACR build
and the agent's model-access role assignment go through ARM REST (stdlib urllib) rather
than the `az` CLI, because `az` and `azd` may be logged into different identities.
Reads azd provisioning outputs from the environment. Idempotent where practical.
"""
from __future__ import annotations

import io
import json
import os
import sys
import tarfile
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path

from azure.ai.projects import AIProjectClient
from azure.ai.projects.models import (
    AgentEndpointProtocol,
    ContainerConfiguration,
    HostedAgentDefinition,
    ProtocolVersionRecord,
)
from azure.core.exceptions import ClientAuthenticationError, HttpResponseError
from azure.identity import AzureDeveloperCliCredential

AGENT_NAME = os.environ.get("HOSTED_AGENT_NAME", "gateway-hosted")
# Foundry User: lets the agent's own identity call the project's model (the BYOM route).
FOUNDRY_USER_ROLE_ID = "53ca6127-db72-4b80-b1b0-d745d6d5456d"
# App Configuration Data Reader: lets the agent read the defender-usercontext feature flag.
APP_CONFIG_READER_ROLE_ID = "516239f1-63e1-4d78-a4de-a74fb236a071"
CONTEXT = Path(__file__).resolve().parents[2] / "src" / "hosted-agent"
ARM = "https://management.azure.com"
ACR_API = "2019-06-01-preview"
# Entra data-plane role propagation after provisioning can take a few minutes.
RETRIES = int(os.environ.get("AGENT_HOOK_RETRIES", "12"))
DELAY = int(os.environ.get("AGENT_HOOK_DELAY", "20"))
APIM_NV_API = "2024-05-01"  # APIM named-value ARM API version (matches provider.bicep)


def _require(name):
    val = os.environ.get(name, "").strip()
    if not val:
        sys.exit(f"{name} is not set. Run this via `azd up`/`azd provision` so outputs are available.")
    return val


def _pairs(endpoints, arm_ids):
    """Zip project endpoints with their ARM ids (same order), padding missing ids with ''."""
    arm_ids = list(arm_ids) + [""] * (len(endpoints) - len(arm_ids))
    return list(zip(endpoints, arm_ids))


def _extract_principal_id(agent):
    ident = getattr(agent, "instance_identity", None)
    return getattr(ident, "principal_id", None) if ident else None


def _extract_blueprint_appid(agent):
    """The agent's Entra Agent ID *blueprint* appid (client id) — shared by all its instances and
    the `appid` claim its runtime tokens carry, so it's what the APIM gateway allowlists."""
    bp = getattr(agent, "blueprint", None)
    return getattr(bp, "client_id", None) if bp else None


def _transient_auth(err):
    """True if the error is a not-yet-propagated RBAC failure worth retrying."""
    if getattr(err, "status_code", None) in (401, 403):
        return True
    if isinstance(err, ClientAuthenticationError):
        return True
    msg = str(getattr(err, "message", "") or err).lower()
    return any(s in msg for s in ("does not have permission", "not authorized", "forbidden", "authorization"))


# --- ARM REST helpers (run as the azd identity) ---------------------------------

def _arm_token(cred):
    return cred.get_token(f"{ARM}/.default").token


def _http(method, url, token=None, data=None, headers=None):
    """Minimal REST call. Returns (status, parsed_body). JSON is decoded when present."""
    hdrs = dict(headers or {})
    if token:
        hdrs["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, method=method, data=data, headers=hdrs)
    try:
        resp = urllib.request.urlopen(req)
        raw = resp.read()
        status, ctype = resp.status, resp.headers.get("Content-Type", "")
    except urllib.error.HTTPError as e:
        raw = e.read()
        status, ctype = e.code, e.headers.get("Content-Type", "")
    if raw and "json" in ctype:
        try:
            return status, json.loads(raw)
        except ValueError:
            pass
    return status, (raw.decode(errors="replace") if raw else "")


def _tar_context():
    """gzip-tar the build context (Dockerfile at root) for ACR to consume."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for p in sorted(CONTEXT.iterdir()):
            if p.name.startswith(".") or p.name == "__pycache__":
                continue
            tar.add(p, arcname=p.name)
    return buf.getvalue()


def _build_image(cred, acr_id, login_server):
    """Build + push the image inside ACR via REST (equivalent of `az acr build`)."""
    tag = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    repo_tag = f"{AGENT_NAME}:{tag}"
    print(f"Building {login_server}/{repo_tag} from {CONTEXT} via ACR REST...", flush=True)

    status, body = _http("POST", f"{ARM}{acr_id}/listBuildSourceUploadUrl?api-version={ACR_API}",
                         token=_arm_token(cred), data=b"")
    if status >= 400 or not isinstance(body, dict):
        raise RuntimeError(f"listBuildSourceUploadUrl failed ({status}): {body}")
    upload_url, relative_path = body["uploadUrl"], body["relativePath"]

    status, body = _http("PUT", upload_url, data=_tar_context(),
                         headers={"x-ms-blob-type": "BlockBlob"})
    if status >= 400:
        raise RuntimeError(f"context upload failed ({status}): {body}")

    run_req = json.dumps({
        "type": "DockerBuildRequest",
        "sourceLocation": relative_path,
        "dockerFilePath": "Dockerfile",
        "imageNames": [repo_tag],
        "isPushEnabled": True,
        "platform": {"os": "Linux", "architecture": "amd64"},
    }).encode()
    status, body = _http("POST", f"{ARM}{acr_id}/scheduleRun?api-version={ACR_API}",
                         token=_arm_token(cred), data=run_req,
                         headers={"Content-Type": "application/json"})
    if status >= 400 or not isinstance(body, dict):
        raise RuntimeError(f"scheduleRun failed ({status}): {body}")
    run_id = (body.get("properties") or {}).get("runId") or body.get("name")
    print(f"  ACR build queued (run {run_id}); waiting...", flush=True)

    for _ in range(80):  # up to ~20 min
        time.sleep(15)
        status, body = _http("GET", f"{ARM}{acr_id}/runs/{run_id}?api-version={ACR_API}",
                             token=_arm_token(cred))
        state = (body.get("properties") or {}).get("status") if isinstance(body, dict) else None
        if state == "Succeeded":
            return f"{login_server}/{repo_tag}"
        if state in ("Failed", "Error", "Canceled", "Timeout"):
            raise RuntimeError(f"ACR build {state} (run {run_id})")
    raise RuntimeError(f"ACR build did not finish in time (run {run_id})")


def _assignment_name(scope, principal_id, role_id=FOUNDRY_USER_ROLE_ID):
    """Deterministic role-assignment guid so retries are idempotent."""
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"{scope}|{principal_id}|{role_id}"))


def _assign_role(cred, principal_id, scope, role_id, role_name, on_fail):
    """Idempotent best-effort role assignment via ARM REST. A brand-new agent SP can be unreplicated
    in AAD (ARM then returns a spurious 500), so retry a few times, then hand off to `on_fail`."""
    sub = scope.split("/")[2]
    role_def = f"/subscriptions/{sub}/providers/Microsoft.Authorization/roleDefinitions/{role_id}"
    url = (f"{ARM}{scope}/providers/Microsoft.Authorization/roleAssignments/"
           f"{_assignment_name(scope, principal_id, role_id)}?api-version=2022-04-01")
    payload = json.dumps({"properties": {
        "roleDefinitionId": role_def,
        "principalId": principal_id,
        "principalType": "ServicePrincipal",
    }}).encode()
    attempts = 4
    for attempt in range(1, attempts + 1):
        status, body = _http("PUT", url, token=_arm_token(cred), data=payload,
                             headers={"Content-Type": "application/json"})
        if status in (200, 201):
            print(f"  granted {role_name}.", flush=True)
            return
        text = json.dumps(body).lower() if isinstance(body, (dict, list)) else str(body).lower()
        if status == 409 or "roleassignmentexists" in text or "already exists" in text:
            print(f"  {role_name} already assigned — skipping.", flush=True)
            return
        transient = status >= 500 or "principalnotfound" in text or "does not exist in the directory" in text
        if attempt < attempts and transient:
            print(f"  agent identity not replicated yet (attempt {attempt}/{attempts}, {status}); "
                  f"waiting {DELAY}s...", flush=True)
            time.sleep(DELAY)
            continue
        on_fail(status)
        return


def _assign_model_access(cred, principal_id, scope):
    """Best-effort: grant the agent's identity Foundry User on its project via ARM REST.

    Hosted agents already get default model-inferencing access through the project endpoint,
    so this is belt-and-suspenders — it must never fail the deploy.
    """
    print(f"Granting the agent identity Foundry User at {scope} (best-effort)...", flush=True)
    _assign_role(
        cred, principal_id, scope, FOUNDRY_USER_ROLE_ID, "Foundry User",
        lambda status: print(
            f"  ! could not grant Foundry User ({status}) — continuing; the agent already has default "
            f"model-inferencing access via its project endpoint. Grant manually only if it needs extra "
            f"project/resource access.", flush=True),
    )


def _assign_app_config_reader(cred, principal_id):
    """Best-effort: grant the agent identity App Configuration Data Reader on the proxy's store so it
    can read the `defender-usercontext` feature flag. No-op when the proxy isn't deployed. Non-fatal —
    the flag fail-safes to off, so the agent runs fine without it (just no Defender enrichment)."""
    sub = os.environ.get("AZURE_SUBSCRIPTION_ID", "").strip()
    env = os.environ.get("AZURE_ENV_NAME", "").strip()
    store = os.environ.get("PROXY_APP_CONFIG_NAME", "").strip()
    if not (sub and env and store and principal_id):
        return  # proxy disabled or outputs missing — nothing to grant
    scope = (f"/subscriptions/{sub}/resourceGroups/rg-{env}-provider"
             f"/providers/Microsoft.AppConfiguration/configurationStores/{store}")
    print(f"Granting the agent identity App Configuration Data Reader at {scope} (best-effort)...", flush=True)
    _assign_role(
        cred, principal_id, scope, APP_CONFIG_READER_ROLE_ID, "App Configuration Data Reader",
        lambda status: print(
            f"  ! could not grant App Configuration Data Reader ({status}) — the defender-usercontext "
            f"flag will read as off. Grant manually to enable Defender enrichment.", flush=True),
    )


def _allow_agent_appid(cred, appid):
    """Best-effort: point APIM's gateway-agent-appid named value at the hosted agent's Entra Agent ID
    blueprint appid, so its MCP calls pass validate-azure-ad-token. Non-fatal — the agent reaches the
    model via its project connection regardless; only its direct MCP calls need this."""
    sub = os.environ.get("AZURE_SUBSCRIPTION_ID", "").strip()
    env = os.environ.get("AZURE_ENV_NAME", "").strip()
    apim = os.environ.get("APIM_NAME", "").strip()
    if not (sub and env and apim and appid):
        print("  ! APIM outputs or blueprint appid missing; set the gateway-agent-appid named value "
              "manually if the agent needs the MCP tools.", flush=True)
        return
    rg = f"rg-{env}-provider"
    url = (f"{ARM}/subscriptions/{sub}/resourceGroups/{rg}/providers/Microsoft.ApiManagement"
           f"/service/{apim}/namedValues/gateway-agent-appid?api-version={APIM_NV_API}")
    body = json.dumps({"properties": {
        "displayName": "gateway-agent-appid", "value": appid, "secret": False,
    }}).encode()
    status, resp = _http("PUT", url, token=_arm_token(cred), data=body,
                         headers={"Content-Type": "application/json"})
    if status in (200, 201, 202):
        print(f"+ gateway now allows the hosted-agent blueprint appid {appid}", flush=True)
    else:
        print(f"  ! could not set gateway-agent-appid ({status}): {resp} — set it manually.", flush=True)


# --- Agent deployment -----------------------------------------------------------

def _deploy(endpoint, arm_id, image, model, cred):
    project = AIProjectClient(endpoint=endpoint, credential=cred, allow_preview=True)
    env_vars = {
        "AZURE_AI_PROJECT_ENDPOINT": endpoint,
        "AZURE_AI_MODEL_DEPLOYMENT_NAME": model,
    }
    # The energy MCP route on the shared gateway (empty unless enableMcp). agent.py adds the MCP
    # tool only when this is set.
    mcp_url = os.environ.get("MCP_GATEWAY_URL", "").strip()
    if mcp_url:
        env_vars["MCP_GATEWAY_URL"] = mcp_url
    # Where the agent reads the defender-usercontext feature flag (empty unless enableProxy). Keyless
    # via the agent's own identity; agent.py fail-safes to no enrichment when this is unset.
    app_config = os.environ.get("PROXY_APP_CONFIG_ENDPOINT", "").strip()
    if app_config:
        env_vars["APP_CONFIG_ENDPOINT"] = app_config
    definition = HostedAgentDefinition(
        container_configuration=ContainerConfiguration(image=image),
        # cpu/memory are strings and must match a valid tier exactly. Valid tiers:
        # (0.25,0.5Gi) (0.5,1Gi) (1,2Gi) (2,4Gi). NB the SDK's ContainerMemoryLimit enum
        # emits "4g" which the service rejects — pass the raw "4Gi" string instead.
        cpu="2",
        memory="4Gi",
        protocol_versions=[ProtocolVersionRecord(protocol=AgentEndpointProtocol.RESPONSES, version="1.0.0")],
        environment_variables=env_vars,
    )
    # SDK auto-injects the "Foundry-Features: HostedAgents=..." preview header.
    agent = project.agents.create_version(
        agent_name=AGENT_NAME,
        definition=definition,
        description="Tiny hosted agent reaching the central model via the shared APIM gateway.",
        metadata={"enableVnextExperience": "true"},
    )
    print(f"+ deployed hosted '{agent.name}' (v{getattr(agent, 'version', '?')}) in {endpoint}", flush=True)

    # The agent runs under its own identity, so it needs model access on the project. We also want
    # its Entra Agent ID blueprint appid so the gateway can allow its MCP calls.
    pid = _extract_principal_id(agent)
    bp_appid = _extract_blueprint_appid(agent)
    for _ in range(6):
        if pid and bp_appid:
            break
        time.sleep(5)
        latest = project.agents.get_version(AGENT_NAME, agent.version)
        pid = pid or _extract_principal_id(latest)
        bp_appid = bp_appid or _extract_blueprint_appid(latest)
    if pid and arm_id:
        _assign_model_access(cred, pid, arm_id)
    elif not pid:
        print(f"  ! could not resolve the agent identity; grant Foundry User to '{AGENT_NAME}' manually.", flush=True)
    else:
        print("  ! project ARM id missing; grant Foundry User to the agent identity manually.", flush=True)
    if pid:
        _assign_app_config_reader(cred, pid)  # no-op unless the proxy (App Config store) is deployed
    return bp_appid


def main():
    acr_id = _require("ACR_ID")                  # ARM id of the registry (REST build)
    login_server = _require("ACR_LOGIN_SERVER")  # for the image reference
    endpoints = json.loads(_require("HOSTED_AGENT_PROJECT_ENDPOINTS"))
    arm_ids = json.loads(os.environ.get("HOSTED_AGENT_PROJECT_RESOURCE_IDS", "[]"))
    model = _require("AGENT_MODEL_DEPLOYMENT_NAME")

    cred = AzureDeveloperCliCredential()
    image = _build_image(cred, acr_id, login_server)  # one image, deployed to every consumer

    blueprint_appids = []
    for endpoint, arm_id in _pairs(endpoints, arm_ids):
        for attempt in range(1, RETRIES + 1):
            try:
                bp = _deploy(endpoint, arm_id, image, model, cred)
                if bp and bp not in blueprint_appids:
                    blueprint_appids.append(bp)
                break
            except (ClientAuthenticationError, HttpResponseError) as err:
                if attempt < RETRIES and _transient_auth(err):
                    print(f"  role not propagated for {endpoint} "
                          f"(attempt {attempt}/{RETRIES}); waiting {DELAY}s...", flush=True)
                    time.sleep(DELAY)
                    continue
                raise

    # Allow the hosted agent's Entra Agent ID blueprint appid through the gateway so its MCP calls
    # pass validate-azure-ad-token. Instances share one blueprint appid; if multiple consumers host
    # the agent (each its own blueprint) allow the first and warn — extend gateway-agent-appid by hand.
    if blueprint_appids:
        if len(blueprint_appids) > 1:
            print(f"  ! multiple hosted-agent blueprints {blueprint_appids}; allowing only the first "
                  f"through the gateway. Add the rest to gateway-agent-appid if they need the MCP tools.",
                  flush=True)
        _allow_agent_appid(cred, blueprint_appids[0])
    elif os.environ.get("MCP_GATEWAY_URL", "").strip():
        print("  ! no hosted-agent blueprint appid resolved; the agent's MCP calls will be rejected by "
              "the gateway until you set the gateway-agent-appid named value.", flush=True)

    print(f"Done. Hosted agent '{AGENT_NAME}' runs in each consumer using model '{model}'.")


def _selftest():
    # Pure-logic checks (no Azure calls).
    assert _pairs(["e1", "e2"], ["a1"]) == [("e1", "a1"), ("e2", "")], "must pad missing arm ids"
    assert _pairs(["e1"], ["a1", "a2"]) == [("e1", "a1")], "must zip to shortest of endpoints"

    class _Id:
        principal_id = "p1"

    class _WithId:
        instance_identity = _Id()

    class _NoId:
        instance_identity = None

    assert _extract_principal_id(_WithId()) == "p1"
    assert _extract_principal_id(_NoId()) is None
    assert _extract_principal_id(object()) is None

    class _WithBp:
        blueprint = type("B", (), {"client_id": "app1"})()

    class _NoBp:
        blueprint = None

    assert _extract_blueprint_appid(_WithBp()) == "app1"
    assert _extract_blueprint_appid(_NoBp()) is None
    assert _extract_blueprint_appid(object()) is None

    # Role-assignment name is deterministic (idempotent retries) and scope-sensitive.
    s = "/subscriptions/s1/resourceGroups/rg/providers/Microsoft.CognitiveServices/accounts/a/projects/p"
    assert _assignment_name(s, "pid") == _assignment_name(s, "pid")
    assert _assignment_name(s, "pid") != _assignment_name(s, "other")
    assert s.split("/")[2] == "s1", "subscription id is the 3rd path segment"

    # Build context tars to a non-empty gzip blob with the Dockerfile at the root.
    blob = _tar_context()
    assert blob[:2] == b"\x1f\x8b", "gzip magic"
    with tarfile.open(fileobj=io.BytesIO(blob)) as t:
        assert "Dockerfile" in t.getnames(), "Dockerfile must be at archive root"
    print("self-test ok")


if __name__ == "__main__":
    if "--self-test" in sys.argv:
        _selftest()
    else:
        main()
