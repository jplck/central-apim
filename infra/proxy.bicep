// Phase 1 of proxy.md: the governance-proxy Container App (an ACS host) that APIM calls
// synchronously to enforce a dynamic, data-driven kill switch. Self-contained (own registry,
// identity, environment) so it toggles cleanly via enableProxy. All public, keyless — the app
// pulls its image with a user-assigned managed identity (AcrPull) and reads revocations from
// Azure App Configuration with that same identity (App Configuration Data Reader). A later
// hook builds the proxy image into this ACR and swaps it (+ real port) onto the app.
param location string
param token string
param tags object

@description('Object ID of the azd deployer, granted App Configuration Data Owner so kill/unkill via `az appconfig kv set` works keylessly. Empty to skip.')
param deployerPrincipalId string = ''

// Placeholder image + port 80 so the FIRST revision is healthy and provisioning succeeds; a
// later hook swaps in the real proxy image and its target port.
param placeholderImage string = 'mcr.microsoft.com/azuredocs/containerapps-helloworld:latest'

resource law 'Microsoft.OperationalInsights/workspaces@2023-09-01' = {
  name: 'law-proxy-${token}'
  location: location
  tags: tags
  properties: {
    sku: { name: 'PerGB2018' }
    retentionInDays: 30
  }
}

resource acr 'Microsoft.ContainerRegistry/registries@2023-11-01-preview' = {
  name: 'acrproxy${token}'
  location: location
  tags: tags
  sku: { name: 'Basic' }
  properties: {
    adminUserEnabled: false // keyless: pull with managed identity only
    publicNetworkAccess: 'Enabled'
  }
}

resource uami 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = {
  name: 'id-proxy-${token}'
  location: location
  tags: tags
}

var acrPullRoleId = '7f951dda-4ed3-4680-a7ca-43fe172d538d' // AcrPull
resource pull 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: acr
  name: guid(acr.id, uami.id, acrPullRoleId)
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', acrPullRoleId)
    principalId: uami.properties.principalId
    principalType: 'ServicePrincipal'
  }
}

// The revocation store. Keyless: local (access-key) auth disabled — Entra only, matching the
// environment's no-access-keys rule. Killing an agent = one `az appconfig kv set`; the proxy
// polls this store's `data.revocations` and denies on the next call. No redeploy, no XML edit.
resource appconfig 'Microsoft.AppConfiguration/configurationStores@2023-03-01' = {
  name: 'appcs-proxy-${token}'
  location: location
  tags: tags
  sku: { name: 'standard' }
  properties: {
    disableLocalAuth: true
    publicNetworkAccess: 'Enabled'
  }
}

var appConfigDataReaderRoleId = '516239f1-63e1-4d78-a4de-a74fb236a071' // App Configuration Data Reader
resource read 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: appconfig
  name: guid(appconfig.id, uami.id, appConfigDataReaderRoleId)
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', appConfigDataReaderRoleId)
    principalId: uami.properties.principalId
    principalType: 'ServicePrincipal'
  }
}

// Deployer gets Data Owner so `az appconfig kv set` (kill/unkill) works without access keys.
var appConfigDataOwnerRoleId = '5ae67dd6-50cb-40e7-96ff-dc2bfa4b606b' // App Configuration Data Owner
resource ownerAssign 'Microsoft.Authorization/roleAssignments@2022-04-01' = if (!empty(deployerPrincipalId)) {
  scope: appconfig
  name: guid(appconfig.id, deployerPrincipalId, appConfigDataOwnerRoleId)
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', appConfigDataOwnerRoleId)
    principalId: deployerPrincipalId
  }
}

resource env 'Microsoft.App/managedEnvironments@2024-03-01' = {
  name: 'cae-proxy-${token}'
  location: location
  tags: tags
  properties: {
    appLogsConfiguration: {
      destination: 'log-analytics'
      logAnalyticsConfiguration: {
        customerId: law.properties.customerId
        sharedKey: law.listKeys().primarySharedKey
      }
    }
  }
}

resource aca 'Microsoft.App/containerApps@2024-03-01' = {
  name: 'ca-proxy-${token}'
  location: location
  tags: tags
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: { '${uami.id}': {} }
  }
  properties: {
    managedEnvironmentId: env.id
    configuration: {
      ingress: {
        external: true
        targetPort: 80 // hook swaps to the real proxy port with the real image
        transport: 'auto'
      }
      registries: [ { server: acr.properties.loginServer, identity: uami.id } ]
    }
    template: {
      containers: [ {
        name: 'proxy'
        image: placeholderImage
        resources: { cpu: json('0.5'), memory: '1Gi' }
        // Where and how the ACS host reads revocations — keyless via the user-assigned MI.
        env: [
          { name: 'APP_CONFIG_ENDPOINT', value: appconfig.properties.endpoint }
          { name: 'AZURE_CLIENT_ID', value: uami.properties.clientId }
        ]
      } ]
      scale: { minReplicas: 1, maxReplicas: 1 }
    }
  }
  dependsOn: [ pull, read ]
}

output acrId string = acr.id
output acrLoginServer string = acr.properties.loginServer
output appId string = aca.id
output appConfigName string = appconfig.name
output appConfigEndpoint string = appconfig.properties.endpoint
output uri string = 'https://${aca.properties.configuration.ingress.fqdn}'
