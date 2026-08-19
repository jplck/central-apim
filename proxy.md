# A governance proxy for agent, model, and tool traffic (dynamic kill switch)

Plan and research for a second Container App that sits **inside an APIM custom policy** and
acts as a synchronous **decision proxy** for agent → model, agent → tool (MCP), and
agent → agent interactions. The headline feature is a **dynamic, non-hard-coded kill
switch**: revoking an agent must be a data change (one write to a store), never an edit to
policy code or APIM XML, and it must work for **non-Microsoft third-party agents on any cloud
or stack (AWS Bedrock, GCP Vertex, Salesforce, on-prem, Databricks, …) that have no Entra
identity**.

This document grounds the design in Microsoft's **Agent Governance Toolkit (AGT)** — in
particular the **Agent Control Specification (ACS)** decision layer and the tutorial-14
kill-switch / rate-limiting scenario — and maps how the proxy interfaces with **Defender,
Microsoft Sentinel, Purview, Entra (CAE + Agent ID), and Agent 365**.

> Status: **Design / research only. Nothing here is deployed.** AGT is an open, evolving
> Microsoft reference toolkit (public preview components); Azure AI threat protection,
> Entra Agent ID, and Agent 365 governance are themselves in preview. Treat package names,
> APIs, and verdict shapes as preview-versioned and re-verify before building. Sources are
> listed under [References](#references).

---

## Verdict

**Build the proxy as an ACS host exposed over HTTP, and call it synchronously from APIM
with `send-request` (blocking, `ignore-error="false"`).** The proxy receives a snapshot of
the pending interaction (who, which model/tool, arguments) and returns a normalized
verdict — `allow | warn | deny | escalate | transform`. APIM enforces the verdict at the
edge.

The **kill switch is dynamic because ACS policies are Rego and the revocation list is a
Rego `data` document**, not policy code. The proxy loads that `data` from an external store
(Azure App Configuration / Blob / Cosmos / Redis / an OPA bundle server) and refreshes it
on a short interval or push. **Killing an agent = one write to that store.** No redeploy, no
XML edit. External security systems (Defender, Sentinel, Purview, A365) become *producers*
into that same store.

**Third-party agents without Entra identities** authenticate at APIM with a **keyless** wire
credential — an **mTLS client certificate** or a **3rd-party OIDC token** (validated directly
against the provider, or federated to Entra)
(SPIFFE only if you already run SPIRE; don't put a raw DID on the wire — APIM can't validate
it). The proxy derives a **stable `agent_id`** (mapped internally to an AGT `did:mesh` DID)
from the cert SAN / `oid` — a deterministic hash of that claim, in AGT's `did:mesh:` naming
(see §7.2) — and governs it by **trust score and ring**, starting untrusted
agents in the lowest ring (sandbox) until they earn elevation.

**Lazy stance (ponytail):** do **not** hand-roll a policy engine, a kill-switch service, or
a rate limiter. Reuse ACS (`agent-control-specification`) for decisions and AGT's
`MCPGateway` / `KillSwitch` / `AgentRateLimiter` for enforcement primitives. Use **native
APIM** (`send-request`, `validate-client-certificate`, `validate-jwt`) and **native
Container Apps** (revisions, KEDA, managed identity) instead of custom infrastructure. The
only thing we actually write is a thin HTTP wrapper around an ACS host plus a set of Rego
policies and a `data` refresh loop.

---

## 1. What we are actually trying to build

| Requirement | Design consequence |
|---|---|
| One proxy fronts **model, tool, and agent** calls | The proxy is protocol-shaped as a **decision endpoint**, not a passthrough; APIM stays the data-plane, the proxy is the control-plane. |
| **Dynamic** kill switch (no hard-coding) | Revocation is **data, not code**: a Rego `data` document / external store the proxy reads. |
| Kill takes effect **fast** and **fails closed** | Short `data` refresh + fail-closed default; optionally an inline `send-request` check per call for zero-lag revocation on the hottest paths. |
| **Third-party agents on any cloud/stack (no Entra identity)** | Keyless wire credential — **mTLS cert** or **3rd-party OIDC token** (validated directly or Entra-federated) — *asserted at APIM*, mapped to a stable `agent_id` (a hash of its public claim; see §7.2) that is *evaluated in the proxy*. |
| Interface with **Defender / Sentinel / Purview / A365 / Entra** | Those systems are **signal producers** that write revocations/among trust changes into the store; the proxy is a signal *consumer*. Optionally the proxy also *emits* audit events back to them. |
| Keyless (no access keys in this environment) | Identity via **mTLS / OAuth / signed JWT**, secrets via **managed identity**; no APIM subscription keys, no shared secrets in the hot path. |

---

## 2. Why AGT / ACS is the right substrate

The Agent Governance Toolkit already factors this exact problem into a **stateless decision
layer** plus **enforcement primitives**. Reusing it is the lazy, correct move.

### 2.1 ACS — the decision layer

| Property | Detail |
|---|---|
| Shape | **Stateless, fail-closed.** Host sends a *complete snapshot* at an **intervention point**; ACS returns a verdict. Retains no session state. |
| Verdicts | `allow`, `warn`, `deny`, `escalate`, `transform` (transform rewrites arguments, e.g. redact a field). |
| Intervention points | `pre_tool_call`, `post_tool_call`, `pre_model_call`, `post_model_call`, agent handoff, etc. — bound to policies in an **ACS manifest** via a JSONPath `policy_target`. |
| Policy engines | **Rego/OPA, Cedar, or custom.** Rego receives canonical input `{intervention_point, policy_target:{path,kind,value}, tool:{name,id,clearance}, agent:{…}, …}` and returns `{decision, reason, message, transform}`. |
| Fail-closed | Malformed manifest, missing target path, or policy error → **deny**. |
| Distribution | Native Rust core; SDKs for **Python / TypeScript / .NET / Rust / Go**. `pip install agent-control-specification`. |

Because ACS is stateless and fail-closed, it maps **1:1 onto an APIM `send-request` call**:
send the snapshot, block for the verdict, enforce it. That is the whole proxy.

### 2.2 Enforcement primitives worth reusing (don't rebuild these)

| Primitive | Package / class | What it gives us |
|---|---|---|
| **MCP security gateway** | `MCPGateway(runtime, denied_tools, sensitive_tools, rate_limit, approval_callback)` | `intercept_tool_call(agent_id, tool, params) → (allowed, reason)`; response scanning (`BLOCK/SANITIZE/LOG` for PII/credential leaks); fails closed. **This is essentially the reference proxy.** |
| **Kill switch** | `hypervisor.security.kill_switch.KillSwitch` | `.kill(agent_did, session_id, reason, in_flight_steps, details) → KillResult`; audit trail (`kill_history`, `total_kills`); reasons `BEHAVIORAL_DRIFT / RATE_LIMIT / RING_BREACH / MANUAL / SESSION_TIMEOUT`; **compensates** (rolls back) in-flight saga steps in preview. |
| **Rate limiting** | `AgentRateLimiter` (per-agent, per-ring token buckets; rings 0–3 default 100/50/20/5 req/s), Agent Mesh two-tier `RateLimiter`, edge `RateLimitMiddleware` | Throttle by agent and trust ring. |
| **Ring / privilege** | `RingElevationManager` (TTL elevation), `RingBreachDetector` (anomaly → auto-kill) | Least-privilege tiers + automatic tripping. |
| **Identity / trust** | `did:mesh:<sha256(name+org)>` DIDs, **Ed25519** keys, **SPIFFE/SVID**, human sponsor, trust score 0–1000 (identity/behavior/network/compliance), rings by tier, `TRUST_REVOCATION_THRESHOLD=300` | Cloud-neutral identity + a numeric basis for "how much do we trust this agent right now." |
| **Audit / evidence** | ACS decision logs, kill history, Decision-BOM style records | Tamper-evident trail for every allow/deny. |

**Ponytail note:** the KillSwitch object is *in-memory*. Its value here is the audit +
compensation semantics, **not** state. The *dynamic/distributed* part of our kill switch is
the external revocation store feeding Rego `data` (§4). Don't try to make the in-memory
object a distributed database.

---

## 3. Architecture

```
                         (control plane: decisions)
  ┌────────────┐  mTLS / OAuth JWT    ┌──────────────────┐  send-request (block) ┌───────────────────────────┐
  │ 3rd-party  │ ───────────────────► │       APIM        │ ────────────────────► │   Governance proxy (ACA)  │
  │ agent      │                      │  (data plane +    │ ◄──────────────────── │   = ACS host / MCPGateway  │
  │ (Bedrock,  │ ◄─────────────────── │   policy enforce) │      verdict          │  Rego policies + `data`    │
  │  Vertex,   │      response         └────────┬─────────┘                        └─────────────┬─────────────┘
  │  Foundry…) │                                │ allow → forward                                │ reads
  └────────────┘                                ▼                                                ▼
                                     ┌────────────────────┐                        ┌───────────────────────────┐
                                     │ model / MCP tool / │                        │  Revocation + trust store  │
                                     │ downstream agent   │                        │  (App Config / Blob /      │
                                     └────────────────────┘                        │   Cosmos / Redis / OPA     │
                                                                                    │   bundle server)           │
                                                                                    └─────────────▲─────────────┘
                                                              async signals write revocations/trust │
                    ┌──────────────────────────────────────────────────────────────────────────────┤
                    │Defender for Cloud (AI)  •  Defender XDR / Graph Security  •  Sentinel SOAR      │
                    │Purview (DSPM for AI / DLP)  •  Entra CAE + Agent ID  •  Agent 365 kill          │
                    └───────────────────────────────────────────────────────────────────────────────┘
```

**Flow (per interaction):**

1. The agent calls APIM (model route, MCP route, or agent route). It authenticates with a
   **cloud-neutral credential** (§7) — no Entra token required.
2. APIM validates the credential (`validate-client-certificate` or `validate-jwt`), extracts
   a **stable `agent_id`**, and calls the proxy with `send-request mode="new"`
   (**synchronous, blocking**, `timeout`, `ignore-error="false"`), passing a JSON snapshot:
   `{ intervention_point, agent_id, target (model|tool|agent), operation, arguments, context }`.
3. The proxy (ACS host) evaluates the snapshot against its Rego policies + current `data`
   (revocation list, trust scores, rings, rate state) and returns a verdict.
4. APIM enforces: `allow` → forward to the model/tool/agent; `deny` → `return-response 403`;
   `transform` → apply the returned rewrite (e.g. redact args) then forward; `escalate` →
   hold for approval; `warn` → forward but tag/log.
5. **Signals loop (async):** Defender/Sentinel/Purview/A365/Entra push revocations and trust
   changes into the store; the proxy refreshes `data` and the next call sees the new state.

**Why `send-request` and not a JWT-only APIM policy:** APIM can validate a token, but it
can't evaluate *dynamic* org policy ("this agent was killed 20 seconds ago by a Defender
alert", "this agent is over its per-ring budget", "redact SSNs from this tool call"). That
needs a stateful-store-backed decision, which is exactly what the proxy provides and what
`send-request` was built to consult. (`send-request` blocks until the response or `timeout`;
`ignore-error="false"` routes failures to `<on-error>` so we can fail closed.)

---

## 4. The dynamic, non-hard-coded kill switch (the core ask)

### 4.1 Principle: revocation is data, not code

ACS policies are **static Rego**; the *state* they read is **`data`**. So we keep policy
code stable and put every revocable fact in a `data` document:

```rego
# policy: kill_switch.rego  (STATIC — never edited to kill an agent)
package agent.killswitch

import future.keywords.if

default decision := "allow"

# deny if this agent (or its blueprint, ring, or DID) is revoked in data.revocations
decision := "deny" if {
    some key in [input.agent.id, input.agent.blueprint, input.agent.did]
    data.revocations[key].active == true
}

reason := data.revocations[input.agent.id].reason
```

```json
// data.json  (DYNAMIC — this is the only thing that changes to kill an agent)
{
  "revocations": {
    "did:mesh:9f2a…":          { "active": true, "reason": "RING_BREACH",       "by": "sentinel-playbook", "ts": "…" },
    "databricks-sp-42":        { "active": true, "reason": "MANUAL",            "by": "secops@contoso",    "ts": "…" },
    "blueprint:ext_energymcp": { "active": true, "reason": "BEHAVIORAL_DRIFT",  "by": "defender-xdr",      "ts": "…" }
  }
}
```

**Killing an agent** = `PUT` one entry into the store. Un-killing = flip `active` or delete.
The proxy never redeploys; APIM XML never changes; the Rego never changes.

### 4.2 Where `data` lives and how it refreshes

| Option | Refresh | Fit |
|---|---|---|
| **Azure App Configuration** | Poll (ETag) or push via Event Grid | Simple, cheap, native MI auth, good for a small-to-medium kill list. **Recommended MVP.** |
| **Azure Blob** (OPA **bundle** format) | OPA/`data` bundle polling (built-in) | Idiomatic OPA; the proxy pulls a signed bundle every N seconds. |
| **Cosmos DB / Redis** | Change feed / pub-sub | Large lists, low-latency, multi-region. |
| **OPA bundle server** | Native bundle protocol | If you already run OPA centrally. |

Refresh interval is the **max revocation lag** you accept (e.g. 5–15 s). For paths that need
**zero lag**, the policy can additionally do a **point lookup** — either the proxy queries
the store directly for `input.agent.id`, or APIM does a second, tiny `send-request` to a
`/killed/{agent_id}` endpoint (cache-busting). A **Redis** `kill-switch:{agent_id}` key with
TTL makes that point lookup `O(1)`, <1 ms. Reserve it for the highest-risk routes; the
`data` refresh covers everything else without per-call cost.

### 4.3 Mapping to AGT's KillSwitch

When the proxy denies on a revocation, it also invokes AGT `KillSwitch.kill(agent_did,
session_id, reason, in_flight_steps)` to (a) write the **audit record** (`kill_history`) and
(b) **compensate** any in-flight saga steps for that session. So the store gives us *dynamic
distribution*, and the AGT object gives us *audit + rollback*. Reasons flow straight through:
a Defender alert writes `reason: "BEHAVIORAL_DRIFT"`, a rate breach writes `"RATE_LIMIT"`, an
operator writes `"MANUAL"`.

### 4.4 Fail-closed

- `send-request` fails / times out → `<on-error>` returns `403` (deny), never open.
- Store unreachable at refresh → keep the **last known** revocation `data` (never drop kills)
  and optionally deny new sessions until refreshed.
- Malformed policy/manifest/target → ACS already denies by construction.

---

## 5. Other AGT capabilities the proxy should expose

All of these are *the same `send-request` → verdict* shape; they just add more Rego and more
`data`. Add them incrementally (see §8), don't build them all up front.

| Capability | AGT basis | Verdict behavior |
|---|---|---|
| **Rate limiting** | `AgentRateLimiter` token buckets per agent & ring | `deny`/`warn` when over budget; `data` holds per-agent limits, so limits are tunable without redeploy. |
| **Ring / least privilege** | rings 0–3, `RingElevationManager` (TTL), `RingBreachDetector` | Deny operations above the agent's ring; auto-kill on breach. |
| **Trust scoring** | 0–1000 score (identity/behavior/network/compliance) | Gate by threshold; `< 300` → revoke. Trust is `data`, updated by signals. |
| **Tool allow/deny + sensitive-tool approval** | `MCPGateway(denied_tools, sensitive_tools, approval_callback)` | `deny` blocked tools; `escalate` sensitive tools to human approval. |
| **Response scanning / DLP** | `MCPResponseScanner` (`BLOCK/SANITIZE/LOG`) | `post_tool_call` / `post_model_call` `transform` to redact PII/credentials before returning. |
| **Cost governance** | budget counters in `data` | `deny`/`warn` over token/$ budget per agent. |
| **Audit / Decision-BOM** | ACS decision logs + kill history | Emit every verdict to Log Analytics / Event Hub for SOC + compliance. |

---

## 6. Interfacing with Microsoft security & governance systems

**Two lanes: one inline call, everything else async.** Calling five security products
synchronously on every request would blow the latency budget and risk failing open. So the
proxy makes **exactly one inline security call** — **Azure AI Content Safety Prompt
Shields** — and treats every other product as an **async signal producer** that trips the
kill switch (§4) for *future* requests.

### 6.1 Inline: Azure AI Content Safety Prompt Shields (the one synchronous call)

Prompt Shields is a **synchronously callable REST API** for jailbreak / direct + indirect
prompt-injection detection — exactly what a per-request verdict needs:

- `POST https://<resource>.cognitiveservices.azure.com/contentsafety/text:shieldPrompt?api-version=2024-09-01`
- Body `{ "userPrompt": "...", "documents": ["..."] }` (covers direct **and** indirect/document injections)
- Response `{ "attackDetected": true|false, ... }`
- Auth: **managed identity** (Cognitive Services scope) — keyless
- Latency ~100–400 ms

The proxy calls it at `pre_model_call`; `attackDetected == true` → `deny` (403). No waiting
for a Defender alert. *(GA, api-version 2024-09-01.)*

### 6.2 Async: security products as kill-switch producers

Each product **pushes** a revocation/trust change into the store (§4.2); the proxy consumes
it via `data`. Latencies below are "signal → enforced" end-to-end.

| System | What it detects / does | Signal path → store | Latency | Key facts / permissions |
|---|---|---|---|---|
| **Defender for Cloud — Threat protection for AI** (GA, May 2025) | Platform-layer alerts on Azure OpenAI / AI Model Inference: jailbreak (`AI.Azure_Jailbreak.*`), credential theft (`AI.Azure_CredentialTheftAttempt`), ASCII-smuggling / indirect injection (`AI.Azure_ASCIISmuggling`, High), wallet abuse (`AI.Azure_DOW*`), anomalous tool invocation (`AI.Azure_AnomalousToolInvocation`), Tor / suspicious-agent access. | **Continuous Export → Event Hub** → consumer flips the store; or → Defender XDR → Sentinel. | ~30 s–2 min | RBAC `Azure Event Hubs Data Sender`. Alerts are **async by design** — trip the switch, don't gate the current call. |
| **Defender XDR / Microsoft Graph Security API** | Unified alerts / incidents. `GET /security/alerts_v2` (legacy `/security/alerts` **deprecated 2026-08-31**). | **Webhook** `POST /subscriptions` (resource `security/alerts_v2`, `changeType "created,updated"`) → proxy endpoint; **or** Defender XDR **Streaming API → Event Hub**. | Seconds (webhook / stream); 1–5 min (polling) | App perms `SecurityAlert.Read.All` / `SecurityIncident.Read.All` (admin consent). Streaming API is GA + well-documented. |
| **Microsoft Sentinel (SOAR)** | Correlation + orchestrated response. | **Automation rule → Logic App playbook** with an HTTP action that `POST`s `{agentId, action:"revoke"}` to the proxy's `/admin/kill-switch`. **Cleanest "SOC kills an agent" path.** | ~1–4 min | Secure the callback with the **Logic App's managed identity** against an Entra-app-role-protected endpoint (not a static key). |
| **Microsoft Purview (DSPM for AI / DLP)** | Classifies AI interactions; DLP / insider-risk / oversharing findings. | **Async only.** Finding → Sentinel / Function → store. For inline label checks use the **MIP SDK** (~50–200 ms). | Async; MIP SDK inline | ⚠️ **Gap:** there is **no synchronous DLP REST API** to score an arbitrary prompt. Purview is an observe / audit layer, not an inline enforcement service — don't design an inline Purview call. |
| **Entra — CAE + Agent ID** | Revoke / disable the identity. | Disable SP `PATCH /servicePrincipals/{id} {accountEnabled:false}` (~30 s, blocks *new* tokens); `invalidateAllRefreshTokens`. **Agent ID:** disabling a **blueprint** blocks all its agent instances. Mirror the revoked `oid` into the store. | ~30 s for new tokens; existing tokens valid to expiry | ⚠️ **CAE reality:** workload-identity CAE only covers **Microsoft Graph** as resource provider — **not** custom APIs / your Container App — and **managed identities are unsupported**. So APIM `validate-jwt` alone will **not** reject a just-revoked token; the **proxy's own `oid` check against the store is the real enforcement point**. |
| **Agent 365** (GA 2026-05-01) | The M365 **control plane** for agents (Observe / Govern / Secure). It doesn't replace Defender/Purview — it **routes agent telemetry into them** (§6.4). Admin can disable an agent or blueprint. | **Out-of-band + telemetry.** Consume its Defender/Purview signals (see §6.4); disable propagates via the underlying **Entra Agent ID SP**. Most A365 controls (rule creation, MCP block, registry GET) are **portal/PowerShell only — no REST API**. | Disable: ~s (new tokens) to ≤60 min (cached); signals: see §6.4 | For M365 / Copilot agents, revoke via the **Entra Agent ID SP**; the store stays the superset covering third-party agents A365 only *sees* (not protects). |

### 6.3 Design consequence

The store is the **single revocation substrate** and the proxy is the **single enforcement
point**. Every product is a *producer*; the proxy never takes a hard, synchronous dependency
on any one product's uptime — except the one deliberate inline Prompt Shields call. That is
what makes the kill switch simultaneously **dynamic** (data), **federated** (many sources),
and **resilient** (fail-closed, no fan-out).

### 6.4 Agent 365 as a bundled Defender + Purview control plane (consume + complement)

**Agent 365** (announced Ignite Nov 2025, **GA 2026-05-01**) is Microsoft's M365 control plane
for agents — pillars **Observe / Govern / Secure**. The key architectural fact for us:
**Agent 365 does not replace Defender or Purview — it is an OpenTelemetry pipeline that
*fans agent telemetry out into* them.** SDK-instrumented or M365-native agents emit spans to
`agent365.svc.cloud.microsoft`, which routes them to **Defender XDR** (hunting + detections),
**Purview** (audit + DSPM/DLP), and the M365 admin center (registry). So "Agent 365 uses
Defender and Purview" means: Defender is its *Secure* surface, Purview its *Govern/data*
surface, both fed by the same telemetry.

**What that means for our proxy: `consume` its signals, `complement` its gaps.** Don't
duplicate Agent 365; treat it as another **producer** into the store (§4) and cover what it
structurally can't (inline enforcement, non-SDK / cross-cloud agents).

**Defender side (what to consume).** Agent-activity lands in Defender XDR **Advanced Hunting**
tables — `CloudAppEvents`, `AgentsInfo`, `BehaviorInfo` (audit/**block** records),
`BehaviorEntities`, `AlertInfo`, `AlertEvidence`. `CloudAppEvents.ActionType` is the pivot:
`InvokeAgent`, `InferenceCall`, `ExecuteToolBySDK`, `ExecuteToolByGateway` (Work IQ MCP),
`ExecuteToolByMCPServer` (custom/BYO). Detections cover jailbreak, indirect prompt injection
(XPIA), credential leakage, evasion, LLM recon, suspicious IP. **Lowest-latency path into the
store: Defender XDR Streaming API → Event Hub** (seconds) for `BehaviorInfo`/`AlertInfo`;
`alerts_v2` / `runHuntingQuery` are the poll alternatives.

**Defender's inline block is real but narrow** — only **Work IQ MCP** tool calls (GA),
**Copilot Studio** (preview), **Foundry** (preview), and endpoint agents (MDE). Rules are a
default **audit-all** plus **custom block rules scoped by Entra Agent ID** — but **created in
the portal only, no REST API**. So Defender cannot inline-block an arbitrary BYO / third-party
agent calling a non-Work-IQ tool. **That's exactly our proxy's job.**

**Purview side (what to consume / emit).** Agent instances are **auto-enrolled** for
**Audit** (Unified Audit Log), data classification, and Compliance Manager; **DLP / IRM /
Communication Compliance** require adding the agent to a policy like a user. UAL operations
to consume: **`AIInvokeAgent`, `AIExecuteTool`, `AIInferenceCall`, `AIGuardrail`** (via the
**Office 365 Management Activity API** or Graph `security/auditLog/queries`). DSPM-for-AI
risk shows in the portal only (**no REST**). **Confirmed gap (still true for A365):** there is
**no synchronous Purview DLP REST API** to score an arbitrary prompt, and DLP inline-block is
limited to Teams / OneDrive-SharePoint / email interactions — **not** the model/inference
layer. So for inline content checks the proxy uses **Prompt Shields / MIP SDK** (§6.1), never
an inline Purview call.

**Portal-only, no-REST controls** (can't automate from the proxy): Defender protection-rule
creation, Work IQ MCP server block, Agent Registry `GET`-all, DSPM risk scores, Comm-Compliance
results. Consume their *side effects* via UAL / Streaming API instead.

**Make the proxy visible in Agent 365 (emit).** Register the proxy as an Agent 365
SDK-instrumented agent and **emit its own OTel spans back** to `agent365.svc.cloud.microsoft`
— then the proxy's allow/deny decisions show up in Defender hunting and Purview audit
alongside everything else. One-directional, async, no hot-path cost.

| Question | Agent 365 built-in | Our proxy |
|---|---|---|
| M365 / Copilot / Foundry agents | ✅ telemetry + (preview) inline block | complement — inline enforcement point |
| **Non-SDK / direct model callers (any 3rd-party)** | ❌ not covered | ✅ **only** control plane |
| Non-Work-IQ / custom-MCP tool calls | partial (telemetry; Defender *may* detect) | ✅ inline enforce |
| Cross-cloud (Bedrock, Vertex, **Databricks Genie**) | 👁 **visibility only** via Connected Platforms — **no** real-time Defender protection | ✅ inline enforce |
| Sub-100 ms inline allow/deny | ❌ not an inline gateway | ✅ purpose-built |
| Synchronous DLP of arbitrary prompts | ❌ no API | ✅ Prompt Shields / MIP SDK |

**Kill-path reconciliation.** Disabling an A365 agent/blueprint = Entra SP
`accountEnabled:false` → blocks **new** tokens in seconds, but existing tokens **coast up to
~60 min** and CAE doesn't cover our custom API (§6.2). The **proxy store kills in-flight
sessions in ~10 ms**. Complementary: Entra stops new sessions, the proxy stops live ones.

---

## 7. Non-Microsoft third-party agents (any cloud/stack) without Entra identities

A third-party agent — hosted on **AWS (Bedrock), GCP (Vertex AI), Salesforce, Anthropic,
Oracle, on-prem, Databricks, or any non-Microsoft stack** — calling APIM **may carry no Entra
token**. The job: give it a **stable, verifiable `agent_id`** at the APIM edge with a
**keyless** mechanism (the environment forbids access keys), then govern it in the proxy.
(Databricks is used below only as a running example; nothing here is Databricks-specific.)

> **Agent 365 doesn't rescue this case.** Its **Connected Platforms** feature can federate
> **Bedrock / Vertex AI / Salesforce Agentforce / Databricks Genie / Anthropic / Oracle** into
> the agent **registry for *visibility only*** — real-time Defender protection does **not** extend
> to those external agents (§6.4). So for any such external agent, **our proxy is the only inline
> enforcement point**, which is exactly what §7.1–7.3 build.

### 7.1 Credential options at APIM, ranked

| # | Mechanism | APIM validation | Stable `agent_id` from | Verdict |
|---|---|---|---|---|
| **7a** | **3rd-party OIDC token** — validate the provider's own token directly, *or* federate it to an Entra app (workload-identity federation); works for AWS (Cognito / STS OIDC), GCP (workload-identity), Okta, Databricks, etc.; agent sends `Authorization: Bearer …`) | `validate-jwt` against the **provider's issuer/JWKS**, or against Entra if federated | `sub` / `oid` / `appid` claim | ✅ **Best.** Federating to Entra yields a real, durable Entra identity → CA policies, Agent ID, and Graph revocation all become available. Keyless. |
| **7b** | **mTLS client certificate** (per-agent X.509 from your private CA or Key Vault) | `validate-client-certificate` (`validate-trust` / `-revocation`) | cert **Subject CN / SAN** | ✅ **Strongly recommended for the no-Entra case.** Cryptographically bound, fully independent of Entra. Needs APIM **Standard v2 / Premium** for native gateway mTLS (Dev / Consumption need App Gateway in front). |
| **7c** | **HMAC-signed request** (per-agent secret in the provider's secret store — e.g. Databricks Secret Scope, AWS Secrets Manager, GCP Secret Manager — mirrored to Key Vault; `HMAC-SHA256(body+ts+nonce)`) | custom policy expression | your `X-Agent-Id` header | ⚠️ **Bespoke fallback.** No standard protocol; needs nonce + timestamp anti-replay. Use only when TLS client certs aren't possible. |
| ~~7d~~ | ~~APIM subscription key~~ | ~~native~~ | ~~`context.Subscription.Id`~~ | ❌ **Excluded — it's an access key** (violates the environment rule) and is bearer-only (possession = identity). |
| **7e** | **SPIFFE / SVID** (X.509 with `spiffe://` SAN, if you already run SPIRE) | same as 7b (`match-by` SAN) | the **SPIFFE URI** | 🟡 Clean **only if SPIRE already exists**. On most 3rd-party runtimes it's **DIY** (self-hosted SPIRE agent; no managed SPIRE on Azure or the major clouds). A raw W3C **DID on the wire has no APIM-native validation** → ❌ don't put a DID on the wire. |

**Recommendation:** **7a where the provider can issue or federate an OIDC token, 7b (mTLS) otherwise.** Both
are keyless and give a stable identifier APIM extracts and forwards as `X-Agent-Id`.

### 7.2 What the `agent_id` actually is (plain version)

The `agent_id` we govern on is **just a deterministic hash of the stable claim the credential
carries**. Nothing more:

```
agent_id = "did:mesh:" + sha256(trust_domain + ":" + stable_claim)
```

The `did:mesh:` prefix is only a **naming convention** (AGT's) so every agent's id has the
same shape — you could rename it `agent_id` and lose nothing. We deliberately use **none** of
the wider W3C DID machinery (DID Documents, resolution, on-the-wire DID credentials); APIM
can't validate those anyway (§7.1, 7e).

**We hash the identifier, not the secret.** There are three distinct things — don't conflate
them:

| Thing | Example | Role | Secret? |
|---|---|---|---|
| **Credential** | cert + private key, or a signed JWT | *proves* the agent owns the identity | yes (key/signature) |
| **Stable claim** | cert Subject/SAN, OAuth `oid`, SPIFFE URI | *names* the identity — **public** | no |
| **`agent_id`** (`did:mesh:…`) | `did:mesh:9f2a…` | our internal key for kill list / trust / audit | derived (hash of the claim) |

The private key/signature is only used **by APIM to verify ownership**; it is never stored or
hashed. We hash the **public** claim.

**Worked example (mTLS):**

1. A third-party agent (say, running on AWS) connects with mTLS; cert SAN = `partner-agent-42`.
2. APIM verifies the cert chain (the private key proves ownership — used here, never exposed).
3. APIM reads the **public** SAN string and forwards it as `X-Agent-Id`.
4. Proxy computes `sha256("contoso:partner-agent-42")` → `agent_id = did:mesh:9f2a…`.
5. That value is the key checked against the revocation store and trust/ring state.

**This is not Entra Agent ID.** Entra Agent ID is Microsoft's identity *product* (service
principals under a blueprint) that a non-Microsoft agent can't easily obtain. Our `agent_id` is a string
**the proxy computes itself**. Entra appears only if you choose OAuth federation (7a), and
then purely as the *source* of the `oid` claim we hash — we are not minting an Entra Agent ID.

Because the hash is **deterministic**, the same cert/`oid` always yields the same `agent_id` —
no runtime registry write or lookup — and an AWS cert-agent, a Foundry `oid`-agent, and
a Copilot agent all collapse into **one** id namespace, so a single kill list, trust score,
ring, and audit trail covers every agent regardless of origin.

### 7.3 Govern unknown agents by trust ring, not allow-list

A brand-new third-party agent starts in the **lowest ring (Ring 3 / sandbox)**: lowest trust
score, tightest rate limit, smallest tool set. It **earns elevation** (`RingElevationManager`,
TTL-bound) as behavior / compliance signals accrue, and is **auto-demoted or killed** on
breach (`RingBreachDetector` → `KillSwitch` → store). So we never pre-enumerate every
third-party agent — the proxy defaults them to minimal privilege and lets trust + the kill
switch do the rest.

### 7.4 The easy case: agents that already have an Entra Agent ID (Foundry, Copilot)

A Foundry hosted agent — or any agent with a real **Entra Agent ID** — is the *easy* path: it
already presents a first-class Entra token, so most of §7.1–7.3 collapses. Same proxy, same
store, same trust/ring model; you just get a verified identity for free **plus a second,
native revocation lever**.

| Aspect | 3rd-party, any cloud/stack (§7.1–7.3) | Real Entra Agent ID (Foundry, Copilot) |
|---|---|---|
| Wire credential | mTLS cert / federated OAuth (you provision) | **native Entra token** from the agent's `fmi_path` two-step exchange — nothing to provision |
| APIM validation | `validate-client-certificate` / `validate-jwt` | **`validate-jwt`** against the tenant (native) |
| Stable claim | cert Subject / SAN | **`oid` / `sub`** = agent *instance*; **`appid`** = *blueprint* (agent type) |
| `agent_id` | hash(SAN) → `did:mesh:…` | just use **`oid`** (already globally unique + stable); hashing into `did:mesh:` is optional, only for one uniform namespace |
| Native revocation | none — proxy store is the only kill switch | **also** disable the SP or **blueprint** via Graph (`accountEnabled:false`); blueprint disable kills *all* its instances |
| Extra governance | none | Entra Conditional Access (Workload Identities Premium), per-instance permission grants, Agent 365, Agent ID lifecycle |
| Starting trust ring | Ring 3 / sandbox | higher — identity is verified + governed |

**Flow:**

1. The agent runtime performs the Agent ID `fmi_path` exchange (Blueprint credential → parent
   token → per-instance token, `aud = api://<your-api>`).
2. It calls APIM with `Authorization: Bearer <token>`.
3. APIM `validate-jwt` (tenant openid-config; check `aud`, `iss`, required claims) and extracts
   `oid`/`sub` (instance) + `appid` (blueprint). Forward `X-Agent-Id: <oid>` and
   `X-Agent-Blueprint: <appid>`.
4. The proxy runs the **same** ACS decision, keying revocation/trust at **instance (`oid`)**
   *and* **blueprint (`appid`)** granularity — so you can kill one agent or a whole class.

**Two-layer kill switch — use both:**

- **Proxy store (real-time):** write the `oid` or `appid` → the very next call is denied,
  *regardless of token lifetime*. This is the dynamic kill switch (§4).
- **Entra (issuance):** disable the SP / blueprint via Graph — stops *new* tokens (~30 s) and
  is the durable, portable revocation. But existing access tokens stay valid until expiry and
  **CAE doesn't cover your custom API** (§6), so the proxy store is what makes revocation
  *immediate*. Entra stops issuance; the proxy stops in-flight valid tokens.

**Net:** the Entra agent hands you a verified identity with no cert plumbing, and you gain a
*second* native revocation lever on top of the dynamic one. The third-party case (any cloud/stack)
is just this same model with the identity **manufactured** at the edge instead of issued by
Entra.

---

## 8. Implementation plan (phased, lazy)

**Phase 1 — MVP kill switch (days, not weeks).**
- Container App running a tiny HTTP service that hosts **ACS** with one Rego policy
  (`kill_switch.rego`) reading `data.revocations` from **Azure App Configuration** (MI auth,
  ETag poll every ~10 s).
- APIM policy: `validate-client-certificate`/`validate-jwt` → derive `agent_id` →
  `send-request` (blocking, `ignore-error="false"`) → enforce `allow`/`deny` with
  `<on-error>` failing closed.
- Kill/unkill = `az appconfig kv set` (or a 10-line `/revoke` admin endpoint). **Done: dynamic
  kill switch, no code/XML edits to kill.**
- Optional one-call win: add an inline **Prompt Shields** check at `pre_model_call` (keyless,
  ~100–400 ms) for immediate jailbreak/injection blocking.

**Phase 2 — signals in.**
- One **Sentinel playbook** (or Function) that writes revocations into App Config on incident.
- Wire **Defender for Cloud AI** + **Defender XDR** alerts → that playbook (Continuous Export
  → Event Hub, or the XDR Streaming API).
- Add **rate limiting** and **cost** counters as extra Rego + `data`.

**Phase 3 — identity & trust.**
- Give each third-party agent a keyless wire credential: **mTLS cert (7b)** or a **3rd-party
  OIDC token (7a)** (validated directly or Entra-federated); APIM extracts `agent_id`; the proxy maps it to an internal
  `did:mesh` DID.
- Add **trust scoring + rings** (`data`-driven), `RingBreachDetector` → auto-kill.
- Add **response scanning** (`MCPResponseScanner`) as a `post_*` `transform`; use the **MIP
  SDK** for inline label checks (Purview has no inline DLP API).

**Phase 4 — audit & scale.**
- Emit every verdict + kill to **Log Analytics / Event Hub** (Decision-BOM).
- Move `data` to **Cosmos/Redis** only if the kill list outgrows App Config.

Each phase is independently shippable; stop whenever it's "good enough."

---

## 9. Deliberately skipped (YAGNI)

- **No custom policy engine** — ACS/Rego does it.
- **No bespoke kill-switch microservice/DB** — App Config + Rego `data` is enough for MVP;
  Cosmos/Redis only on real scale need.
- **No synchronous fan-out to every security product** — async signals into one store;
  avoids latency and fail-open.
- **No networking/private endpoints** for the demo — all public, matching the repo's stance.
- **No per-product SDK in the hot path** — the proxy reads one store; products are producers.
- **No SPIRE/SPIFFE stand-up or DID-on-the-wire** — mTLS with your own CA (7b)
  or a 3rd-party OIDC token (7a, validated or Entra-federated) achieve the same stable identity with supported
  tooling; SPIFFE only pays off if SPIRE already exists.
- **No inline Purview/DLP call** — there's no synchronous API; classify with the MIP SDK or
  consume Purview findings async.
- **No custom CAE resource-provider implementation** — workload CAE only covers Graph; the
  proxy's own `oid` / `agent_id` store check is the enforcement point.

---

## 10. Open questions / risks

- **Latency budget:** `send-request` on every model/tool call adds a round trip. Measure;
  co-locate the proxy in the same region/environment as APIM; consider caching `allow` for
  idempotent low-risk routes with a short TTL (never cache `deny`).
- **Revocation lag vs cost:** shorter `data` refresh = faster kills, more store reads. Tune
  per route; use the inline point-lookup only where zero lag is required.
- **AGT preview drift:** package names, verdict fields, and KillSwitch semantics
  (compensate-only in preview) may change — pin versions.
- **Third-party identity trust bootstrapping:** who issues the mTLS cert (or which provider
  OIDC issuers we trust) for each third-party agent, and how is that CA / issuer trusted at
  APIM? Needs a CA / trust-domain decision.
- **A365 vs store authority:** decide whether A365 disable is the source of truth mirrored
  into the store, or the store is authoritative and A365 is one producer.

---

## References

**Agent Governance Toolkit** — https://microsoft.github.io/agent-governance-toolkit/
- Tutorial 14 — **Kill switch & rate limiting**: https://microsoft.github.io/agent-governance-toolkit/tutorials/14-kill-switch-and-rate-limiting/
- Tutorial 55 / **Agent Control Specification** (decision contract, manifest, Rego I/O; `pip install agent-control-specification`).
- Tutorial 02 — **Trust & Identity** (DIDs, Ed25519, SPIFFE/SVID, trust scoring, rings).
- Tutorial 07 — **MCP Security Gateway** (`MCPGateway`, response scanning, fail-closed).

**Azure API Management policies (Microsoft Learn)**
- `send-request`: https://learn.microsoft.com/en-us/azure/api-management/send-request-policy
- `validate-client-certificate`: https://learn.microsoft.com/en-us/azure/api-management/validate-client-certificate-policy
- Client-certificate auth: https://learn.microsoft.com/en-us/azure/api-management/api-management-howto-mutual-certificates-for-clients

**Defender / XDR / Graph Security**
- Threat protection for AI (Defender for Cloud): https://learn.microsoft.com/en-us/azure/defender-for-cloud/ai-threat-protection
- AI-workload alert reference: https://learn.microsoft.com/en-us/azure/defender-for-cloud/alerts-ai-workloads
- Prompt Shields (Content Safety): https://learn.microsoft.com/en-us/azure/ai-services/content-safety/concepts/jailbreak-detection
- Graph Security `alerts_v2` migration: https://learn.microsoft.com/en-us/graph/alertsv1-alertsv2-migration
- Defender XDR Streaming API → Event Hub: https://learn.microsoft.com/en-us/defender-xdr/streaming-api-event-hub

**Sentinel / Purview / Entra / Agent 365 / Databricks**
- Sentinel playbooks: https://learn.microsoft.com/en-us/azure/sentinel/automation/create-playbooks
- Purview DSPM for AI: https://learn.microsoft.com/en-us/purview/dspm-for-ai-considerations
- Entra CAE for workload identities: https://learn.microsoft.com/en-us/entra/identity/conditional-access/concept-continuous-access-evaluation-workload
- Entra Agent ID best practices (blueprint disable): https://learn.microsoft.com/en-us/entra/agent-id/best-practices-agent-id
- Agent 365 admin guide: https://learn.microsoft.com/en-us/microsoft-365/copilot/agent-essentials/m365-agents-admin-guide
- 3rd-party OIDC / workload-identity federation to Entra (example — Databricks OAuth M2M): https://learn.microsoft.com/en-us/azure/databricks/dev-tools/auth/oauth-m2m

**Agent 365 ↔ Defender / Purview (§6.4)**
- Agent 365 overview: https://learn.microsoft.com/en-us/microsoft-agent-365/overview
- Agent 365 GA blog (2026-05-01): https://www.microsoft.com/security/blog/2026/05/01/microsoft-agent-365-now-generally-available-expands-capabilities-and-integrations/
- Connected Platforms (registry sync — Databricks Genie et al.): https://learn.microsoft.com/en-us/microsoft-agent-365/admin/connected-platforms
- Observability concepts (endpoints, `ActionType` values): https://learn.microsoft.com/en-us/microsoft-agent-365/developer/observability-concepts
- Defender — enable security for AI agents: https://learn.microsoft.com/en-us/defender-xdr/security-for-ai/get-started-defender-security-for-ai
- Defender — real-time agent protection (block rules): https://learn.microsoft.com/en-us/defender-xdr/security-for-ai/ai-agent-real-time-protection
- Defender — detect & investigate agent threats (hunting tables): https://learn.microsoft.com/en-us/defender-xdr/security-for-ai/ai-agent-detection-protection
- Purview for Agent 365 (DLP scope, capabilities): https://learn.microsoft.com/en-us/purview/ai-agent-365
- Purview audit-log activities — Agent 365 operations: https://learn.microsoft.com/en-us/purview/audit-log-activities#agent-365-activities

*Preview / gap flags:* **Agent 365 GA 2026-05-01**, but its Defender agent-detection +
inline-blocking for **Copilot Studio / Foundry** is **preview**, and Work IQ MCP is preview.
**Portal/PowerShell-only (no REST):** Defender protection-rule creation, Work IQ MCP block,
Agent Registry `GET`-all, DSPM-for-AI risk scores. **No** synchronous Purview DLP API for
arbitrary prompts (use Prompt Shields / MIP SDK); Purview DLP inline-block is limited to
Teams / OneDrive-SharePoint / email, not the model layer. **CAE** for workload identities
covers **Graph only** and **not** managed identities, so the proxy store is the real-time
enforcement point. Connected Platforms federates external agents (Databricks Genie, Bedrock,
Vertex…) for **visibility only** — no real-time Defender protection. W3C DID has no
APIM-native validation path.
