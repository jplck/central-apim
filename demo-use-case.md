# Demo use case — Defender / Agent 365 kill switch → central governance proxy

**One-line thesis.** Disabling an agent's *blueprint* in Entra/Agent 365 stops **new** tokens
but lets **already-issued** tokens keep hitting models and tools until they expire. This demo
forwards the same Defender / Agent 365 kill signal to a **central governance proxy** that every
agent→resource call already passes through, so the *next* request from any surviving instance is
denied at the gateway in seconds — not after the token's 60–90 min lifetime.

The whole point is a **single central gateway for all interactions between agents and
resources**: one choke point, one revocation, every model and every tool covered.

---

## 1. The use case

An **energy-supplier customer agent** (Microsoft Agent Framework, hosted in Foundry) is
registered in **Agent 365** under an Entra Agent ID **blueprint**. It emits telemetry to A365
(Observe) and consumes two things through the central **APIM gateway**, keylessly, with Entra
tokens:

- **GPT models** on the provider Foundry resource (models route), and
- the **energy MCP server** (`src/mcp-energy`) exposed as a native APIM MCP server.

All of that is **given / already wired up**. The interesting moment is an incident:

1. **Detection.** Defender for Cloud (AI threat protection) or Defender XDR / Agent 365 *Secure*
   flags the agent — jailbreak, credential theft, indirect prompt injection, or anomalous tool
   invocation.
2. **Containment (directory).** SecOps trips the kill switch: **disable the blueprint** in Entra
   Agent ID (`accountEnabled = false`). New tokens for every instance of that blueprint stop
   being issued.
3. **The gap.** Instances that *already* hold a valid token keep calling models and the MCP
   server. CAE doesn't cover our custom gateway, so APIM's `validate-azure-ad-token` still
   accepts those coasting tokens. **The disabled identity is still operational against
   resources.** ← this is the problem this demo solves.
4. **Containment (runtime).** The *same* kill signal is forwarded to the **central governance
   proxy**, which adds the blueprint's app id to a **revocation set**. APIM consults the proxy on
   **every** model and MCP call, so the surviving instances are denied at the gateway on their
   next request — within the poll interval (≤10 s), regardless of token validity.

```
  Defender for Cloud (AI) ─┐
  Defender XDR / A365 Secure├─ signal ─► Signal ingestor ─► App Config `revocations`  (blueprint appId)
  Sentinel SOAR playbook  ─┘                                          ▲ poll ≤10s
                                                                      │
  agent (coasting token) ─► APIM gateway ─ /check {agent_id=appid} ─► governance proxy ─► deny ─► 403
        (models route + energy-MCP route both run the shared governance-check fragment)
```

**Open for future expansion** (Section 2.6 / Phase 3): register the energy MCP as a **BYO MCP
tool in Agent 365** (agents call it via the A365 Tooling Gateway); add more signal producers
(Purview, Entra risk); revoke per-instance (`oid`) as well as per-blueprint; extend the proxy
from a kill switch to trust-rings / rate-limits behind the same `/check` contract.

---

## 2. Technical analysis

### 2.1 The gap: blueprint disable ≠ session kill

| | Entra / A365 blueprint disable | Central proxy revocation |
|---|---|---|
| Effect | `accountEnabled=false` — stops **new** token issuance | denies calls whose token is **already** live |
| Latency | seconds for new tokens | ≤ poll interval (10 s) for existing sessions |
| Existing tokens | **valid until `exp` (≈60–90 min)** | **blocked at next call** |
| Covers our gateway? | ❌ CAE only covers Microsoft Graph as resource; MIs unsupported | ✅ it *is* the gateway check |

CAE (Continuous Access Evaluation) would be the directory-native fix, but workload-identity CAE
only propagates to **Microsoft Graph** as the resource provider — not our APIM / Container App —
and managed identities are unsupported. So the directory says "disabled" while the token still
works against resources. The proxy is the enforcement point that closes that window.

### 2.2 What "block the blueprint" means on the wire

Entra Agent ID object model: **Blueprint** (application) → **BlueprintPrincipal** (SP) →
**Agent Identity** (SP) per instance. Instances authenticate using the **blueprint's**
credentials, so on the token:

- `appid` / `azp` = the **blueprint app (client) id** — shared by *all* instances of that blueprint.
- `oid` / `sub` = the **per-instance** identity.

Our APIM kill-switch policy already extracts `agentId` from `appid` (fallback `azp`) and posts
`{"agent_id": "<blueprintAppId>"}` to the proxy `/check` (`infra/provider.bicep`, `killSwitchXml`).
Therefore **one revocation entry keyed by the blueprint app id blocks every instance** of that
blueprint — an exact 1:1 match for "Defender blocked the blueprint." (Per-instance kill = key by
`oid`; a future policy tweak sends both — Phase 3.)

### 2.3 The enforcement path already ships

| Component | Repo artifact | Role |
|---|---|---|
| Gateway check | `infra/provider.bicep` — `governance-check` fragment on models + energy-MCP APIs | `validate-azure-ad-token`, then blocking `send-request` to proxy `/check`; **fail-closed 403** on deny / non-200 / unreachable |
| Decision | `src/proxy/server.py` — `POST /check` | set-membership of `agent_id` against `revocations`; `allow` / `deny`; fail-closed until first load; 10 s poll |
| Revocation store | Azure App Configuration key **`revocations`** (JSON array or `{"revoked":[...]}`) | the one thing that changes to kill an agent — **keyless**, MI-read |
| Kill action | `az appconfig kv set --key revocations …` | no redeploy, no policy edit |

Because every agent→model and agent→tool call runs the same fragment, a single write to
`revocations` covers **all** interactions of the offending agent. That is the "central gateway
for all agent interactions" pattern made concrete.

### 2.4 The kill signal — shape and path to the store

Producer lanes, all converging on one action (*add the blueprint appId to `revocations`*):

- **Defender for Cloud — AI threat protection** (GA). Alerts on Azure OpenAI / AI inference:
  jailbreak (`AI.Azure_Jailbreak.*`), credential theft, ASCII-smuggling / indirect injection,
  wallet abuse, anomalous tool invocation. **Path:** *Continuous Export → Event Hub* (JSON;
  role `Azure Event Hubs Data Sender`). The alert payload carries the resource and the caller
  identity (`appid`/`oid`) in its entities/properties.
  Docs: `defender-for-cloud/alerts-ai-workloads`, `defender-for-cloud/continuous-export`.
- **Defender XDR / Agent 365 *Secure*.** Agent activity lands in advanced-hunting
  `CloudAppEvents` (`ActionType` ∈ `InvokeAgent`, `InferenceCall`, `ExecuteToolByGateway`
  (Work IQ MCP), `ExecuteToolByMCPServer` (BYO)) plus `AlertInfo` / `BehaviorInfo`. **Path:**
  *Streaming API → Event Hub* (`AlertInfo` GA, `BehaviorInfo` preview), schema
  `{records:[{time,tenantId,category,properties}]}`. Detections carry the Entra Agent ID.
  Docs: `defender-xdr/streaming-api-event-hub`, `defender-xdr/supported-event-types`.
- **Microsoft Purview / Agent 365 *Govern*.** Agents **auto-enroll** into Purview **Audit**;
  every AI interaction emits UAL operations — `AIInvokeAgent`, `AIExecuteTool`, `AIInferenceCall`,
  and **`AIGuardrail`** (a guardrail / DLP trip — the kill-worthy one). DLP-match, DSPM-for-AI,
  and IRM findings feed the same way. **Path:** *Office 365 Management Activity API* or a
  **Sentinel** connector → ingestor → `revocations`. ⚠️ **Async only** — Purview has **no
  synchronous DLP REST** to score a prompt, so inline content checks stay Content Safety Prompt
  Shields / MIP (`proxy.md` §6.1); Purview is a *revoke producer*, not an inline call.
  Docs: `purview/ai-agent-365`, `purview/audit-search`. Deep-dive: `a365-purview-junction.md` §4.
- **Agent 365 / Entra admin action.** SecOps disables the blueprint (`accountEnabled=false`).
  Most A365 controls have **no REST API** (portal/PowerShell only), so we don't call A365 — we
  consume the *side effect*: the Defender alert above, or an Entra audit / Sentinel event.
  Docs: `entra/agent-id/disable-agent-identities`, `entra/agent-id/manage-agent-blueprint`.
- **Sentinel SOAR** (cleanest "SOC kills an agent"). Automation rule → Logic App playbook →
  authenticated `POST {agentId, action:"revoke"}` to the ingestor, using the Logic App's
  **managed identity** (no static key).

### 2.5 Why this closes the gap

- **Token validity is irrelevant.** The proxy denies any token whose `appid` (future: `oid`) is
  in `revocations`; it never inspects `exp`. Effective block latency = App Config poll (≤10 s),
  **not** the 60–90 min token lifetime.
- **Fail-closed everywhere.** Proxy unreachable/timeout → APIM returns 403; store unreadable →
  proxy keeps last-known-good revocations and denies until the first successful load. A kill
  never silently reverts.
- **One choke point.** Central gateway = one revocation covers every model call and every tool
  call the agent can make.
- **Complementary, not redundant.** Entra disable stops *new* sessions; the proxy stops *live*
  ones. Together they fully contain the identity — the exact composition Microsoft's own guidance
  implies but doesn't enforce at a custom gateway.
- **Completes Agent 365 across all three of its engines.** A365 orchestrates Entra + Defender +
  Purview and **inherits each one's inline gap** — Entra's token coast, Defender's inline block
  limited to Work IQ MCP / Copilot Studio / Foundry, and Purview's DLP limited to M365 Copilot
  surfaces (no sync REST). The proxy is the single inline enforcement point covering the *union*
  for BYO / direct / non-M365 callers on the model and MCP routes. Why: `a365-purview-junction.md` §6.

### 2.6 Future: the Agent 365 tools MCP feature

The energy MCP can later be surfaced as a **Bring-Your-Own MCP tool** inside Agent 365 (see
`mcp_tools_a365.md`): register via `a365 develop-mcp register-external-mcp-server`, admin
approves, agents call the tools through the A365 **Tooling Gateway**; Defender observes
`ExecuteToolByGateway` / `ExecuteToolByMCPServer`. Auth target = **EntraOAuth** (the gateway
presents an Entra token the server validates via JWKS — keyless, no access keys).

**Governance continuity:** whether the MCP server is reached through *our* APIM MCP route or the
*A365* Tooling Gateway, the **same `revocations` store governs it** — the APIM route already runs
the proxy check, and A365-observed tool abuse is just another producer into the same set. The
kill switch does not change.

> For the full Agent 365 + Purview + Defender junction (pillar-by-pillar, Purview's audit/DSPM/DLP
> role and its inline boundaries, and why the proxy still exists) see **`a365-purview-junction.md`**.

---

## 3. Implementation plan

**Ponytail framing.** The enforcement path (APIM fragment + proxy + App Config `revocations`)
already ships. The *only* new component is a thin **signal ingestor** that turns a Defender /
Agent 365 signal into **one App Config write**. Build it in phases; each phase is independently
demoable.

### Phase 0 — prove the kill (works today, zero new code)

```bash
az appconfig kv set --name <appconfig> --key revocations --value '["<blueprintAppId>"]' --yes
```

The agent's next model or MCP call returns **403 within ≤10 s**. This is the demo money shot and
needs nothing new — it directly shows the coasting-token gap being closed.

### Phase 1 — SOC-initiated kill (Sentinel / Logic App → proxy)

- Add one authenticated admin endpoint to the proxy: `POST /admin/revoke {agentId}` that
  set-adds to the App Config `revocations` key. Proxy MI needs **App Configuration Data Owner**
  on that key; protect the endpoint with an Entra app role, caller = the Logic App managed
  identity.
- Sentinel automation rule → playbook → POST. One-click "kill this agent" for SecOps, landing in
  the same store. ~1 small handler + one role assignment.

### Phase 2 — automated Defender kill (Event Hub consumer)

**Built.** `enableProxy` now also provisions a **keyless Event Hub** (`evhns-proxy-*` /
`alerts`, `disableLocalAuth: true`, consumer group `ingestor`) and the governance proxy runs a
**background consumer thread** (`src/proxy/server.py`) that reads alerts off it — keyless via the
same managed identity — extracts the offending **appid** and set-adds it to `revocations`. No
separate Function/job: the enforcer is also the ingestor, so it reuses the identity, image and
App Config client (the proxy MI is now **App Configuration Data Owner** to write). Writes are
idempotent and reflected in `state` immediately, not on the next poll.

- The appid extractor is a **schema-agnostic recursive scan** for keys like `appId` /
  `applicationId` / `aadClientId` (A365 / the SDK stamp the identity onto the model call, so
  Defender copies it onto the alert). Override with the `ALERT_APPID_JSONPATH` env var if a real
  alert nests it somewhere ambiguous.

**One manual step — wire Defender export to the hub (keyless / trusted service):**

```bash
# Values from azd outputs:
NS=$(azd env get-value PROXY_EVENTHUB_NAMESPACE)
HUB=$(azd env get-value PROXY_EVENTHUB_NAME)
# 1) Grant Defender for Cloud's identity Send on the namespace (trusted-service export; no SAS).
#    In the portal continuous-export blade this is the "Export as a trusted service" toggle.
#    Role: Azure Event Hubs Data Sender (2b629674-e913-4c01-ae53-ef4638d8f975).
# 2) Defender for Cloud → Environment settings → <subscription> → Continuous export →
#    Event Hub: destination = this namespace/hub, exported data = Security alerts.
```

- Ref: [Continuous export as a trusted service](https://techcommunity.microsoft.com/blog/microsoftdefendercloudblog/continuous-export-as-trusted-service-to-event-hub/3859983)
  (grant **Azure Event Hubs Data Sender** to the Defender for Cloud identity — `disableLocalAuth`
  leaves no SAS path, matching the no-access-keys rule).
- Result: Defender alert → Event Hub → proxy consumer → revocation → gateway block with **no human
  in the loop**, within the write + poll latency (seconds).

**Smoke-test the ingest lane (no Defender needed).** Inject one synthetic alert into the hub and
watch the proxy revoke it — proves hub→consumer→App Config→block end to end:

```bash
NS=$(azd env get-value PROXY_EVENTHUB_NAMESPACE)
HUB=$(azd env get-value PROXY_EVENTHUB_NAME)
CFG=$(azd env get-value PROXY_APP_CONFIG_NAME)
RG="rg-$(azd env get-value AZURE_ENV_NAME)-provider"
NS_ID=$(az eventhubs namespace show -g "$RG" -n "$NS" --query id -o tsv)

# One-time: let yourself send (keyless; this is the same role Defender needs for real).
az role assignment create --assignee "$(az ad signed-in-user show --query id -o tsv)" \
  --role "Azure Event Hubs Data Sender" --scope "$NS_ID"   # wait ~1-2 min for RBAC to propagate

# Send a fake alert carrying a test appid (data-plane REST, AAD token, no SAS).
TOKEN=$(az account get-access-token --resource https://eventhubs.azure.net --query accessToken -o tsv)
curl -s -X POST "https://$NS.servicebus.windows.net/$HUB/messages?timeout=60&api-version=2014-01" \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"properties":{"extendedProperties":{"AppId":"smoke-test-appid"}}}'

# Verify: the appid shows up in revocations and /health within a few seconds.
sleep 5
az appconfig kv show -n "$CFG" --key revocations --auth-mode login --query value -o tsv   # ["smoke-test-appid"]
curl -s "$(azd env get-value PROXY_URI)/health"                                           # revoked_count: 1

# Clean up so the demo starts empty.
az appconfig kv set -n "$CFG" --key revocations --value '[]' --yes --auth-mode login
```

The consumer starts at `@latest`, so send the test *after* the proxy is running (it is, post-deploy).
Proxy log line to watch: `[proxy] revoked smoke-test-appid from Defender alert`.

### Phase 3 — expansion (as needed)

- **Per-instance kill:** have the APIM policy also send `oid`; revoke at blueprint *or* instance
  granularity.
- **A365 tools MCP:** register the energy MCP as BYO (EntraOAuth); the same store governs
  Tooling-Gateway tool calls (Section 2.6).
- **More producers / richer policy:** Purview findings, Entra risk; swap the proxy's `decide()`
  for OPA/ACS (trust rings, rate limits) behind the unchanged `/check` contract.
- **Un-kill / TTL:** remove the id (or attach an expiry) to restore an agent after remediation.

### Deliberately **not** built (ponytail)

No bespoke policy engine, no synchronous fan-out to five security products, no per-product hard
dependency, no new datastore. One App Config key and one Event Hub consumer reuse everything
already deployed — the proxy stays a set-membership check until a real requirement earns more.

---

### References

- Defender for Cloud — AI alerts & export: `defender-for-cloud/alerts-ai-workloads`,
  `defender-for-cloud/continuous-export`.
- Defender XDR streaming: `defender-xdr/streaming-api-event-hub`,
  `defender-xdr/supported-event-types`.
- Entra Agent ID kill switch: `entra/agent-id/disable-agent-identities`,
  `entra/agent-id/manage-agent-blueprint`.
- A365 BYO MCP / Work IQ: `microsoft-365/copilot/extensibility/work-iq/mcp/overview`;
  repo `mcp_tools_a365.md`.
- This repo: `proxy.md` (§4 kill switch, §6 security-system interfacing), `src/proxy/server.py`,
  `infra/provider.bicep`.
