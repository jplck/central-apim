# Agent 365 + Purview + Defender — how the governance proxy fits the junction

Deep-dive companion to `demo-use-case.md`. Answers two questions:
1. **Is Agent 365 "just a UI on top of Purview and Defender"?** (validates a common mental model)
2. **How does our central governance proxy / kill switch work in junction with Agent 365, and
   where does Purview specifically fit?**

---

## 1. Is Agent 365 a UI over Defender + Purview? — verdict: half true

**Agent 365 is a *control plane*, not a UI, and it spans four Microsoft services — Entra, Work
IQ, Defender, and Purview — behind one registry, one admin console, and one telemetry
pipeline.** Microsoft's own tagline: *"Extend Microsoft 365 and Microsoft Security controls to
manage agentic AI at scale."* So it **orchestrates and surfaces** those products; it does not
replace them and is not merely a skin over two of them.

The five official pillars and what actually powers each:

| Pillar | Function | Underlying product | Is it "Defender/Purview"? |
|---|---|---|---|
| **Registry** | inventory of every agent (first-party, Copilot Studio, BYO, 3P) | Agent 365's own registry | ❌ new |
| **Access** | agent identity + risk-based Conditional Access + least privilege | **Microsoft Entra (Agent ID)** | ❌ Entra |
| **Observe** | telemetry, analytics, agent maps | **Work IQ** + A365 OTel pipeline | ❌ new / Work IQ |
| **Protect** | threat detection + data protection | **Defender + Purview** | ✅ yes |
| **Govern** | lifecycle guardrails, onboarding, compliance | Agent 365 + **Entra** + **Purview** | partly |

**Where the "UI over Defender + Purview" intuition is right:** for **Protect** (and the data
side of **Govern**), A365 builds no detection engine and no DLP engine of its own. Agents
**auto-enroll** into Purview (audit, DSPM, classification) and their activity lands in Defender
XDR advanced hunting. For those surfaces A365 genuinely is a consumption/orchestration layer.

**Where it's wrong / incomplete:**
- **Identity is Entra, and it's the load-bearing part for a kill switch.** The blueprint /
  instance identities, the `accountEnabled=false` disable, and Conditional Access all live in
  **Entra Agent ID** — not Purview/Defender. Without it there is no `appid` to key a revocation
  on and nothing to "block."
- **Work IQ + the MCP Tooling Gateway** (the "agent tools" / BYO-MCP feature) is a new
  data/tooling plane, not a Defender/Purview view.
- **"UI" undersells the pipe.** A365 is the **OpenTelemetry pipeline** (`agent365.svc.cloud.microsoft`)
  that *instruments* agents and *fans spans out* to Defender (Secure), Purview (Govern), and the
  admin-center registry. The pipe is A365's; the sinks are Defender/Purview.

**Consequence for us:** because A365 *extends* Entra + Defender + Purview, it **inherits their
enforcement boundaries** — Entra's token-coast, Defender's narrow inline block, and Purview's
M365-surface-only DLP (Section 4). That inherited inline gap is exactly what the proxy fills.

---

## 2. The telemetry pipeline (how signals are produced)

```
  agent (SDK-instrumented or M365-native)
        │  OTel spans
        ▼
  agent365.svc.cloud.microsoft   ── fans out to ──►  Defender XDR   (Secure: hunting + detections)
        │                                            Purview        (Govern: audit + DSPM/DLP)
        │                                            M365 admin ctr (Registry)
        ▼
  our signal ingestor  ◄── Event Hub / O365 Mgmt Activity API / Sentinel ── (Defender + Purview)
        │  set-add blueprint appId
        ▼
  App Config `revocations`  ──poll ≤10s──►  governance proxy  ──►  APIM denies at the gateway
```

A365 produces the telemetry; Defender and Purview are the analytic sinks; **our proxy is the
inline enforcement point** that neither of them provides for arbitrary BYO / direct callers.

---

## 3. How the kill switch works in junction with Agent 365

Trace the demo agent through the A365 pillars, ending at our proxy:

1. **Registry + Access (Entra).** The energy agent is registered under an Entra Agent ID
   **blueprint**; every instance authenticates with the blueprint's credentials, so its token's
   `appid` = the blueprint client id. *This is the value our APIM policy already extracts and
   sends to the proxy `/check`.*
2. **Observe (Work IQ / OTel).** The agent emits telemetry to A365 (given/wired). Tool calls, if
   routed through the A365 Tooling Gateway, show up as `ExecuteToolByGateway` /
   `ExecuteToolByMCPServer`.
3. **Protect (Defender + Purview).** Defender detects abuse (jailbreak, credential theft,
   anomalous tool use); Purview audits every AI operation and classifies the data touched.
4. **Govern / containment.** SecOps disables the blueprint in Entra (`accountEnabled=false`) —
   stops **new** tokens, but existing tokens coast (the gap in `demo-use-case.md` §2.1).
5. **Runtime enforcement (our proxy).** The **same** Defender/Purview signal is forwarded into
   App Config `revocations`; APIM consults the proxy on every model + MCP call and denies the
   surviving instances in ≤10 s — regardless of token validity.

A365 gives us the **identity** (step 1) and the **signals** (steps 2–3); the proxy provides the
**inline choke point** (step 5) A365 structurally lacks for non-M365 / direct / BYO calls.

---

## 4. Purview's role — deep dive

Purview is the **Govern/data** surface of A365. For our design it is an **async signal
producer and audit substrate**, never an inline call on the hot path (there is no synchronous
Purview API to score an arbitrary prompt).

### 4.1 Auto-enrollment (what you get for free vs. what you must opt into)

- **Automatic on agent creation:** **Audit** (Unified Audit Log), **data classification**, and
  **Compliance Manager** assessment. (Learn: `purview/ai-agent-365`.)
- **Add the agent to a policy like a user** for: **DLP**, **Insider Risk Management (IRM)**,
  **Communication Compliance**. Not automatic.

### 4.2 Audit operations — the consumable signal

Every AI interaction emits UAL operations you can consume via the **Office 365 Management
Activity API** or Graph `security/auditLog/queries`:

| Operation | Fires when | Kill-switch relevance |
|---|---|---|
| `AIInvokeAgent` | an agent is invoked | attribution / correlation |
| `AIExecuteTool` | a tool/MCP call runs | tool-abuse trail |
| `AIInferenceCall` | a model call runs | model-abuse trail |
| **`AIGuardrail`** | a policy/guardrail (e.g. DLP, safety) trips | **most kill-worthy** — a guardrail block is a revoke trigger |

Retention 180 d (standard) / 1 yr (E5). These are the events a **Sentinel connector** or a
polling job turns into a revocation.

### 4.3 DSPM for AI — posture, portal-only

Data Security Posture Management for AI gives risk scoring and an AI security dashboard across
Copilot / A365 / custom + third-party agents. **Visibility only, no REST API** — consume its
*side effects* (the UAL operations above, IRM alerts) rather than calling it.

### 4.4 DLP / sensitivity labels — real inline block, but a narrow surface

Purview DLP **does** block inline, but only on **Microsoft 365 Copilot surfaces**:
- **Prompt DLP:** a prompt containing sensitive info types can be blocked (or blocked from web
  grounding). **Label-based:** Copilot won't process / summarize files carrying a blocked
  sensitivity label — across SharePoint, OneDrive, Exchange, Teams, and (2026) local/network
  files; Copilot Studio agents if the DLP scope is extended.
- **Boundaries that matter for us:** DLP enforcement is at the **Copilot/M365 interaction
  layer**, **not** the raw **model/inference** or a **BYO/third-party agent calling your APIM /
  MCP directly**. Many tenants also start in **simulation** mode (observe, not enforce).
- **Confirmed gap (still true under A365):** there is **no synchronous DLP REST API** to score
  an arbitrary prompt. For inline content checks the proxy uses **Content Safety Prompt Shields
  / MIP SDK** (`proxy.md` §6.1), never an inline Purview call.
  (Learn: `purview/dlp-microsoft365-copilot-location-learn-about`, `purview/dlp-sensitivity-label-as-condition`.)

### 4.5 IRM + Communication Compliance

GA for agents (add the agent to a policy). Produce **async findings** — insider-risk /
oversharing / communication violations. Route to Sentinel → revocation like any other producer.

---

## 5. Purview → revocation wiring (concrete)

Purview never inline-blocks the model/MCP layer, so its job in our loop is to **produce a
revoke signal**:

```
Purview AIGuardrail / DLP match / IRM alert
   → Office 365 Management Activity API  (or Microsoft Sentinel connector)
   → automation (Sentinel playbook / Event Hub consumer, managed identity)
   → set-add the agent's blueprint appId to App Config `revocations`
   → proxy denies at the gateway on the next call (≤10 s)
```

Same ingestor, same store, same `/check` contract as the Defender path in `demo-use-case.md`
§3 — Purview is just another producer. (Implementation is Phase 2/3 there; nothing new in the
enforcement path.)

---

## 6. Inherited gaps → why the proxy still exists

Because A365 = orchestration over Entra + Defender + Purview, it inherits each one's boundary.
The proxy covers the union of what none of them enforce inline at a custom gateway:

| A365 pillar / product | What it enforces | Inherited gap | Proxy's job |
|---|---|---|---|
| **Access (Entra)** disable blueprint | blocks **new** tokens | existing tokens coast ≈60–90 min; CAE ≠ custom API | deny live sessions in ≤10 s |
| **Protect (Defender)** inline block | Work IQ MCP (GA), Copilot Studio/Foundry (preview) | **not** arbitrary BYO / non-Work-IQ / direct model callers | inline PEP for those |
| **Govern (Purview)** DLP | M365 Copilot prompt + labeled files | **not** the model/inference layer or a direct API/MCP call; no sync DLP API | Prompt Shields/MIP inline; consume DLP as async revoke |
| **Observe (Work IQ / OTel)** | telemetry only | not an enforcement point | turn telemetry into revocations |

**Net:** A365 makes the agent world **observable and identity-anchored**; Defender and Purview
make it **detectable and auditable**; the proxy makes it **inline-enforceable for everything
they only see** — and closes the token-coast gap none of them close at our gateway.

---

### References

- Agent 365 overview / pillars: `microsoft-agent-365/overview`; microsoft.com/microsoft-agent-365.
- Purview for A365 agents (auto-enroll): `purview/ai-agent-365`.
- Purview audit / AI operations: `purview/audit-search`, Office 365 Management Activity API.
- Purview DLP for Copilot: `purview/dlp-microsoft365-copilot-location-learn-about`,
  `purview/dlp-sensitivity-label-as-condition`.
- Defender export paths: `defender-for-cloud/alerts-ai-workloads`,
  `defender-xdr/streaming-api-event-hub`.
- This repo: `demo-use-case.md`, `proxy.md` (§6 security-system interfacing), `mcp_tools_a365.md`.
