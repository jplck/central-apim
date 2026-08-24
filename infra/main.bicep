// Multiple Foundry resources sharing ONE API Management instance to consume
// models hosted in a separate "provider" Foundry resource. All public, keyless
// (managed identity only). azd creates the two resource groups.
//
// Layout:
//   rg-<env>-provider  : Foundry A (owns gpt-4o-mini) + APIM (the model gateway)
//   rg-<env>-consumers : Foundry B, Foundry C (projects + shared APIM connection)
//                        + Azure Policy denying model deployments here.
targetScope = 'subscription'

@minLength(1)
@maxLength(24)
@description('azd environment name; seeds resource names and the azd-env-name tag.')
param environmentName string

@minLength(1)
@description('Azure region for all resources.')
param location string

@description('Object ID of the user/service principal running azd, granted Azure AI Developer on the consumer Foundry resources so you can build/run agents. Leave empty to skip.')
param principalId string = ''

// The consumer Foundry resources. Add more names here to prove N resources, one APIM.
param consumers array = [ 'b', 'c' ]

@description('Deploy Foundry hosted (containerized) agents: shared ACR + capability host on each consumer.')
param enableHostedAgents bool = true

@description('Which consumers get a hosted-agent capability host. Public hosting environments are capped per subscription+region (provisioning a 2nd deadlocks), so default to the first consumer only. Every consumer still consumes the central model via APIM.')
param hostedAgentConsumers array = take(consumers, 1)

@description('Provision an Azure Databricks workspace for a Genie agent that Microsoft Agent 365 ingests via external Registry sync ("Databricks Genie"). Off by default: the Genie space is UI-authored and the sync needs a Databricks service principal + Agent 365 licensing (see README). Enabling deploys a Premium workspace, which is free until a SQL warehouse runs.')
param enableDatabricks bool = false

@description('Deploy the demo "energy customer-profile" MCP server on Azure Container Apps (self-contained: own registry, identity, environment). The postprovision hook builds the image and swaps it onto the app. See README.')
param enableMcp bool = true

@description('Deploy Phase 1 of the governance proxy (proxy.md): a Container App (ACS host) that APIM calls synchronously to enforce a dynamic, data-driven kill switch, plus the Azure App Configuration store it reads revocations from. Self-contained and off by default; the proxy image is swapped on by a later hook.')
param enableProxy bool = false

var token = toLower(uniqueString(subscription().id, environmentName, location))
var tags = { 'azd-env-name': environmentName }

resource rgProvider 'Microsoft.Resources/resourceGroups@2024-03-01' = {
  name: 'rg-${environmentName}-provider'
  location: location
  tags: tags
}

resource rgConsumers 'Microsoft.Resources/resourceGroups@2024-03-01' = {
  name: 'rg-${environmentName}-consumers'
  location: location
  tags: tags
}

resource rgDatabricks 'Microsoft.Resources/resourceGroups@2024-03-01' = if (enableDatabricks) {
  name: 'rg-${environmentName}-databricks'
  location: location
  tags: tags
}

// 1. Consumer identities first: the provider's APIM policy needs their client IDs
//    to allow only these Foundry resources through the gateway.
module identities 'identities.bicep' = {
  scope: rgConsumers
  params: { location: location, token: token, tags: tags, consumers: consumers }
}

// 2. Provider Foundry + APIM gateway (allows the consumer identities, calls the
//    backend Foundry with APIM's own managed identity).
module provider 'provider.bicep' = {
  scope: rgProvider
  params: {
    location: location
    token: token
    tags: tags
    consumerClientIds: identities.outputs.clientIds
    // Wire the energy MCP route's backend to the MCP Container App. Native APIM MCP servers take the
    // backend BASE url (transport endpoint /mcp is set in mcpProperties). Referencing this output
    // makes Bicep deploy the (self-contained) mcp module before provider. Empty => no MCP route.
    #disable-next-line BCP318 // guarded by enableMcp; mcp is deployed whenever this is read.
    mcpBackendUrl: enableMcp ? mcp.outputs.baseUri : ''
  }
}

// 2b. Shared Azure Container Registry for hosted-agent images (provider RG, alongside
//     the shared gateway). Consumer identities get AcrPull; the deployer builds into it.
module acr 'acr.bicep' = if (enableHostedAgents) {
  scope: rgProvider
  params: {
    location: location
    token: token
    tags: tags
    pullPrincipalIds: identities.outputs.principalIds
    deployerPrincipalId: principalId
  }
}

// 3. Consumer Foundry resources: projects + account-level APIM connection + deny policy.
module consumers_ 'consumers.bicep' = {
  scope: rgConsumers
  params: {
    location: location
    token: token
    tags: tags
    principalId: principalId
    consumers: consumers
    identityResourceIds: identities.outputs.resourceIds
    identityClientIds: identities.outputs.clientIds
    apimGatewayUrl: provider.outputs.apimGatewayUrl
    apiPath: provider.outputs.apiPath
    modelName: provider.outputs.modelName
    modelVersion: provider.outputs.modelVersion
    enableHostedAgents: enableHostedAgents
    hostedAgentConsumers: hostedAgentConsumers
    #disable-next-line BCP318 // guarded by enableHostedAgents; acr is deployed whenever this is read.
    acrLoginServer: enableHostedAgents ? acr.outputs.loginServer : ''
    #disable-next-line BCP318
    acrResourceId: enableHostedAgents ? acr.outputs.id : ''
  }
}

// 4. Optional: Azure Databricks workspace for a Genie agent that Microsoft Agent 365
//    syncs via external Registry sync ("Databricks Genie"). Only the workspace is
//    provisioned here; the SQL warehouse + Genie space are authored in the workspace
//    (Genie spaces are UI-only — no create API), the sync service principal is created
//    with the Databricks CLI, and the Agent 365 connection is a manual admin-center
//    step. See the README "Databricks Genie + Agent 365" runbook.
module databricks 'databricks.bicep' = if (enableDatabricks) {
  scope: rgDatabricks
  params: { location: location, token: token, tags: tags }
}

// 5. Optional demo: an energy customer-profile MCP server on Container Apps. Self-contained
//    (own ACR + identity + environment) in the provider RG. Provisioned with a placeholder
//    image; the postprovision hook builds src/mcp-energy into the ACR and swaps it onto the app.
module mcp 'mcp.bicep' = if (enableMcp) {
  scope: rgProvider
  params: { location: location, token: token, tags: tags }
}

// 6. Optional Phase 1 of proxy.md: the governance-proxy Container App (an ACS host) + the
//    Azure App Configuration revocation store it reads. Self-contained in the provider RG,
//    provisioned with a placeholder image; a later hook builds the proxy image and swaps it on.
module proxy 'proxy.bicep' = if (enableProxy) {
  scope: rgProvider
  params: { location: location, token: token, tags: tags, deployerPrincipalId: principalId }
}

output PROVIDER_FOUNDRY_NAME string = provider.outputs.foundryName
output PROVIDER_FOUNDRY_ENDPOINT string = provider.outputs.foundryEndpoint
output APIM_NAME string = provider.outputs.apimName
output APIM_GATEWAY_URL string = provider.outputs.apimGatewayUrl
output SHARED_MODEL_NAME string = provider.outputs.modelName
output CONNECTION_NAME string = consumers_.outputs.connectionName
// Use this as the agent model: <connection-name>/<model-name>
output AGENT_MODEL_DEPLOYMENT_NAME string = '${consumers_.outputs.connectionName}/${provider.outputs.modelName}'
output CONSUMER_PROJECT_ENDPOINTS array = consumers_.outputs.projectEndpoints
// Consumer Entra appids (managed-identity client ids) the gateway accepts; index 0 = B, 1 = C.
// These are the values you revoke via the governance-proxy kill switch (App Config `revocations`).
output CONSUMER_CLIENT_IDS array = identities.outputs.clientIds
// Hosted (containerized) agents: shared registry + per-project ARM ids for the deploy hook.
output CONSUMER_PROJECT_RESOURCE_IDS array = consumers_.outputs.projectResourceIds
#disable-next-line BCP318 // guarded by enableHostedAgents; acr is deployed whenever this is read.
output ACR_NAME string = enableHostedAgents ? acr.outputs.name : ''
#disable-next-line BCP318
output ACR_LOGIN_SERVER string = enableHostedAgents ? acr.outputs.loginServer : ''
#disable-next-line BCP318 // ARM id of the registry; the deploy hook builds the image via ACR REST.
output ACR_ID string = enableHostedAgents ? acr.outputs.id : ''
output HOSTED_AGENT_NAME string = 'gateway-hosted'
// Only the consumers that actually got a capability host (subset) — the hosted-agent hook targets these.
output HOSTED_AGENT_PROJECT_ENDPOINTS array = enableHostedAgents ? consumers_.outputs.hostedProjectEndpoints : []
output HOSTED_AGENT_PROJECT_RESOURCE_IDS array = enableHostedAgents ? consumers_.outputs.hostedProjectResourceIds : []

// Databricks (optional): workspace for a Genie agent that Agent 365 syncs. Empty unless enableDatabricks.
#disable-next-line BCP318 // guarded by enableDatabricks; the workspace is deployed whenever this is read.
output DATABRICKS_WORKSPACE_URL string = enableDatabricks ? databricks.outputs.workspaceUrl : ''
#disable-next-line BCP318
output DATABRICKS_WORKSPACE_ID string = enableDatabricks ? databricks.outputs.workspaceId : ''

// MCP demo (optional): the deploy hook reads these to build the image and swap it onto the app.
#disable-next-line BCP318 // guarded by enableMcp; the module is deployed whenever these are read.
output MCP_ACR_ID string = enableMcp ? mcp.outputs.acrId : ''
#disable-next-line BCP318
output MCP_ACR_LOGIN_SERVER string = enableMcp ? mcp.outputs.acrLoginServer : ''
#disable-next-line BCP318
output MCP_APP_ID string = enableMcp ? mcp.outputs.appId : ''
#disable-next-line BCP318
output MCP_URI string = enableMcp ? mcp.outputs.uri : ''

// The energy MCP server fronted by the APIM gateway (governance fragment applies): auth + kill switch.
output MCP_GATEWAY_URL string = enableMcp ? '${provider.outputs.apimGatewayUrl}/${provider.outputs.mcpApiPath}/mcp' : ''

// Governance proxy (optional, Phase 1): App Config store to write kills into, and the proxy
// app the later image-swap hook targets.
#disable-next-line BCP318 // guarded by enableProxy; the module is deployed whenever these are read.
output PROXY_APP_CONFIG_NAME string = enableProxy ? proxy.outputs.appConfigName : ''
#disable-next-line BCP318
output PROXY_APP_CONFIG_ENDPOINT string = enableProxy ? proxy.outputs.appConfigEndpoint : ''
// Phase 2 ingest: point Defender continuous export (as a trusted service) at this Event Hub.
#disable-next-line BCP318
output PROXY_EVENTHUB_NAMESPACE string = enableProxy ? proxy.outputs.eventHubNamespace : ''
#disable-next-line BCP318
output PROXY_EVENTHUB_NAME string = enableProxy ? proxy.outputs.eventHubName : ''
#disable-next-line BCP318
output PROXY_EVENTHUB_NAMESPACE_FQDN string = enableProxy ? proxy.outputs.eventHubNamespaceFqdn : ''
#disable-next-line BCP318
output PROXY_ACR_ID string = enableProxy ? proxy.outputs.acrId : ''
#disable-next-line BCP318
output PROXY_ACR_LOGIN_SERVER string = enableProxy ? proxy.outputs.acrLoginServer : ''
#disable-next-line BCP318
output PROXY_APP_ID string = enableProxy ? proxy.outputs.appId : ''
#disable-next-line BCP318
output PROXY_URI string = enableProxy ? proxy.outputs.uri : ''
