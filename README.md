# Multiple Foundry resources → one APIM → shared models

A minimal azd/Bicep sample proving the question: **can several Azure AI Foundry
resources consume models through one shared API Management gateway, hosted by a
different Foundry resource?** Yes.

Everything is public and **keyless** — managed identity only (`disableLocalAuth: true`,
no APIM subscription keys).

## Architecture

```
                          rg-<env>-provider
  ┌────────────────┐      ┌──────────────────────────────────────────┐
  │ Foundry B      │      │  API Management (StandardV2)              │
  │  proj-b ──MI──►│      │  API: /foundry/deployments/{name}/...     │
  │  connection ───┼─────►│   1. validate-azure-ad-token              │      ┌───────────────┐
  └────────────────┘      │      (allow B & C client IDs only)        │      │ Foundry A     │
  ┌────────────────┐      │   2. authentication-managed-identity ─────┼─MI──►│ gpt-4.1       │
  │ Foundry C      │      │      (APIM's own identity → backend)      │      │ (the model)   │
  │  proj-c ──MI──►│      │                                           │      └───────────────┘
  │  connection ───┼─────►│                                           │
  └────────────────┘      └──────────────────────────────────────────┘
   rg-<env>-consumers
   + Azure Policy: deny Microsoft.CognitiveServices/accounts/deployments
```

- **Foundry A** (provider): owns the real `gpt-4.1` deployment.
- **Foundry B, C** (consumers): each has a project and an **account-level** connection
  (`isSharedToAll: true`) to the shared APIM — so every project in the resource shares it.
  They own **no** models; a policy blocks model creation in their resource group.
- **One APIM** fronts A and is consumed by both B and C.

### Keyless, two hops

1. **Consumer → APIM**: the project's user-assigned managed identity gets an Entra token
   (`audience=https://cognitiveservices.azure.com`). APIM's `validate-azure-ad-token`
   accepts it only if the token's app ID is B's or C's identity.
2. **APIM → Foundry A**: APIM swaps in its **own** system-assigned identity
   (`authentication-managed-identity`) which holds **Cognitive Services OpenAI User** on A.

Consumers never get a role on A — APIM is the trust boundary.

## Deploy

```bash
azd up          # prompts for env name + location, creates both resource groups
```

Pick a region with `gpt-4.1` GlobalStandard **and** APIM StandardV2 (e.g. `swedencentral`, `eastus2`).

After provisioning, a **postprovision hook** (`infra/hooks/create-agents.sh`) runs
automatically and:

1. creates a `gateway-test` **prompt agent** in every consumer project (pointed at
   `apim-shared/gpt-4.1`), then
2. builds the tiny **hosted agent** image on the shared ACR and registers it as a
   `gateway-hosted` hosted agent in the hosting consumer project(s) — `hostedAgentConsumers`,
   default the first consumer (skipped if you set `enableHostedAgents=false`).

It is idempotent where practical and retries while the just-assigned RBAC roles propagate.

## Test (does an agent in B/C reach A's model?)

The postprovision hook already created a `gateway-test` agent in each consumer project;
just start a conversation against it. The model deployment name is
`<connection-name>/<model-name>` → **`apim-shared/gpt-4.1`** (the
`AGENT_MODEL_DEPLOYMENT_NAME` output). To (re)create one by hand:

```python
from azure.ai.projects import AIProjectClient
from azure.ai.projects.models import PromptAgentDefinition
from azure.identity import DefaultAzureCredential

project = AIProjectClient(
    endpoint="<CONSUMER_PROJECT_ENDPOINTS[0]>",  # e.g. proj-b
    credential=DefaultAzureCredential(),
    allow_preview=True,
)
agent = project.agents.create_version(
    agent_name="gateway-test",
    definition=PromptAgentDefinition(
        model="apim-shared/gpt-4.1",       # connection/model
        instructions="You are a helpful assistant.",
    ),
)

# Invoke the agent via the Responses API (the only way Foundry agents are called).
# This hop hits the project endpoint, not APIM; Foundry then calls the model through
# APIM using *chat completions*.
oai = project.get_openai_client()
conversation = oai.conversations.create()
response = oai.responses.create(
    conversation=conversation.id,
    input="Say hello from behind the gateway.",
    extra_body={"agent_reference": {"name": agent.name, "type": "agent_reference"}},
)
print(response.output_text)  # a reply proves B → APIM(chat/completions) → A works
```

Run the same against `proj-c`'s endpoint to prove **two** resources share **one** APIM.

## Hosted (containerized) agents

Same idea as the prompt agent, but the agent is a **container** running inside Foundry
that reaches the central model through APIM. One tiny image (`src/hosted-agent/`) is
deployed to a consumer project and calls the shared model.

> **Platform cap:** a Foundry **public hosting environment** is limited per
> subscription+region — provisioning a second one in the same sub+region deadlocks in
> `Creating` for ~50 min and then fails. Since you required all-public (no networking),
> private hosting isn't an option, so the sample hosts the container on **one** consumer.
> `hostedAgentConsumers` (default `take(consumers, 1)` → the first consumer) picks which.
> **Every** consumer still consumes the central model via APIM (prompt agent + connection)
> regardless — that's the multi-resource-one-gateway proof.

- **Infra** (`enableHostedAgents=true`, default): a shared **ACR** (provider RG), a
  **capability host** (`kind: Agents`, public hosting) on the hosting consumer, a
  `ContainerRegistry` connection, and **AcrPull** for its identity — all keyless.
- **Container** (`src/hosted-agent/agent.py`): ~40 lines — an **energy-supplier customer
  agent**. `FoundryChatClient(model="apim-shared/gpt-4.1").as_agent(...)` served by
  `ResponsesHostServer`. Its model calls route project → APIM → provider A, exactly like
  the prompt agent, just from inside a container. When `MCP_GATEWAY_URL` is set it also
  attaches the native **`MCPStreamableHTTPTool`** pointed at the `energy-mcp` route on the
  **same gateway**, injecting its Entra Agent ID bearer token per request via
  `header_provider` — so both model *and* tools go project → APIM → backend, keyless. The
  gateway allows the agent by its **blueprint appid** (see below). `requirements.txt`
  includes `mcp` (the tool's transport; `agent_framework_foundry_hosting` also imports it
  unconditionally, so omitting it crashes the container on startup).
- **Deploy** (`infra/hooks/create_hosted_agents.py`): builds the image on ACR via ARM REST
  (`listBuildSourceUploadUrl` → upload → `scheduleRun`) as the **azd** identity — no `az`
  CLI, so it works even if `az` and `azd` are logged into different identities — then
  `create_version(HostedAgentDefinition(...))`. It also tries (best-effort) to grant the
  agent's own identity **Foundry User** on its project; that grant is optional — a hosted
  agent already has default model-inferencing access via its project endpoint. When
  `enableMcp` is on it also injects `MCP_GATEWAY_URL` into the agent and sets the APIM
  **`gateway-agent-appid`** named value to the agent's Entra Agent ID **blueprint appid**,
  so the shared `governance-check` fragment's `validate-azure-ad-token` admits the agent's
  MCP calls. All instances of a blueprint share one appid, so one value covers them all;
  both steps are best-effort and never fail the deploy.

Invoke it (hosted agents use their **own agent endpoint**, not `agent_reference`):

```bash
# after azd up:
python src/hosted-agent/invoke.py "<CONSUMER_PROJECT_ENDPOINT>" "Say hi via the gateway."
```

The endpoint is `{project_endpoint}/agents/gateway-hosted/endpoint/protocols/openai/responses?api-version=v1`
(OpenAI Responses protocol). A freshly deployed container is cold — the first call can take
~a minute (`invoke.py` retries `session_not_ready`).

> To host on a different (or additional, if your sub+region quota allows) consumer, set
> `hostedAgentConsumers` in `main.parameters.json`, e.g. `["c"]`. To host on both, use a
> separate subscription or region for the second one.

## Add another consumer

Edit one line in `infra/main.bicep`:

```bicep
param consumers array = [ 'b', 'c', 'd' ]   // add 'd'
```

`azd up` again — a new Foundry resource, identity, project and shared connection are
created, and its identity is automatically allow-listed in the APIM policy.

## Analysis: the new Foundry "AI Gateway" (inbound APIM) feature

You asked whether the new *Add AI Gateway* feature fits the **provider** Foundry. It does —
here's the assessment:

- **What it is**: a Foundry **portal** flow (*Operate → Admin console → AI Gateway → Add AI
  Gateway*, preview). It stands up / attaches an APIM (v2 tier) **in front of that resource's
  own model deployments** to enforce **tokens-per-minute limits, quotas and governance**. It is
  **inbound** governance for the resource that hosts the models — i.e. Foundry A.
- **Where it fits**: exactly on the **provider (A)**. It has no purpose on the consumers (B, C) —
  they hold no deployments (and we deny them).
- **Why it's not in this Bicep**: the feature is portal-driven and preview, with **no documented
  ARM/Bicep resource**, so it can't be declared as IaC today. This template instead implements the
  *documented alternative* — "import the Foundry model endpoints into APIM" — which puts APIM
  inbound to A explicitly and reproducibly.
- **Recommendation**: our shared APIM is **already** the inbound gateway to A, so add the
  governance there rather than enabling the portal feature separately (which would double-front A).
  Drop a token-limit policy onto the provider API (`infra/provider.bicep`, inside `<inbound>`):

  ```xml
  <azure-openai-token-limit tokens-per-minute="50000" counter-key="@(context.Request.IpAddress)"
      estimate-prompt-tokens="true" remaining-tokens-header-name="x-ratelimit-remaining-tokens" />
  ```

  If you prefer the native experience, deploy this template first, then in the Foundry portal add
  an AI Gateway on **A** and reuse the APIM instance created here (Standard v2, same sub/tenant → eligible).

## Databricks Genie agent + Microsoft Agent 365

Microsoft **Agent 365** has an external **Registry sync** (M365 admin center, preview) that
pulls agents from other platforms into its registry. The supported platforms are Amazon
Bedrock, Google Vertex AI, Salesforce Agentforce, and **Databricks Genie** — so the Databricks
integration is specifically a **Genie space** (conversational analytics agent), *not* an
arbitrary Model Serving / custom agent.

This sample adds the Azure side (an Azure Databricks workspace) and a runbook to expose a Genie
agent and register it in Agent 365. It's **off by default** (`enableDatabricks=false`).

**What Bicep provisions** (`infra/databricks.bicep`, when `enableDatabricks=true`):
a Premium Azure Databricks **workspace** in `rg-<env>-databricks`, all-public. That's it — the
workspace resource is **free**; you only pay when a SQL warehouse runs (see cost note). Output:
`DATABRICKS_WORKSPACE_URL`.

**Why the rest is a runbook, not IaC:** Genie spaces are **UI-authored — there is no create
API**; the sync authenticates with a **Databricks service principal**; and the Agent 365
connection is a **licensed admin-center action**. None of those are ARM/Bicep resources, so the
template stops at the workspace (same "provision the shell, configure the platform out-of-band"
split as the hosted agents).

### Runbook

1. **Deploy the workspace.** Add `"enableDatabricks": { "value": true }` to the `parameters`
   block of `infra/main.parameters.json` (a literal JSON bool — no env-var typing pitfalls),
   then `azd up`. Note the `DATABRICKS_WORKSPACE_URL` output.
2. **Create a cheap serverless SQL warehouse** (Genie needs one; auto-stops when idle):
   ```bash
   databricks auth login --host "$DATABRICKS_WORKSPACE_URL"
   databricks warehouses create --json '{
     "name": "genie-wh", "warehouse_type": "PRO", "enable_serverless_compute": true,
     "cluster_size": "2X-Small", "auto_stop_mins": 5, "max_num_clusters": 1 }'
   ```
3. **Create the Genie space (UI).** In the workspace: **Genie → New** → pick the warehouse from
   step 2 → add the built-in **`samples`** catalog (e.g. `samples.nyctaxi.trips`) as its data →
   save. Using `samples` means **no storage account or table to create**. Ask it a question to
   confirm it answers.
4. **Create the service principal Agent 365 authenticates with** (needs a client id + secret;
   easiest is an Entra app added to the workspace, then give it workspace admin):
   ```bash
   databricks service-principals create --json '{"displayName":"agent365-sync","active":true}'
   # add its application id to the workspace "admins" group, then create an OAuth secret for it
   # (account admin): databricks account service-principal-secrets create --service-principal-id <id>
   ```
5. **Register in Agent 365.** M365 admin center → **Agents → All Agents → Registry sync →
   Manage → + Connect a platform** → select **Databricks Genie** → enter the **Workspace URL**
   and the SP **Client ID / Client Secret** → **Validate** → **Save** → **Sync agents**. The
   Genie space now appears in the Agent 365 registry.

> **Keyless note:** the Azure side stays keyless — the workspace is public and holds no keys, and
> there is **no Databricks→APIM model tie-in** (every documented path for that needs a static
> secret in a Databricks secret scope, which your "no access keys" rule forbids, so it's
> deliberately left out). The **one** unavoidable credential is the **Databricks** service
> principal secret in step 4 — the Agent 365 connector requires it, and it's a Databricks
> credential, not an Azure access key. Requires an Agent 365 license (E5/E7 or add-on).

> **Cost:** the workspace is free; a **2X-Small serverless SQL warehouse** is roughly
> **~$3–6/hr while actively querying** and **$0 idle** (the `auto_stop_mins: 5` above). Over
> `samples` there's no storage cost. Expect a couple of dollars for a demo session.

## Demo: energy customer-profile MCP server (Container Apps)

A tiny custom **MCP server** on Azure Container Apps that returns **fake** energy customer +
meter data for **5 demo customers** — for wiring MCP tools into an agent without a real
back end. **On by default** (`enableMcp=true`); set it to `false` to skip.

- **App** (`src/mcp-energy/`): ~70 lines of [FastMCP](https://modelcontextprotocol.io) over
  Streamable HTTP (`/mcp`, port 8000). All data lives in `src/mcp-energy/data.json` — no DB,
  no auth, read-only. Tools:

  | Tool | Args | Returns |
  |------|------|---------|
  | `get_customer_profile` | `customer_id` | name, address, tariff, meter list |
  | `list_meters` | `customer_id` | electricity/gas meters |
  | `get_meter_readings` | `meter_id`, `start?`, `end?` | daily readings (ISO date-range filter) |
  | `get_consumption_summary` | `customer_id`, `period=month\|year` | pre-aggregated totals/cost |

  Demo IDs: `C-1001`…`C-1005`. Run locally: `cd src/mcp-energy && pip install -r requirements.txt && python server.py` (selftest: `python server.py selftest`).

- **Infra** (`infra/mcp.bicep`, provider RG): self-contained and keyless — its own **ACR**,
  user-assigned identity (**AcrPull**), Container Apps environment + Log Analytics, and the
  container app. Provisioned with a placeholder image on port 80 so the first revision is
  healthy.
- **Deploy** (`infra/hooks/deploy_mcp.py`): the postprovision hook builds `src/mcp-energy`
  into the ACR via ARM REST as the **azd** identity (no `az` CLI), then swaps the real image
  and target port 8000 onto the app. The live URL is the `MCP_URI` output
  (`https://<app>.<region>.azurecontainerapps.io/mcp`).
- **Gateway route** (`infra/provider.bicep`): the server is also fronted by the shared APIM
  gateway as a **native APIM MCP server** (`type: 'mcp'`, streamable-HTTP passthrough — not a
  generic HTTP API), so APIM is MCP-protocol-aware: it surfaces the backend's tools as
  first-class API-tool sub-resources and can be registered/discovered in API Center. It applies
  the same `governance-check` policy fragment (Entra token validation + kill switch) as the
  models API. The MCP container itself is unauth, so **APIM is its auth + governance enforcement
  point**; callers present the same Entra token they use for the models route. Client endpoint:
  `https://<apim>/energy-mcp/mcp` (the `MCP_GATEWAY_URL` output); backend transport endpoint
  `/mcp` is set in `mcpProperties`. Requires APIM api-version `2025-09-01-preview` on a tier that
  supports MCP servers (this demo uses Standard v2).
- **Agent 365 (BYO MCP)**: `infra/hooks/register_mcp_a365.sh` runs after deploy and does
  Phase 0 (`a365 develop-mcp evaluate`) + Phase 1 (NoAuth `register-external-mcp-server`).
  Opt-in and non-fatal: `azd env set ENABLE_A365_MCP_REGISTER true` (or `dryrun`), needs
  `a365` ≥1.1.165-preview + `az login`. Full plan and the EntraOAuth hardening path:
  [`mcp_tools_a365.md`](mcp_tools_a365.md).

## Demo: dynamic kill switch (governance proxy) — Phase 1

Revoke any consumer agent's access to the gateway **as data, with no redeploy and no policy
edit** — one `az appconfig kv set`. This is Phase 1 of [`proxy.md`](proxy.md). **Off by
default**; enable with `azd env set ENABLE_PROXY true` before `azd up`.

- **App** (`src/proxy/`): a ~120-line FastAPI decision service. `POST /check {"agent_id": ...}`
  → `{"verdict": "allow"|"deny"}`. It polls App Configuration key `revocations` (a JSON array
  of Entra `appid`s) every 10s and **fails closed** until the first load succeeds. Run the
  logic self-test: `python src/proxy/server.py --self-test`.
- **Infra** (`infra/proxy.bicep`, provider RG): keyless and self-contained — an **App
  Configuration** store (the revocation list), a user-assigned identity (**App Configuration
  Data Reader**), and a Container App. The **deployer** gets **App Configuration Data Owner**
  so you can edit the list from the CLI.
- **Gateway** (`infra/provider.bicep`): the governance check — `validate-azure-ad-token` + the
  kill switch — lives in one reusable APIM **policy fragment** (`governance-check`) that every
  API includes via `<include-fragment>` (the models API and the `energy-mcp` API today). When
  armed, the kill switch reads the caller's `appid` from the validated token and does a
  synchronous `send-request` to the proxy's `/check`. Non-`allow` (or any proxy error / timeout)
  → **403**. The `governance-proxy-host` named value defaults to a non-resolving sentinel, so the
  whole block is **skipped** (zero overhead) unless the proxy is deployed; the `deploy_proxy.py`
  postprovision hook arms it with the live host.

**Kill a consumer** (e.g. consumer B) — takes effect within ~10s, no redeploy:

```bash
# The gateway-accepted consumer appids (index 0 = B, 1 = C):
azd env get-value CONSUMER_CLIENT_IDS

APPCS=$(azd env get-value PROXY_APP_CONFIG_NAME)
az appconfig kv set --name "$APPCS" --key revocations \
  --value '["<consumer-b-appid>"]' --auth-mode login --yes
```

**Un-kill** — set it back to an empty list:

```bash
az appconfig kv set --name "$APPCS" --key revocations \
  --value '[]' --auth-mode login --yes
```

## Notes / assumptions

- **Account-level connection**: created on the account (`accounts/connections`) with
  `isSharedToAll: true` so all projects share it, per your "resource not project" requirement.
  If the platform ever rejects `ProjectManagedIdentity` at account scope, move the `connection`
  resource under the project (`accounts/projects/connections`) — nothing else changes.
- **Backend URL** uses A's `properties.endpoint` (`…cognitiveservices.azure.com`) + `/openai`.
  If a model 404s through the gateway, switch the backend to `https://<a-name>.openai.azure.com/openai`.
- **Two resource groups** exist only so the "deny model deployments" policy can target the
  consumers without also blocking A's deployment.
- **Deployer role**: the azd principal gets **Foundry User** on each consumer account so the
  postprovision hook can create agents. (Azure AI Developer is *not* enough — its dataActions
  cover OpenAI/Speech/ContentSafety/MaaS but not `AIServices/agents/write`.) The hook
  authenticates with `AzureDeveloperCliCredential`, so it runs as that **same** azd principal —
  not your `az` CLI login, which may be a different tenant/identity and would 403.
- APIM **StandardV2** is the minimum tier the Foundry BYOM feature supports (Premium also works).
- **Hosted-agent model routing**: the hosted agent uses the same BYOM deployment name
  (`apim-shared/gpt-4.1`) as the prompt agent, so its model calls go through APIM to A. This
  assumes Foundry resolves the `<connection>/<model>` route identically for hosted-agent model
  calls as for prompt agents (the docs imply yes — both use Foundry model routing). The container
  reads it from `AZURE_AI_MODEL_DEPLOYMENT_NAME`, so if a hosted agent ever needs a different
  route, change only that env var in `infra/hooks/create_hosted_agents.py`.
- **API surface**: the gateway exposes only **chat completions** (`/deployments/{name}/chat/completions`)
  — that's all Foundry BYOM calls, per the docs. You *invoke the agents* via the **Responses API**
  (`get_openai_client().responses.create(... agent_reference ...)`) against the project endpoint;
  APIM isn't in that hop. Raw `/responses` passthrough *through* APIM is intentionally not exposed.
```
