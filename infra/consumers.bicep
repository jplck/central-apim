// Consumer Foundry resources (B, C, ...). Each has a project and an ACCOUNT-LEVEL
// APIM connection (isSharedToAll: all projects in the resource share it). They own
// no models of their own — an Azure Policy denies model deployments in this RG.
param location string
param token string
param tags object
param principalId string
param consumers array

@description('Resource IDs of the per-consumer user-assigned identities (same order as consumers).')
param identityResourceIds array

@description('Client IDs of the per-consumer user-assigned identities (same order as consumers).')
param identityClientIds array = []

param apimGatewayUrl string
param apiPath string
param modelName string
param modelVersion string
param inferenceApiVersion string = '2024-10-21'

@description('Enable Foundry hosted (containerized) agents: capability host + shared-ACR connection on each consumer.')
param enableHostedAgents bool = true

@description('Which consumers get a hosted-agent capability host. Public hosting environments are capped per subscription+region (a 2nd one deadlocks), so default to just the first consumer. All consumers still consume the central model via APIM regardless.')
param hostedAgentConsumers array = take(consumers, 1)

@description('Login server of the shared ACR that hosts agent images (e.g. acrxxxx.azurecr.io).')
param acrLoginServer string = ''

@description('Resource ID of the shared ACR.')
param acrResourceId string = ''

var connectionName = 'apim-shared'

// Static model list advertised to Foundry agents (avoids needing discovery endpoints
// on APIM). deploymentInPath routes as /deployments/{name}/chat/completions.
var staticModels = [
  { name: modelName, properties: { model: { name: modelName, version: modelVersion, format: 'OpenAI' } } }
]

resource consumerFoundry 'Microsoft.CognitiveServices/accounts@2025-04-01-preview' = [for (c, i) in consumers: {
  name: 'aoai-consumer-${c}-${token}'
  location: location
  tags: tags
  kind: 'AIServices'
  sku: { name: 'S0' }
  identity: { type: 'UserAssigned', userAssignedIdentities: { '${identityResourceIds[i]}': {} } }
  properties: {
    allowProjectManagement: true
    customSubDomainName: 'aoai-consumer-${c}-${token}'
    publicNetworkAccess: 'Enabled'
    disableLocalAuth: true
  }
}]

resource project 'Microsoft.CognitiveServices/accounts/projects@2025-04-01-preview' = [for (c, i) in consumers: {
  parent: consumerFoundry[i]
  name: 'proj-${c}'
  location: location
  tags: tags
  identity: { type: 'UserAssigned', userAssignedIdentities: { '${identityResourceIds[i]}': {} } }
  properties: {
    displayName: 'Project ${toUpper(c)}'
  }
}]

// Account-level connection to the shared APIM gateway, authenticated with the
// project's managed identity (keyless). Shared to every project in the resource.
resource connection 'Microsoft.CognitiveServices/accounts/connections@2025-04-01-preview' = [for (c, i) in consumers: {
  parent: consumerFoundry[i]
  name: connectionName
  properties: {
    category: 'ApiManagement'
    target: '${apimGatewayUrl}/${apiPath}'
    #disable-next-line BCP036 // 'ProjectManagedIdentity' is valid here per Microsoft's Foundry BYOM sample; Bicep type metadata is stale.
    authType: 'ProjectManagedIdentity'
    audience: 'https://cognitiveservices.azure.com'
    isSharedToAll: true
    credentials: {}
    metadata: {
      deploymentInPath: 'true'
      inferenceAPIVersion: inferenceApiVersion
      models: string(staticModels)
    }
  }
}]

// Enable the Foundry Agent Service hosting environment on a consumer account so hosted
// (containerized) agents can run. Public hosting => no BYO network needed.
// Public hosting environments are capped per subscription+region: provisioning a 2nd one
// in the same sub+region deadlocks in "Creating" for ~50 min then fails. So we only create
// one here (hostedAgentConsumers defaults to the first consumer). Others stay model-only.
@batchSize(1)
resource capabilityHost 'Microsoft.CognitiveServices/accounts/capabilityHosts@2025-10-01-preview' = [for (c, i) in consumers: if (enableHostedAgents && contains(hostedAgentConsumers, c)) {
  parent: consumerFoundry[i]
  name: 'agents'
  properties: {
    capabilityHostKind: 'Agents'
    enablePublicHostingEnvironment: true
  }
}]

// Connection so each hosting project can pull hosted-agent images from the shared ACR with
// its managed identity (which holds AcrPull). Keyless. Shared to all projects.
resource acrConnection 'Microsoft.CognitiveServices/accounts/connections@2025-04-01-preview' = [for (c, i) in consumers: if (enableHostedAgents && contains(hostedAgentConsumers, c)) {
  parent: consumerFoundry[i]
  name: 'hosted-agents-acr'
  properties: {
    category: 'ContainerRegistry'
    target: acrLoginServer
    authType: 'ManagedIdentity'
    isSharedToAll: true
    credentials: {
      clientId: identityClientIds[i]
      resourceId: acrResourceId
    }
    metadata: {
      ResourceId: acrResourceId
    }
  }
}]
// Foundry User grants the `Microsoft.CognitiveServices/*` dataAction (includes
// accounts/AIServices/agents/write). Azure AI Developer does NOT — its dataActions
// only cover OpenAI/Speech/ContentSafety/MaaS, so agent creation 403s.
var foundryUserRoleId = '53ca6127-db72-4b80-b1b0-d745d6d5456d' // Foundry User
resource deployerAccess 'Microsoft.Authorization/roleAssignments@2022-04-01' = [for (c, i) in consumers: if (!empty(principalId)) {
  scope: consumerFoundry[i]
  name: guid(consumerFoundry[i].id, principalId, foundryUserRoleId)
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', foundryUserRoleId)
    principalId: principalId
  }
}]

// Block model deployments in this resource group (consumers must use the gateway).
// Built-in "Not allowed resource types".
resource denyModelDeployments 'Microsoft.Authorization/policyAssignments@2024-04-01' = {
  name: 'deny-model-deployments'
  properties: {
    displayName: 'Deny model deployments in consumer Foundry resources'
    description: 'Consumer Foundry resources must consume models through the shared APIM gateway, not deploy their own.'
    policyDefinitionId: '/providers/Microsoft.Authorization/policyDefinitions/6c112d4e-5bc7-47ae-a041-ea2d9dccd749'
    parameters: {
      listOfResourceTypesNotAllowed: { value: [ 'Microsoft.CognitiveServices/accounts/deployments' ] }
    }
    enforcementMode: 'Default'
  }
}

output connectionName string = connectionName
output projectEndpoints array = [for (c, i) in consumers: 'https://aoai-consumer-${c}-${token}.services.ai.azure.com/api/projects/proj-${c}']
output projectResourceIds array = [for (c, i) in consumers: project[i].id]
// Subset that actually hosts agents (has a capability host). Same predicate/order for both,
// so the filtered endpoint/id arrays stay aligned. Consumed by the hosted-agent deploy hook.
var hostedEndpointsAll = [for (c, i) in consumers: contains(hostedAgentConsumers, c) ? 'https://aoai-consumer-${c}-${token}.services.ai.azure.com/api/projects/proj-${c}' : '']
var hostedArmIdsAll = [for (c, i) in consumers: contains(hostedAgentConsumers, c) ? project[i].id : '']
output hostedProjectEndpoints array = filter(hostedEndpointsAll, e => !empty(e))
output hostedProjectResourceIds array = filter(hostedArmIdsAll, e => !empty(e))
