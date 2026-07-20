#!/usr/bin/env python3
"""Post-provision hook: create a prompt agent in each consumer Foundry project.

The agent routes to the provider's model through the shared APIM gateway
(model deployment name = "<connection>/<model>"). Reads azd provisioning outputs
from the environment. Idempotent: skips a project if the agent already exists.
"""
import json
import os
import sys
import time

from azure.ai.projects import AIProjectClient
from azure.ai.projects.models import PromptAgentDefinition
from azure.core.exceptions import ClientAuthenticationError, HttpResponseError
from azure.identity import AzureDeveloperCliCredential

AGENT_NAME = os.environ.get("AGENT_NAME", "gateway-test")
# Entra data-plane role propagation after provisioning can take a few minutes.
RETRIES = int(os.environ.get("AGENT_HOOK_RETRIES", "12"))
DELAY = int(os.environ.get("AGENT_HOOK_DELAY", "20"))
INSTRUCTIONS = (
    "You are a helpful assistant. You answer using a model served through the "
    "shared Azure API Management gateway."
)


def _require(name):
    val = os.environ.get(name, "").strip()
    if not val:
        sys.exit(f"{name} is not set. Run this via `azd up`/`azd provision` so outputs are available.")
    return val


def _exists(project, name):
    try:
        return any(True for _ in project.agents.list_versions(name))
    except HttpResponseError as err:
        if getattr(err, "status_code", None) == 404:
            return False  # no such agent yet
        raise  # auth/propagation errors bubble up to the retry loop


def _transient_auth(err):
    """True if the error is a not-yet-propagated RBAC failure worth retrying."""
    code = getattr(err, "status_code", None)
    if code in (401, 403):
        return True
    if isinstance(err, ClientAuthenticationError):
        return True
    msg = str(getattr(err, "message", "") or err).lower()
    return any(s in msg for s in ("does not have permission", "not authorized", "forbidden", "authorization"))


def _create(endpoint, model, cred):
    project = AIProjectClient(endpoint=endpoint, credential=cred, allow_preview=True)
    if _exists(project, AGENT_NAME):
        print(f"= '{AGENT_NAME}' already exists in {endpoint} - skipping")
        return
    agent = project.agents.create_version(
        agent_name=AGENT_NAME,
        definition=PromptAgentDefinition(model=model, instructions=INSTRUCTIONS),
        description="Smoke-test agent reaching the provider model via the shared APIM gateway.",
    )
    print(f"+ created '{agent.name}' (v{getattr(agent, 'version', '?')}) in {endpoint}")


def main():
    endpoints = json.loads(_require("CONSUMER_PROJECT_ENDPOINTS"))
    model = _require("AGENT_MODEL_DEPLOYMENT_NAME")
    # azd runs this hook, so use its identity (= AZURE_PRINCIPAL_ID, the principal
    # the Bicep grants Foundry User to). DefaultAzureCredential would pick the az CLI
    # login first, which may be a different tenant/identity and 403.
    cred = AzureDeveloperCliCredential()

    for endpoint in endpoints:
        for attempt in range(1, RETRIES + 1):
            try:
                _create(endpoint, model, cred)
                break
            except (ClientAuthenticationError, HttpResponseError) as err:
                if attempt < RETRIES and _transient_auth(err):
                    print(f"  role not propagated for {endpoint} "
                          f"(attempt {attempt}/{RETRIES}); waiting {DELAY}s...", flush=True)
                    time.sleep(DELAY)
                    continue
                raise

    print(f"Done. Use model deployment name '{model}' when running these agents.")


if __name__ == "__main__":
    main()
