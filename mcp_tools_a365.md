# Integrating the energy MCP server into Microsoft Agent 365 (BYO MCP)

Step-by-step plan for registering our `src/mcp-energy` server as a **Bring-Your-Own (BYO)
MCP server** in Microsoft Agent 365, so agents (Copilot Studio, VS Code, Claude Code,
GitHub Copilot CLI) can call its tools through the governed **Tooling Gateway**.

**Phase 0 (evaluate) and Phase 1 (NoAuth registration) are automated** by the post-deploy
hook `infra/hooks/register_mcp_a365.sh` (see [Automation](#automation)). Phases 2–3 are
planned, not implemented.

> Status: BYO MCP is **preview**. Details below are from the Microsoft Learn docs
> (`manage-tools-for-agent`, `develop-mcp` CLI reference, Agent 365 identity/tooling docs).

---

## Verdict

Our path is **BYO MCP → `a365 develop-mcp register-external-mcp-server`** (not
`develop-mcp publish`, which is the Dataverse route). Our server already satisfies the
hardest prerequisite: a **public HTTPS Streamable-HTTP `/mcp` endpoint** (the Container
Apps `MCP_URI` output). Recommended production auth: **EntraOAuth** — the only
keyless-compliant option for the "no access keys" rule.

## 1. How BYO works

A **Tooling Gateway** sits between the agent surface and our server; every call is brokered
through it, which is what gives admins governance and Defender its telemetry.

| Actor | Action |
|---|---|
| **Developer** | Registers the remote server via the Agent 365 CLI (URL, auth type, tool names) → creates a **Request** |
| **IT admin** | M365 admin center → **Agents ▸ Tools ▸ Requests**: reviews, **Approves**, grants Entra consent |
| **Agent builder** | Adds the approved server in **Copilot Studio / VS Code / Claude Code / GitHub Copilot CLI** |
| **Security** | Watches invocations in **Defender advanced hunting** (`CloudAppEvents | ActionType == "ExecuteToolByGateway"`) |

## 2. Auth decision

| Type | Fit | Notes |
|---|---|---|
| **NoAuth** | ⚠️ demo-only | Zero server changes; public endpoint callable by anyone. Fine for *fake* data. **Phase 1 uses this.** |
| **APIKey** (Header/Query) | ❌ | A static key = an "access key" → **violates the no-keys rule**. |
| **ExternalOAuth** | ❌ | Needs a non-Entra IdP + `--idp-client-secret`. We're all-Entra. |
| **EntraOAuth** | ✅ **target** | Registration takes **no secret** (`--remote-scopes` only). Gateway gets an Entra token for our API; server validates it via **public JWKS** — keyless. **Phase 2.** |

## 3. Gap analysis — our server today vs. EntraOAuth

- **Today:** `FastMCP` on ACA, `/mcp`, **NoAuth**, 4 tools, fake `data.json`. ✅ public, ✅ HTTPS, ✅ streamable-http.
- **Missing for EntraOAuth:** validate the incoming bearer JWT on every request — signature
  (tenant JWKS), `iss` (tenant v2.0), `aud` (our API app), expected **app role/scope**, and
  ideally `azp/appid` ∈ Agent 365 gateway app IDs. One real code addition.

## 4. Prerequisites (one-time, tenant-level)

- **Agent 365 CLI ≥ `1.1.165-preview`** (`a365 --version`).
- **Agent 365 Tools SP** — the first-party resource `ea9ffc3e-8a23-4a7d-836d-234d7c7565c1`
  ("Agent Tools") that BYO apps consent against **must exist in the tenant**, or registration
  fails at consent with *"Couldn't complete consent for one or more apps."* Provision once as
  Global Admin: `az ad sp create --id ea9ffc3e-8a23-4a7d-836d-234d7c7565c1` (equivalent to
  Microsoft's `New-Agent365ToolsServicePrincipalProdPublic.ps1`).
- **Roles:** developer needs `az login` (+ app-registration rights for EntraOAuth); **approver
  needs AI Administrator or Global Administrator** *and* tenant-consent ability.
- **Licensing / preview enablement** for Agent 365 in the tenant (confirm BYO preview is on).

## 5. Step-by-step plan

### Phase 0 — Evaluate (automated)
`a365 develop-mcp evaluate --server-url <MCP_URI>` scores tool **names/descriptions/param
schemas** (what the LLM sees) and reachability. Advisory gate; never blocks.

### Phase 1 — NoAuth registration (automated)
```
a365 develop-mcp register-external-mcp-server \
  --server-name "ext_energymcp" \            # must start ext_, ≤20 chars
  --server-url "<MCP_URI>" \
  --auth-type NoAuth \
  --publisher "Contoso Energy (demo)" \
  --description "Energy customer + meter profile demo" \
  --tools "get_customer_profile,list_meters,get_meter_readings,get_consumption_summary"
```
⚠️ **Tool names must exactly match** what `tools/list` returns, or calls fail at runtime.
Then a tenant admin **approves** in **Agents ▸ Tools ▸ Requests** (~30 min to propagate to
Copilot Studio). Test from Copilot Studio or this GitHub Copilot CLI.

> The CLI also accepts **`--input-file <manifest.json>`** instead of flags — camelCase keys:
> `serverName`, `serverUrl` (the **downstream origin** the gateway proxies to, e.g. your APIM
> URL — *not* the client URL), `authType`, `tools` (array of `{name,…}` objects), `remoteScopes`.

### Phase 2 — Harden to EntraOAuth (planned)
1. **Entra app for the API (resource):** app registration; **Application ID URI** `api://<appId>`;
   expose a **delegated scope** (e.g. `mcp.invoke`). The gateway calls downstream **per-user
   (OBO)**, so `remoteScopes` is a *delegated* scope (claims land in `scp`), matching the
   interactive OAuth connect. *App-only* (App Role + `.default`, `roles` claim) is the alt only
   if you need agent-identity calls with no user in the loop.
2. **JWT validation in `server.py`** (env-gated `MCP_REQUIRE_AUTH=true` so local/demo stays
   NoAuth): a small Starlette middleware validating JWKS/iss/aud/role, or FastMCP's built-in
   `TokenVerifier` + `AuthSettings`. New dep: `pyjwt[crypto]`. Data stays fake. Optionally
   serve `/.well-known/oauth-protected-resource` (MCP spec).
3. **Infra (`infra/mcp.bicep`):** add **app settings only** — `MCP_REQUIRE_AUTH`,
   `AZURE_TENANT_ID`, `MCP_AUDIENCE` (=`api://<appId>`), `MCP_ALLOWED_CALLER_APPIDS`. All
   public identifiers, **no secrets** → still keyless. The Entra app itself isn't ARM;
   provision it via `az ad app` / Microsoft Graph in a hook.
4. **Register EntraOAuth:**
   ```
   a365 develop-mcp register-external-mcp-server \
     --server-name "ext_energymcp" --server-url "<MCP_URI>" \
     --auth-type EntraOAuth --remote-scopes "api://<appId>/mcp.invoke" \  # delegated scope; .default only for app-only
     --publisher "Contoso Energy (demo)" \
     --description "Energy customer + meter profile demo" \
     --tools "get_customer_profile,list_meters,get_meter_readings,get_consumption_summary"
   ```
   The CLI **creates the platform client app registration(s)**, wires redirect URIs + the API
   permission on your role.
5. **Admin approves + consents** → grants the gateway's client app your delegated scope
   tenant-wide; each user still does the interactive **connect** on first call.
6. **Verify:** gateway call succeeds; a wrong-audience token gets **401**. Log received JWT
   claims once to confirm app-only (`roles`) vs delegated (`scp`).

### Phase 3 — Govern & observe (planned)
- **Kill switch:** admin **Block** on the server = runtime-enforced across all surfaces.
- **Monitoring:** Defender KQL `CloudAppEvents | where ActionType == "ExecuteToolByGateway"`.

## 6. Using it from Azure AI Foundry (Entra agent identity)

Foundry is a **separate surface** from A365 — never point it at the A365 gateway URL
(`https://agent365.svc.cloud.microsoft/agents/servers/<name>`); that fails with
`TenantIdInvalid`. Point Foundry at the **direct** Container Apps `/mcp` URL.

**Adding the tool:** Foundry portal ▸ **Agents ▸ *your agent* ▸ Tools ▸ + Add ▸ Custom MCP**
(or `McpTool(server_url=…)` via the SDK) ▸ paste the direct `/mcp` URL ▸ pick an auth mode
below. There is **no separate "grant the Foundry agent access to the A365 agent" step** — a
Foundry agent reaches the server **directly**; the A365 registration only governs A365-side
consumers (Copilot/declarative agents), which live on the other identity plane. Two options:

- **NoAuth (simplest):** add the MCP tool with **auth = none**. Works because the server is
  public. This is mutually exclusive with the Entra option below.
- **Entra agent identity (this repo):** the server validates the Entra token Foundry sends.
  1. `sh infra/hooks/setup_mcp_entra_app.sh` → creates the resource app registration and prints
     `MCP_ENTRA_TENANT_ID` + `MCP_ENTRA_AUDIENCE` (`api://<clientId>`), storing both in the azd
     env. Without this audience, Foundry fails to fetch the token
     (`Failed to fetch agentic identity access token ... 400`).
  2. `azd provision` (or `azd deploy mcp`) to roll the two env vars onto the app —
     `server.py` then enforces bearer validation (signature via tenant JWKS, issuer, `aud`).
  3. In Foundry, set the MCP tool's auth to **Entra agent identity** with **audience =
     `api://<clientId>`**. Grant that identity the `Mcp.Invoke` app role only if you set
     `MCP_ENTRA_REQUIRE_ROLE`.
  4. **Publish gotcha:** pre-publish, all agents in a project **share one** agent identity;
     after you **publish**, the agent gets its **own** identity. Only matters if you role-gate —
     re-grant `Mcp.Invoke` to the published agent's identity. The default (no role) needs nothing.
  - Trade-off: enabling Entra auth returns **401** to A365's NoAuth calls — one endpoint can't
    be both. Keep them on separate deployments if you need both surfaces.

## 6.5 Consuming the **gateway** URL (the governed path)

The A365 gateway URL (`https://agent365.svc.cloud.microsoft/agents/servers/<name>`) is used by
**adding the registered server as a tool in a supported client** — it's surfaced **from the
registry automatically** (you do NOT paste the URL). Supported surfaces (preview):
**Copilot Studio, VS Code, Claude Code, GitHub Copilot CLI**. **Azure AI Foundry and M365
Declarative Agents are NOT supported** — that's why Foundry against the gateway URL returns
`TenantIdInvalid`/`403` no matter the audience/role.

Copilot Studio steps ([manage-tools-for-agent](https://learn.microsoft.com/en-us/microsoft-365/admin/manage/manage-tools-for-agent#use-an-approved-mcp-server)):
1. Open [Copilot Studio](https://copilotstudio.microsoft.com/) → create/open a **custom agent**.
2. **Tools** → **MCP Server** → **select the server from the registry** (`ext_energymcp3`).
3. Prompt to invoke; first call may prompt a **one-time connection** setup.

Allow **up to 30 min** post-approval for it to appear across Copilot Studio environments.

**Coding agents** (VS Code / Claude Code / GitHub Copilot CLI) paste a **tenant-scoped** gateway
URL into `mcp.json` (`https://agent365.svc.cloud.microsoft/agents/tenants/<tenantId>/servers/<name>`)
with an OAuth **public-client** app id (the `<name>-PublicClients` app the registration created);
the user consents interactively on first call. Example (VS Code `.vscode/mcp.json`):
```json
{ "servers": { "energyMCP": { "type": "http",
  "url": "https://agent365.svc.cloud.microsoft/agents/tenants/<tenantId>/servers/ext_energymcp3",
  "oauth": { "clientId": "<PublicClients-appId>" } } } }
```
Auth model: **admin approval** (tenant consent, done at registration) **+ per-user** OAuth connect.
Foundry uses the **direct** `/mcp` URL (§6) instead — same server, two disjoint planes. (Work IQ
*catalog* servers are a separate pre-integrated Foundry path; BYO `ext_` servers are not.)

## 7. Caveats & risks (from the docs)

- **Preview.** No **delete** of a BYO server; no **republish** of new versions.
- **A365 surfaces:** Copilot Studio / VS Code / Claude Code / GitHub Copilot CLI. The A365
  gateway URL is **NOT** consumable from Azure AI Foundry or M365 Declarative Agents (yet).
- **`ext_` prefix, ≤20 chars**; a failed registration **doesn't roll back** created Entra apps
  (delete manually).
- **The one asterisk on "keyless":** the CLI-created *platform client* app may carry an Entra
  client secret (`--secret-lifetime-months`, default 24). That's an Entra-managed app
  credential auto-provisioned by the platform, not a static API key you embed. Cap it with
  `--secret-lifetime-months 1` if policy requires. Our own server holds no secret.

## 8. Open questions / assumptions

1. **App-only vs OBO at the gateway** → decides App Role (`roles`) vs delegated (`scp`)
   validation. Confirm by inspecting a real token in Phase 2.6.
2. **Agent 365 BYO preview + licensing enabled** in the demo tenant.
3. `--service-tree-id` is only required in Microsoft *corp* tenants (likely N/A here).

---

## Automation

`infra/hooks/register_mcp_a365.sh` runs in the **postprovision** hook (after
`deploy_mcp.py` has swapped the real image onto the container app) and performs **Phase 0 +
Phase 1**. It is **opt-in** and **non-fatal by default** so `azd up` stays green for anyone
who hasn't set it up.

| Env var | Default | Meaning |
|---|---|---|
| `ENABLE_A365_MCP_REGISTER` | *(unset = skip)* | `true`/`1` = register; `dryrun` = pass `--dry-run`; unset/`false` = skip |
| `A365_MCP_SERVER_NAME` | `ext_energymcp3` | server name (must start `ext_`, ≤20 chars) |
| `A365_EVAL_ENGINE` | `none` | `none` (deterministic only, nothing leaves the machine) / `auto` / `github-copilot` / `claude-code` |
| `A365_STRICT` | *(unset)* | `1` = a failure aborts the provision instead of warning |

Enable and run:
```
azd env set ENABLE_A365_MCP_REGISTER true
azd provision          # or azd up
```
Preview without side effects:
```
azd env set ENABLE_A365_MCP_REGISTER dryrun
azd provision
```
Requires `a365` (≥1.1.165-preview) and `az login` on the machine running the hook. The
script validates the server-name rule, evaluates the live server, then registers it NoAuth
using `src/mcp-energy/a365-register.json` via `-f` (so tool descriptions are supplied
non-interactively); a tenant admin still approves in the M365 admin center.

Self-check (no CLIs needed): `sh infra/hooks/register_mcp_a365.sh --check`.

## Troubleshooting (learned in practice)

- **Registration is not cleanly retryable.** On any failure A365 prints *"All created
  resources have been cleaned up"* but leaves orphaned **Entra apps** (`-A365Proxy`,
  `-PublicClients`, `-RemoteProxy`, `- BYO`) **and** **Power Platform connectors**. These are
  usually left **active** (not soft-deleted) and **accumulate/duplicate** across retries; the
  `- BYO` app holds the deterministic App-ID URI
  `https://agent365.svc.cloud.microsoft/agents/servers/<serverName>/tenants/<tenantId>`, so the
  next attempt fails with `500 ... identifierUris already exists`. Purge before retrying:
  - Entra (verified recipe) — list, soft-delete, then hard-purge (soft-delete alone still
    **reserves the URI for 30 days**), then confirm both queries return empty:
    ```sh
    az ad app list --filter "startswith(displayName,'ext_<name>')" --query "[].id" -o tsv \
      | xargs -rn1 az ad app delete --id                      # soft-delete each
    az rest --method GET --url "https://graph.microsoft.com/v1.0/directory/deletedItems/microsoft.graph.application?\$filter=startswith(displayName,'ext_<name>')&\$select=id" \
      --query "value[].id" -o tsv \
      | xargs -rn1 -I{} az rest --method DELETE --url "https://graph.microsoft.com/v1.0/directory/deletedItems/{}"   # hard-purge each
    ```
    (Leave the **working** registration's apps alone — only purge the failed name's orphans.)
  - Connectors: delete via `api.powerapps.com/.../apis?$filter=environment eq '<envId>'`
    (audience `https://service.powerapps.com/`), or connector creation `400`s on the clash.
    If that API is unavailable (e.g. `404`), just **bump to a fresh, never-used `ext_` name**,
    which sidesteps the connector tombstone entirely.
- **A failed serverName is "burned."** The connector's internal name encodes the serverName;
  once created+deleted it is tombstoned and recreation `400`s. **Use a fresh `ext_` name per
  failed attempt** (this demo is on `ext_energymcp5`; `ext_energymcp3/4` were burned/purged).
- **EntraOAuth BYO can fail server-side at connector creation.** Symptom: PPMI returns
  `500 ... identifierUris already exists` for `<name> - BYO`. This is a **retry artifact** —
  PPMI retries provisioning ~3× in one call (you'll see 3× duplicated `-A365Proxy`/
  `-PublicClients`/`-RemoteProxy` apps but one `- BYO`), and retries 2/3 collide with attempt
  1's `- BYO` (which holds the deterministic URI). The *real* attempt-1 failure is the
  connector step (`Failed to create connector … HTTP 400`, seen by others too). **NoAuth is
  unaffected.** If it reproduces across a *fresh* name with a clean payload, treat it as a
  **service-side issue**: consume the server via its **direct Entra-secured `/mcp` URL** (works
  for Azure AI Foundry) and escalate the Copilot/A365 path to Microsoft.
- **Keep the `-f` payload minimal** — only `serverName, serverUrl, authType, description,
  publisherName, tools, remoteScopes`. Null `externalOAuth`/`apiKey` blocks can make the
  EntraOAuth connector definition malformed; the known-good sample omits them.
- **Provision the Agent Tools SP for real** (see Prerequisites §4) — the tenant may lack it
  even though it is listed; missing → *"Couldn't complete consent for one or more apps."*
- **MOS caps the server short `description` at 80 chars** — longer fails the publish step.

### Using it after registration
1. **Admin approves** in M365 admin center → **Agents ▸ Tools ▸ Requests** (grants final
   consent). ~30 min to propagate.
2. **Copilot Studio** → agent → **Tools ▸ Add a tool ▸ Model Context Protocol** → pick the
   server; its tools become available.
3. **Test** e.g. *"Show the energy customer profile for C-1001."*
