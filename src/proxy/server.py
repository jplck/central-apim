"""Governance proxy — Phase 1 kill switch (proxy.md).

APIM calls POST /check synchronously with {"agent_id": "..."} and enforces the returned
verdict. Revocation is DATA, not code: the revoked set is read from Azure App Configuration
(key `revocations`, a JSON array) with a managed identity and refreshed on a poll. Killing an
agent is one `az appconfig kv set` — no redeploy, no policy edit.

ponytail: the decision is a set-membership check, not an OPA/Rego engine. Rego + ACS earn
their place in Phase 3 (trust rings, rate limits, multi-dimensional policy); a kill switch
does not need them. Upgrade path: swap `decide()` for an ACS/OPA call, same wire contract.
"""
from __future__ import annotations

import json
import os
import sys
import threading
import time

ENDPOINT = os.environ.get("APP_CONFIG_ENDPOINT", "").rstrip("/")
CLIENT_ID = os.environ.get("AZURE_CLIENT_ID") or None  # user-assigned MI to pick
KEY = os.environ.get("REVOCATIONS_KEY", "revocations")
LABEL = os.environ.get("REVOCATIONS_LABEL") or None
POLL_SECONDS = int(os.environ.get("POLL_SECONDS", "10"))
PORT = int(os.environ.get("PORT", "8080"))


class _State:
    """Last-known-good revocation set. `loaded` gates fail-closed until the first read."""

    def __init__(self) -> None:
        self.loaded = False
        self.revoked: set[str] = set()
        self.lock = threading.Lock()

    def snapshot(self) -> tuple[bool, set[str]]:
        with self.lock:
            return self.loaded, set(self.revoked)

    def set(self, revoked: set[str]) -> None:
        with self.lock:
            self.revoked = revoked
            self.loaded = True


state = _State()


def decide(agent_id: str, loaded: bool, revoked: set[str]) -> tuple[str, str]:
    """Pure decision. Fail closed until we have a revocation list; deny revoked agents."""
    if not loaded:
        return "deny", "revocation list unavailable (fail-closed)"
    if agent_id and agent_id in revoked:
        return "deny", "agent revoked"
    return "allow", "ok"


def parse_revocations(value: str | None) -> set[str]:
    """App Config value -> set of agent ids. Accepts a JSON array or {"revoked": [...]}."""
    if not value or not value.strip():
        return set()
    data = json.loads(value)
    if isinstance(data, dict):
        data = data.get("revoked", [])
    return {str(x) for x in data}


def _client():
    from azure.appconfiguration import AzureAppConfigurationClient
    from azure.identity import DefaultAzureCredential, ManagedIdentityCredential

    cred = ManagedIdentityCredential(client_id=CLIENT_ID) if CLIENT_ID else DefaultAzureCredential()
    return AzureAppConfigurationClient(base_url=ENDPOINT, credential=cred)


def refresh(client) -> None:
    """ponytail: re-fetch the small key each poll; add an ETag conditional if it ever grows."""
    from azure.core.exceptions import ResourceNotFoundError

    try:
        setting = client.get_configuration_setting(key=KEY, label=LABEL)
        revoked = parse_revocations(setting.value)
    except ResourceNotFoundError:
        revoked = set()  # key not created yet == no revocations (we're connected, so loaded)
    state.set(revoked)


def _poller() -> None:
    client = _client()
    while True:
        try:
            refresh(client)
        except Exception as e:  # keep last-known-good on transient errors
            print(f"[proxy] refresh error: {e}", flush=True)
        time.sleep(POLL_SECONDS)


def _make_app():
    from fastapi import FastAPI
    from pydantic import BaseModel

    app = FastAPI(title="governance-proxy")

    class CheckReq(BaseModel):
        agent_id: str = ""

    @app.post("/check")
    def check(req: CheckReq):
        loaded, revoked = state.snapshot()
        verdict, reason = decide(req.agent_id, loaded, revoked)
        return {"verdict": verdict, "reason": reason, "agent_id": req.agent_id}

    @app.get("/health")
    def health():
        loaded, revoked = state.snapshot()
        return {"status": "ok", "loaded": loaded, "revoked_count": len(revoked)}

    @app.on_event("startup")
    def _startup():
        if ENDPOINT:
            try:
                refresh(_client())  # one sync load so we don't fail-closed longer than needed
            except Exception as e:
                print(f"[proxy] initial load failed (fail-closed until it succeeds): {e}", flush=True)
        threading.Thread(target=_poller, daemon=True).start()

    return app


def _selftest() -> None:
    assert parse_revocations(None) == set()
    assert parse_revocations("") == set()
    assert parse_revocations('["a","b"]') == {"a", "b"}
    assert parse_revocations('{"revoked":["c"]}') == {"c"}
    # fail closed before load
    assert decide("a", False, set()) == ("deny", "revocation list unavailable (fail-closed)")
    # loaded, empty list -> allow
    assert decide("a", True, set())[0] == "allow"
    # loaded, revoked -> deny
    assert decide("a", True, {"a"})[0] == "deny"
    assert decide("a", True, {"b"})[0] == "allow"
    # empty agent id is never matched as revoked
    assert decide("", True, {""})[0] == "allow"
    print("self-test ok")


if __name__ == "__main__":
    if "--self-test" in sys.argv:
        _selftest()
    else:
        import uvicorn

        uvicorn.run(_make_app(), host="0.0.0.0", port=PORT)
