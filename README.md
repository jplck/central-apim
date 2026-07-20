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
- **Container** (`src/hosted-agent/agent.py`): ~15 lines —
  `FoundryChatClient(model="apim-shared/gpt-4.1").as_agent(...)` served by
  `ResponsesHostServer`. Its model calls route project → APIM → provider A, exactly like
  the prompt agent, just from inside a container. `requirements.txt` **must** include
  `mcp` even though this agent has no tools: `agent_framework_foundry_hosting` imports it
  unconditionally, and omitting it crashes the container on startup (the invoke then fails
  with `424 session_not_ready` because `/readiness` never serves).
- **Deploy** (`infra/hooks/create_hosted_agents.py`): builds the image on ACR via ARM REST
  (`listBuildSourceUploadUrl` → upload → `scheduleRun`) as the **azd** identity — no `az`
  CLI, so it works even if `az` and `azd` are logged into different identities — then
  `create_version(HostedAgentDefinition(...))`. It also tries (best-effort) to grant the
  agent's own identity **Foundry User** on its project; that grant is optional — a hosted
  agent already has default model-inferencing access via its project endpoint.

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
