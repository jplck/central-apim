// Shared Azure Container Registry for hosted-agent images. Both consumer Foundry
// projects pull from it with their managed identity (AcrPull); the azd deployer
// builds into it via `az acr build`. Keyless (no admin user).
param location string
param token string
param tags object

@description('Principal IDs of the consumer Foundry managed identities that pull images (AcrPull).')
param pullPrincipalIds array

@description('Object ID of the azd deployer, granted Contributor on this registry so `az acr build` can queue builds. Empty to skip.')
param deployerPrincipalId string = ''

resource acr 'Microsoft.ContainerRegistry/registries@2023-11-01-preview' = {
  #disable-next-line BCP334 // 'acr' + 13-char uniqueString token = 16 chars, always >= 5.
  name: 'acr${token}'
  location: location
  tags: tags
  sku: { name: 'Standard' }
  properties: {
    adminUserEnabled: false // keyless: pull with managed identity only
    publicNetworkAccess: 'Enabled'
  }
}

var acrPullRoleId = '7f951dda-4ed3-4680-a7ca-43fe172d538d' // AcrPull
resource pull 'Microsoft.Authorization/roleAssignments@2022-04-01' = [for pid in pullPrincipalIds: {
  scope: acr
  name: guid(acr.id, pid, acrPullRoleId)
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', acrPullRoleId)
    principalId: pid
    principalType: 'ServicePrincipal'
  }
}]

// `az acr build` runs a server-side ACR Task -> needs the scheduleRun action, which
// only Owner/Contributor grant. Scoped to this one registry to keep it minimal.
// ponytail: skip if the deployer is already subscription Owner/Contributor.
var contributorRoleId = 'b24988ac-6180-42a0-ab88-20f7382dd24c'
resource build 'Microsoft.Authorization/roleAssignments@2022-04-01' = if (!empty(deployerPrincipalId)) {
  scope: acr
  name: guid(acr.id, deployerPrincipalId, contributorRoleId)
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', contributorRoleId)
    principalId: deployerPrincipalId
  }
}

output name string = acr.name
output loginServer string = acr.properties.loginServer
output id string = acr.id
