"""Governance dashboard — a tiny admin surface for the kill switch (proxy.md).

Two jobs, one page:
  - **Live event stream** — its own Event Hub consumer (consumer group `dashboard`) tails the
    same Defender alerts the proxy ingests and pushes each to the browser over a WebSocket.
  - **Edit the store** — read/write App Configuration's `revocations` key (block / un-block an
    agent) so an operator can drive the kill switch by hand and watch the effect.

Separate Container App, same environment/identity/ACR as the proxy. Keyless throughout (the shared
user-assigned MI already has App Configuration Data Owner + Event Hubs Data Receiver).

ponytail: no auth on this admin surface — it's a public demo dashboard that can write revocations.
Put it behind Container Apps auth (Easy Auth) or IP restrictions before using it anywhere real.
ponytail: broadcast fan-out is in-process (per-connection asyncio.Queue), no Redis/pubsub — fine
for one replica. Add a backplane only if the dashboard ever scales past minReplicas=1.
"""
# NOTE: no `from __future__ import annotations` — it stringizes annotations, and FastAPI can't
# resolve the *locally* defined Ids body model (inside _make_app) from a string, so it treats the
# body as a query param and /api/revocations PUT returns 422.
import asyncio
import collections
import json
import os
import sys
import threading
from datetime import datetime, timezone

ENDPOINT = os.environ.get("APP_CONFIG_ENDPOINT", "").rstrip("/")
CLIENT_ID = os.environ.get("AZURE_CLIENT_ID") or None
KEY = os.environ.get("REVOCATIONS_KEY", "revocations")
LABEL = os.environ.get("REVOCATIONS_LABEL") or None
PORT = int(os.environ.get("PORT", "8080"))
EH_NAMESPACE = os.environ.get("EVENTHUB_FULLY_QUALIFIED_NAMESPACE", "")
EH_NAME = os.environ.get("EVENTHUB_NAME", "")
EH_CONSUMER_GROUP = os.environ.get("EVENTHUB_CONSUMER_GROUP", "$Default")
# Keep in sync with src/proxy/server.py — both read the same alerts.
APPID_KEYS = {"appid", "applicationid", "clientid", "agentid", "aadclientid", "azp"}

CLIENTS: set[asyncio.Queue] = set()
RECENT: collections.deque = collections.deque(maxlen=100)  # replay buffer for new connections
LOOP: asyncio.AbstractEventLoop | None = None  # set at startup, bridges consumer thread -> WS


def extract_appid(alert: dict) -> str | None:
    """Recursive key scan for the offending agent's Entra appid (same logic as the proxy)."""
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


def event_dict(body: str) -> dict:
    """Shape one Event Hub payload for the UI: timestamp, extracted appid, parsed body."""
    try:
        parsed = json.loads(body)
    except Exception:
        parsed = body
    appid = extract_appid(parsed) if isinstance(parsed, dict) else None
    return {"ts": datetime.now(timezone.utc).isoformat(), "appid": appid, "body": parsed}


def publish(event: dict) -> None:
    """Called from the consumer thread: buffer + fan out to every connected WebSocket."""
    RECENT.append(event)
    loop = LOOP
    if loop is None:
        return
    for q in list(CLIENTS):
        loop.call_soon_threadsafe(q.put_nowait, event)


def _appcfg():
    from azure.appconfiguration import AzureAppConfigurationClient
    from azure.identity import DefaultAzureCredential, ManagedIdentityCredential

    cred = ManagedIdentityCredential(client_id=CLIENT_ID) if CLIENT_ID else DefaultAzureCredential()
    return AzureAppConfigurationClient(base_url=ENDPOINT, credential=cred)


def get_revocations() -> list[str]:
    from azure.core.exceptions import ResourceNotFoundError

    try:
        setting = _appcfg().get_configuration_setting(key=KEY, label=LABEL)
        data = json.loads(setting.value) if setting.value else []
    except ResourceNotFoundError:
        return []
    if isinstance(data, dict):
        data = data.get("revoked", [])
    return sorted({str(x) for x in data})


def set_revocations(ids: list[str]) -> list[str]:
    from azure.appconfiguration import ConfigurationSetting

    ordered = sorted({str(x).strip() for x in ids if str(x).strip()})
    _appcfg().set_configuration_setting(ConfigurationSetting(
        key=KEY, label=LABEL, value=json.dumps(ordered), content_type="application/json"))
    return ordered


def _consumer() -> None:
    from azure.eventhub import EventHubConsumerClient
    from azure.identity import DefaultAzureCredential, ManagedIdentityCredential

    cred = ManagedIdentityCredential(client_id=CLIENT_ID) if CLIENT_ID else DefaultAzureCredential()
    consumer = EventHubConsumerClient(
        fully_qualified_namespace=EH_NAMESPACE,
        eventhub_name=EH_NAME,
        consumer_group=EH_CONSUMER_GROUP,
        credential=cred,
    )
    with consumer:
        consumer.receive(
            on_event=lambda _ctx, event: event and publish(event_dict(event.body_as_str())),
            starting_position="@latest",
        )


def _consumer_loop() -> None:
    import time

    while True:
        try:
            _consumer()
        except Exception as e:  # transient EH/AAD error — back off and reconnect
            print(f"[dashboard] event hub consumer error, retrying in 30s: {e}", flush=True)
            time.sleep(30)


PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Governance dashboard</title>
<style>
:root{color-scheme:dark}
body{margin:0;font:14px/1.4 system-ui,sans-serif;background:#0d1117;color:#e6edf3}
header{padding:12px 16px;background:#161b22;border-bottom:1px solid #30363d;display:flex;gap:12px;align-items:center}
header h1{font-size:15px;margin:0;font-weight:600}
#dot{width:9px;height:9px;border-radius:50%;background:#f85149}#dot.on{background:#3fb950}
main{display:grid;grid-template-columns:1fr 340px;gap:16px;padding:16px}
h2{font-size:12px;text-transform:uppercase;letter-spacing:.05em;color:#8b949e;margin:0 0 8px}
.card{background:#161b22;border:1px solid #30363d;border-radius:8px;padding:12px}
#events{max-height:78vh;overflow:auto}
.ev{border-bottom:1px solid #21262d;padding:6px 0;font-family:ui-monospace,monospace;font-size:12px}
.ev .ts{color:#8b949e}.ev .appid{color:#d29922}
.ev pre{margin:4px 0 0;white-space:pre-wrap;word-break:break-all;color:#adbac7;max-height:8em;overflow:auto}
.rev{display:flex;justify-content:space-between;align-items:center;padding:6px 8px;background:#0d1117;border:1px solid #30363d;border-radius:6px;margin-bottom:6px;font-family:ui-monospace,monospace;font-size:12px;word-break:break-all}
button{background:#21262d;color:#e6edf3;border:1px solid #30363d;border-radius:6px;padding:4px 10px;cursor:pointer;font-size:12px}
button:hover{background:#30363d}button.danger{border-color:#f85149;color:#f85149}
button.block{border-color:#d29922;color:#d29922;padding:2px 8px;margin-left:8px}
input{background:#0d1117;color:#e6edf3;border:1px solid #30363d;border-radius:6px;padding:6px 8px;width:100%;box-sizing:border-box;font-family:ui-monospace,monospace}
.row{display:flex;gap:8px;margin-top:8px}.muted{color:#8b949e;font-size:12px}
</style></head><body>
<header><span id="dot"></span><h1>Governance dashboard</h1><span class="muted" id="hub"></span></header>
<main>
  <section><h2>Defender alert stream</h2><div class="card" id="events"><div class="muted">waiting for events…</div></div></section>
  <section><h2>Blocked agents (App Config · revocations)</h2>
    <div class="card">
      <div id="revs"><div class="muted">loading…</div></div>
      <div class="row"><input id="appid" placeholder="appid to block" spellcheck="false"><button onclick="block()">Block</button></div>
      <div class="row"><button onclick="loadRevs()">Refresh</button></div>
    </div>
  </section>
</main>
<script>
const $=s=>document.querySelector(s);
function esc(x){return (typeof x==='string'?x:JSON.stringify(x,null,2)).replace(/[&<>]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]))}
let first=true;
function addEvent(ev){
  const box=$('#events'); if(first){box.innerHTML='';first=false;}
  const d=document.createElement('div'); d.className='ev';
  const appid=ev.appid?`<span class="appid">${esc(ev.appid)}</span> <button class="block" onclick="block('${esc(ev.appid)}')">block</button>`:'<span class="muted">no appid</span>';
  d.innerHTML=`<div><span class="ts">${esc(ev.ts)}</span> — ${appid}</div><pre>${esc(ev.body)}</pre>`;
  box.insertBefore(d,box.firstChild);
}
function connect(){
  const ws=new WebSocket((location.protocol==='https:'?'wss':'ws')+'://'+location.host+'/ws');
  ws.onopen=()=>$('#dot').classList.add('on');
  ws.onmessage=e=>addEvent(JSON.parse(e.data));
  ws.onclose=()=>{$('#dot').classList.remove('on');setTimeout(connect,2000);};
}
async function loadRevs(){
  const j=await (await fetch('/api/revocations')).json();
  const box=$('#revs');
  box.innerHTML = j.revoked.length ? '' : '<div class="muted">none blocked</div>';
  for(const id of j.revoked){
    const d=document.createElement('div'); d.className='rev';
    d.innerHTML=`<span>${esc(id)}</span><button class="danger" onclick="unblock('${esc(id)}')">unblock</button>`;
    box.appendChild(d);
  }
}
async function block(id){
  id=id||$('#appid').value.trim(); if(!id) return;
  await fetch('/api/revocations/'+encodeURIComponent(id),{method:'POST'});
  $('#appid').value=''; loadRevs();
}
async function unblock(id){
  await fetch('/api/revocations/'+encodeURIComponent(id),{method:'DELETE'}); loadRevs();
}
connect(); loadRevs();
</script></body></html>"""


def _make_app():
    from fastapi import FastAPI, WebSocket, WebSocketDisconnect
    from fastapi.responses import HTMLResponse
    from pydantic import BaseModel

    app = FastAPI(title="governance-dashboard")

    class Ids(BaseModel):
        revoked: list[str] = []

    @app.get("/", response_class=HTMLResponse)
    def index():
        return PAGE

    @app.get("/health")
    def health():
        return {"status": "ok", "clients": len(CLIENTS), "buffered": len(RECENT)}

    @app.get("/api/revocations")
    def api_get():
        return {"revoked": get_revocations()}

    @app.put("/api/revocations")
    def api_put(body: Ids):
        return {"revoked": set_revocations(body.revoked)}

    @app.post("/api/revocations/{appid}")
    def api_add(appid: str):
        return {"revoked": set_revocations(get_revocations() + [appid])}

    @app.delete("/api/revocations/{appid}")
    def api_del(appid: str):
        return {"revoked": set_revocations([x for x in get_revocations() if x != appid])}

    @app.websocket("/ws")
    async def ws(websocket: WebSocket):
        await websocket.accept()
        q: asyncio.Queue = asyncio.Queue()
        CLIENTS.add(q)
        try:
            for ev in list(RECENT):
                await websocket.send_json(ev)
            while True:
                await websocket.send_json(await q.get())
        except WebSocketDisconnect:
            pass
        finally:
            CLIENTS.discard(q)

    @app.on_event("startup")
    async def _startup():
        global LOOP
        LOOP = asyncio.get_running_loop()
        if EH_NAMESPACE and EH_NAME:
            print(f"[dashboard] alert consumer on {EH_NAMESPACE}/{EH_NAME} ({EH_CONSUMER_GROUP})", flush=True)
            threading.Thread(target=_consumer_loop, daemon=True).start()

    return app


def _selftest() -> None:
    assert extract_appid({"properties": {"extendedProperties": {"AppId": "guid-1"}}}) == "guid-1"
    assert extract_appid({"entities": [{"Type": "user", "AadClientId": "guid-2"}]}) == "guid-2"
    assert extract_appid({"nothing": "here"}) is None
    ev = event_dict('{"properties":{"extendedProperties":{"AppId":"guid-3"}}}')
    assert ev["appid"] == "guid-3" and ev["body"]["properties"]["extendedProperties"]["AppId"] == "guid-3"
    assert event_dict("not json")["body"] == "not json"
    # publish with no loop must not raise and must buffer
    RECENT.clear()
    publish({"ts": "t", "appid": "x", "body": {}})
    assert len(RECENT) == 1
    print("self-test ok")


if __name__ == "__main__":
    if "--self-test" in sys.argv:
        _selftest()
    else:
        import uvicorn

        uvicorn.run(_make_app(), host="0.0.0.0", port=PORT)
