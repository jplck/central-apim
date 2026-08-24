"""Governance proxy — Phase 1 kill switch (proxy.md).

APIM calls POST /check synchronously with {"agent_id": "..."} and enforces the returned
verdict. Revocation is DATA, not code: the revoked set is read from Azure App Configuration
(key `revocations`, a JSON array) with a managed identity and refreshed on a poll. Killing an
agent is one `az appconfig kv set` — no redeploy, no policy edit.

ponytail: the decision is a set-membership check, not an OPA/Rego engine. Rego + ACS earn
their place in Phase 3 (trust rings, rate limits, multi-dimensional policy); a kill switch
does not need them. Upgrade path: swap `decide()` for an ACS/OPA call, same wire contract.
"""
# NOTE: no `from __future__ import annotations` — it stringizes annotations, and FastAPI can't
# resolve the *locally* defined CheckReq body model (inside _make_app) from a string, so it
# treats the body as a query param and every /check returns 422 -> kill switch fail-closes to 403.
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
# Phase 2: Defender-alert ingest. When these are set, a background consumer reads AI alerts off the
# Event Hub and revokes the offending appid — keyless, via the same user-assigned MI (AZURE_CLIENT_ID).
EH_NAMESPACE = os.environ.get("EVENTHUB_FULLY_QUALIFIED_NAMESPACE", "")
EH_NAME = os.environ.get("EVENTHUB_NAME", "")
EH_CONSUMER_GROUP = os.environ.get("EVENTHUB_CONSUMER_GROUP", "$Default")
# Candidate key names (normalized: lowercased, non-alphanumerics stripped) under which a Defender
# alert may carry the offending agent's Entra appid.
APPID_KEYS = {"appid", "applicationid", "clientid", "agentid", "aadclientid", "azp"}


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


def extract_appid(alert: dict) -> str | None:
    """Best-effort: pull the offending agent's Entra appid out of a Defender for Cloud alert.

    ponytail: a recursive key scan over the alert JSON, not a fixed schema path — Defender alert
    shapes vary and drift, and A365 / the SDK stamp the identity in different places. Set
    ALERT_APPID_JSONPATH (dotted) to force a specific field if a real alert nests it ambiguously.
    """
    override = os.environ.get("ALERT_APPID_JSONPATH")
    if override:
        cur: object = alert
        for part in override.split("."):
            cur = cur.get(part) if isinstance(cur, dict) else None
            if cur is None:
                break
        if isinstance(cur, str) and cur.strip():
            return cur.strip()
    stack: list = [alert]
    while stack:
        node = stack.pop()
        if isinstance(node, dict):
            for k, v in node.items():
                key = "".join(ch for ch in k.lower() if ch.isalnum())
                if isinstance(v, str) and v.strip() and key in APPID_KEYS:
                    return v.strip()
                if isinstance(v, (dict, list)):
                    stack.append(v)
        elif isinstance(node, list):
            stack.extend(node)
    return None


def add_revocation(client, appid: str) -> bool:
    """Read-merge-write the revocations set (idempotent). Returns True if newly added. Needs App
    Configuration Data Owner on the store."""
    from azure.appconfiguration import ConfigurationSetting
    from azure.core.exceptions import ResourceNotFoundError

    try:
        setting = client.get_configuration_setting(key=KEY, label=LABEL)
        current = parse_revocations(setting.value)
    except ResourceNotFoundError:
        current = set()
    if appid in current:
        return False
    current.add(appid)
    client.set_configuration_setting(ConfigurationSetting(
        key=KEY, label=LABEL, value=json.dumps(sorted(current)), content_type="application/json"))
    return True


def _handle_alert(appcfg, body: str) -> None:
    """Parse one Event Hub payload (a single alert or a continuous-export {"records":[...]}) and
    revoke every appid found. Never raises — a bad message must not kill the consumer."""
    try:
        payload = json.loads(body)
    except Exception:
        return
    records = payload.get("records", [payload]) if isinstance(payload, dict) else payload
    for rec in records if isinstance(records, list) else [records]:
        if not isinstance(rec, dict):
            continue
        appid = extract_appid(rec)
        if not appid:
            print("[proxy] alert with no extractable appid (set ALERT_APPID_JSONPATH?)", flush=True)
            continue
        try:
            if add_revocation(appcfg, appid):
                refresh(appcfg)  # reflect the kill in `state` now — don't wait for the next poll
                print(f"[proxy] revoked {appid} from Defender alert", flush=True)
        except Exception as e:
            print(f"[proxy] revoke write failed for {appid}: {e}", flush=True)


def _consumer() -> None:
    """Stream Defender alerts off the Event Hub and revoke offending appids. Keyless MI.

    ponytail: no checkpoint store, starts at @latest — a demo ingestor, not an exactly-once pipeline.
    Add a Blob checkpoint store if you need replay across restarts.
    """
    from azure.eventhub import EventHubConsumerClient
    from azure.identity import DefaultAzureCredential, ManagedIdentityCredential

    cred = ManagedIdentityCredential(client_id=CLIENT_ID) if CLIENT_ID else DefaultAzureCredential()
    appcfg = _client()
    consumer = EventHubConsumerClient(
        fully_qualified_namespace=EH_NAMESPACE,
        eventhub_name=EH_NAME,
        consumer_group=EH_CONSUMER_GROUP,
        credential=cred,
    )
    with consumer:
        consumer.receive(
            on_event=lambda _ctx, event: event and _handle_alert(appcfg, event.body_as_str()),
            starting_position="@latest",
        )


def _consumer_loop() -> None:
    while True:
        try:
            _consumer()
        except Exception as e:  # transient EH/AAD error — back off and reconnect, never exit
            print(f"[proxy] event hub consumer error, retrying in 30s: {e}", flush=True)
            time.sleep(30)


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
        if verdict == "deny":  # every 403 the gateway returns starts here — make it explain itself
            print(f"[proxy] DENY agent_id={req.agent_id!r} reason={reason!r} "
                  f"loaded={loaded} revoked_count={len(revoked)}", flush=True)
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
        if EH_NAMESPACE and EH_NAME:
            print(f"[proxy] Defender alert consumer on {EH_NAMESPACE}/{EH_NAME} ({EH_CONSUMER_GROUP})", flush=True)
            threading.Thread(target=_consumer_loop, daemon=True).start()

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
    # Phase 2: alert -> appid extraction (recursive, schema-agnostic)
    assert extract_appid({"properties": {"extendedProperties": {"AppId": "guid-1"}}}) == "guid-1"
    assert extract_appid({"a": {"b": [{"applicationId": "guid-2"}]}}) == "guid-2"
    assert extract_appid({"entities": [{"Type": "user", "AadClientId": "guid-3"}]}) == "guid-3"
    assert extract_appid({"azp": "guid-4"}) == "guid-4"
    assert extract_appid({"nothing": "here", "count": 3}) is None
    print("self-test ok")


if __name__ == "__main__":
    if "--self-test" in sys.argv:
        _selftest()
    else:
        import uvicorn

        uvicorn.run(_make_app(), host="0.0.0.0", port=PORT)
