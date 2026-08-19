#!/usr/bin/env python3
"""Post-provision hook: build the governance-proxy image into its ACR, swap it (and the real
port 8080) onto the Container App from infra/proxy.bicep, then point APIM's `governance-proxy-host`
named value at it so the kill-switch policy in provider.bicep activates.

Reuses the ACR-build / app-swap plumbing from deploy_mcp.py (same directory). Runs as the azd
deploy identity via ARM REST. Reads azd provisioning outputs from the environment.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from azure.identity import AzureDeveloperCliCredential

import deploy_mcp as d  # reuse _build_image / _update_app / _http / _arm_token / _require

CONTEXT = Path(__file__).resolve().parents[2] / "src" / "proxy"
REPO = "governance-proxy"
TARGET_PORT = 8080  # server.py listens here; bicep provisions the placeholder on 80
APIM_API = "2024-05-01"


def _set_named_value(cred, host: str) -> None:
    """Point APIM's `governance-proxy-host` named value at the proxy host (no scheme). Empty
    inputs -> skip (the policy stays inert at its disabled-sentinel default, so the gateway is
    unaffected)."""
    sub = os.environ.get("AZURE_SUBSCRIPTION_ID", "").strip()
    env = os.environ.get("AZURE_ENV_NAME", "").strip()
    apim = os.environ.get("APIM_NAME", "").strip()
    if not (sub and env and apim and host):
        print("[proxy] APIM/proxy outputs missing; leaving kill-switch named value disabled.", flush=True)
        return
    rg = f"rg-{env}-provider"
    url = (f"{d.ARM}/subscriptions/{sub}/resourceGroups/{rg}/providers/Microsoft.ApiManagement"
           f"/service/{apim}/namedValues/governance-proxy-host?api-version={APIM_API}")
    body = json.dumps({
        "properties": {"displayName": "governance-proxy-host", "value": host, "secret": False}
    }).encode()
    status, resp = d._http("PUT", url, token=d._arm_token(cred), data=body,
                           headers={"Content-Type": "application/json"})
    if status not in (200, 201, 202):
        raise RuntimeError(f"set governance-proxy-host named value failed ({status}): {resp}")
    print(f"+ APIM kill-switch armed: governance-proxy-host -> {host}", flush=True)


def main():
    acr_id = d._require("PROXY_ACR_ID")
    login_server = d._require("PROXY_ACR_LOGIN_SERVER")
    app_id = d._require("PROXY_APP_ID")

    cred = AzureDeveloperCliCredential()
    image = d._build_image(cred, acr_id, login_server, context=CONTEXT, repo=REPO)
    fqdn = d._update_app(cred, app_id, image, port=TARGET_PORT, uri_suffix="/health",
                         label="Governance proxy")
    # Store the host only (no scheme): the APIM policy prepends https:// and a `//` inside the
    # guard expression would break APIM's parser.
    proxy_uri = os.environ.get("PROXY_URI", "").strip()
    host = fqdn or (proxy_uri.split("://", 1)[-1].split("/")[0] if proxy_uri else "")
    _set_named_value(cred, host)
    print("Done. The governance proxy is deployed and the APIM kill switch is armed.")


if __name__ == "__main__":
    main()
