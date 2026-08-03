#!/usr/bin/env python3
"""Post-provision hook: build the energy customer-profile MCP image into its ACR and
swap it (and the real target port) onto the Container App provisioned by infra/mcp.bicep.

Runs as the azd deploy identity (AzureDeveloperCliCredential) via ARM REST (stdlib
urllib), matching create_hosted_agents.py — no dependency on the `az` CLI being logged
into the same account. Reads azd provisioning outputs from the environment.
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
from datetime import datetime, timezone
from pathlib import Path

from azure.identity import AzureDeveloperCliCredential

CONTEXT = Path(__file__).resolve().parents[2] / "src" / "mcp-energy"
REPO = "energy-mcp"
TARGET_PORT = 8000  # the port server.py listens on (bicep provisions the placeholder on 80)
ARM = "https://management.azure.com"
ACR_API = "2019-06-01-preview"
APP_API = "2024-03-01"


def _require(name: str) -> str:
    val = os.environ.get(name, "").strip()
    if not val:
        sys.exit(f"{name} is not set. Run this via `azd up`/`azd provision` so outputs are available.")
    return val


def _arm_token(cred) -> str:
    return cred.get_token(f"{ARM}/.default").token


def _http(method, url, token=None, data=None, headers=None):
    """Minimal REST call. Returns (status, parsed_body); JSON is decoded when present."""
    hdrs = dict(headers or {})
    if token:
        hdrs["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, method=method, data=data, headers=hdrs)
    try:
        resp = urllib.request.urlopen(req)
        raw, status, ctype = resp.read(), resp.status, resp.headers.get("Content-Type", "")
    except urllib.error.HTTPError as e:
        raw, status, ctype = e.read(), e.code, e.headers.get("Content-Type", "")
    if raw and "json" in ctype:
        try:
            return status, json.loads(raw)
        except ValueError:
            pass
    return status, (raw.decode(errors="replace") if raw else "")


def _tar_context(context: Path = CONTEXT) -> bytes:
    """gzip-tar the build context (Dockerfile at root) for ACR to consume."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for p in sorted(context.iterdir()):
            if p.name.startswith(".") or p.name == "__pycache__":
                continue
            tar.add(p, arcname=p.name)
    return buf.getvalue()


def _build_image(cred, acr_id, login_server) -> str:
    """Build + push the image inside ACR via REST (equivalent of `az acr build`)."""
    tag = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    repo_tag = f"{REPO}:{tag}"
    print(f"Building {login_server}/{repo_tag} from {CONTEXT} via ACR REST...", flush=True)

    status, body = _http("POST", f"{ARM}{acr_id}/listBuildSourceUploadUrl?api-version={ACR_API}",
                         token=_arm_token(cred), data=b"")
    if status >= 400 or not isinstance(body, dict):
        raise RuntimeError(f"listBuildSourceUploadUrl failed ({status}): {body}")
    upload_url, relative_path = body["uploadUrl"], body["relativePath"]

    status, body = _http("PUT", upload_url, data=_tar_context(), headers={"x-ms-blob-type": "BlockBlob"})
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
                         token=_arm_token(cred), data=run_req, headers={"Content-Type": "application/json"})
    if status >= 400 or not isinstance(body, dict):
        raise RuntimeError(f"scheduleRun failed ({status}): {body}")
    run_id = (body.get("properties") or {}).get("runId") or body.get("name")
    print(f"  ACR build queued (run {run_id}); waiting...", flush=True)

    for _ in range(80):  # up to ~20 min
        time.sleep(15)
        status, body = _http("GET", f"{ARM}{acr_id}/runs/{run_id}?api-version={ACR_API}", token=_arm_token(cred))
        state = (body.get("properties") or {}).get("status") if isinstance(body, dict) else None
        if state == "Succeeded":
            return f"{login_server}/{repo_tag}"
        if state in ("Failed", "Error", "Canceled", "Timeout"):
            raise RuntimeError(f"ACR build {state} (run {run_id})")
    raise RuntimeError(f"ACR build did not finish in time (run {run_id})")


def _put_body(app: dict, image: str, port: int) -> dict:
    """Build a minimal, valid PUT body from a GET response, swapping image + target port.

    Container Apps rejects a PUT whose user-assigned identity map carries the read-only
    principalId/clientId, so the identity keys are kept but their values emptied.
    """
    props = app["properties"]
    config = dict(props["configuration"])
    config["ingress"] = dict(config.get("ingress") or {})
    config["ingress"]["targetPort"] = port
    config["ingress"].pop("fqdn", None)  # read-only

    template = json.loads(json.dumps(props["template"]))  # deep copy
    template["containers"][0]["image"] = image

    ident = app.get("identity") or {}
    uai = ident.get("userAssignedIdentities") or {}
    identity = {"type": ident.get("type", "None")}
    if uai:
        identity["userAssignedIdentities"] = {k: {} for k in uai}

    return {
        "location": app["location"],
        "tags": app.get("tags", {}),
        "identity": identity,
        "properties": {
            "managedEnvironmentId": props["managedEnvironmentId"],
            "configuration": config,
            "template": template,
        },
    }


def _update_app(cred, app_id, image, port=TARGET_PORT) -> None:
    """Swap the built image + real target port onto the container app, then wait for it."""
    status, app = _http("GET", f"{ARM}{app_id}?api-version={APP_API}", token=_arm_token(cred))
    if status >= 400 or not isinstance(app, dict):
        raise RuntimeError(f"GET container app failed ({status}): {app}")

    print(f"Swapping image -> {image} and targetPort -> {port} on {app_id.split('/')[-1]}...", flush=True)
    body = json.dumps(_put_body(app, image, port)).encode()
    status, resp = _http("PUT", f"{ARM}{app_id}?api-version={APP_API}",
                         token=_arm_token(cred), data=body, headers={"Content-Type": "application/json"})
    if status not in (200, 201):
        raise RuntimeError(f"PUT container app failed ({status}): {resp}")

    for _ in range(40):  # up to ~7 min
        status, app = _http("GET", f"{ARM}{app_id}?api-version={APP_API}", token=_arm_token(cred))
        state = (app.get("properties") or {}).get("provisioningState") if isinstance(app, dict) else None
        if state == "Succeeded":
            fqdn = app["properties"]["configuration"]["ingress"].get("fqdn", "")
            print(f"+ MCP server live at https://{fqdn}/mcp", flush=True)
            return
        if state in ("Failed", "Canceled"):
            raise RuntimeError(f"container app update {state}")
        time.sleep(10)
    raise RuntimeError("container app update did not reach Succeeded in time")


def main():
    acr_id = _require("MCP_ACR_ID")
    login_server = _require("MCP_ACR_LOGIN_SERVER")
    app_id = _require("MCP_APP_ID")

    cred = AzureDeveloperCliCredential()
    image = _build_image(cred, acr_id, login_server)
    _update_app(cred, app_id, image)
    print("Done. The energy customer-profile MCP server is deployed.")


def _selftest():
    app = {
        "location": "westeurope",
        "tags": {"azd-env-name": "dev"},
        "identity": {"type": "UserAssigned",
                     "userAssignedIdentities": {"/subscriptions/s/rg/id": {"principalId": "p", "clientId": "c"}}},
        "properties": {
            "provisioningState": "Succeeded",
            "managedEnvironmentId": "/subscriptions/s/rg/env",
            "configuration": {"ingress": {"external": True, "targetPort": 80, "fqdn": "old.example.io"},
                              "registries": [{"server": "acr.io", "identity": "/subscriptions/s/rg/id"}]},
            "template": {"containers": [{"name": "mcp", "image": "placeholder:1"}], "scale": {"minReplicas": 1}},
        },
    }
    out = _put_body(app, "acr.io/energy-mcp:20260101", 8000)
    assert out["properties"]["template"]["containers"][0]["image"] == "acr.io/energy-mcp:20260101"
    assert out["properties"]["configuration"]["ingress"]["targetPort"] == 8000
    assert "fqdn" not in out["properties"]["configuration"]["ingress"], "read-only fqdn must be dropped"
    assert out["identity"]["userAssignedIdentities"] == {"/subscriptions/s/rg/id": {}}, "UAMI values must be emptied"
    assert out["properties"]["managedEnvironmentId"] == "/subscriptions/s/rg/env"
    # Input is not mutated (deep copy).
    assert app["properties"]["template"]["containers"][0]["image"] == "placeholder:1"
    assert app["properties"]["configuration"]["ingress"]["targetPort"] == 80

    blob = _tar_context()
    assert blob[:2] == b"\x1f\x8b", "gzip magic"
    with tarfile.open(fileobj=io.BytesIO(blob)) as t:
        names = t.getnames()
    assert "Dockerfile" in names and "data.json" in names, f"missing build files: {names}"
    print("self-test ok")


if __name__ == "__main__":
    if "--self-test" in sys.argv:
        _selftest()
    else:
        main()
