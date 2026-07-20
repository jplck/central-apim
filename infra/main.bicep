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

output PROVIDER_FOUNDRY_NAME string = provider.outputs.foundryName
output PROVIDER_FOUNDRY_ENDPOINT string = provider.outputs.foundryEndpoint
output APIM_NAME string = provider.outputs.apimName
output APIM_GATEWAY_URL string = provider.outputs.apimGatewayUrl
output SHARED_MODEL_NAME string = provider.outputs.modelName
output CONNECTION_NAME string = consumers_.outputs.connectionName
// Use this as the agent model: <connection-name>/<model-name>
output AGENT_MODEL_DEPLOYMENT_NAME string = '${consumers_.outputs.connectionName}/${provider.outputs.modelName}'
output CONSUMER_PROJECT_ENDPOINTS array = consumers_.outputs.projectEndpoints
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
