// Optional demo: an energy-provider customer-profile MCP server on Azure Container
// Apps. Self-contained (own registry, identity, environment) so it toggles cleanly
// via enableMcp. All public, keyless — the app pulls its image with a user-assigned
// managed identity (AcrPull). The postprovision hook builds the image into this ACR
// and swaps it onto the app (see infra/hooks/deploy_mcp.py).
param location string
param token string
param tags object

// Placeholder image + port 80 so the FIRST revision is healthy and provisioning
// succeeds; deploy_mcp.py then swaps in the real image and target port 8000.
param placeholderImage string = 'mcr.microsoft.com/azuredocs/containerapps-helloworld:latest'

resource law 'Microsoft.OperationalInsights/workspaces@2023-09-01' = {
  name: 'law-mcp-${token}'
  location: location
  tags: tags
  properties: {
    sku: { name: 'PerGB2018' }
    retentionInDays: 30
  }
}

resource acr 'Microsoft.ContainerRegistry/registries@2023-11-01-preview' = {
  name: 'acrmcp${token}' // distinct from the hosted-agent ACR (acr<token>) so both can coexist
  location: location
  tags: tags
  sku: { name: 'Basic' }
  properties: {
    adminUserEnabled: false // keyless: pull with managed identity only
    publicNetworkAccess: 'Enabled'
  }
}

resource uami 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = {
  name: 'id-mcp-${token}'
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

resource env 'Microsoft.App/managedEnvironments@2024-03-01' = {
  name: 'cae-mcp-${token}'
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
  name: 'ca-mcp-${token}'
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
        targetPort: 80 // hook swaps to 8000 with the real image
        transport: 'auto'
      }
      registries: [ { server: acr.properties.loginServer, identity: uami.id } ]
    }
    template: {
      containers: [ {
        name: 'mcp'
        image: placeholderImage
        resources: { cpu: json('0.5'), memory: '1Gi' }
      } ]
      scale: { minReplicas: 1, maxReplicas: 1 }
    }
  }
  dependsOn: [ pull ]
}

output acrId string = acr.id
output acrLoginServer string = acr.properties.loginServer
output appId string = aca.id
output uri string = 'https://${aca.properties.configuration.ingress.fqdn}/mcp'
