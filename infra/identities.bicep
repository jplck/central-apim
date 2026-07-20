// One user-assigned managed identity per consumer Foundry resource. Created before
// the provider so its APIM policy can allow exactly these client IDs, and before the
// consumer accounts so they can be assigned this identity (breaks the circular dep).
param location string
param token string
param tags object
param consumers array

resource uami 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = [for c in consumers: {
  name: 'id-foundry-${c}-${token}'
  location: location
  tags: tags
}]

output clientIds array = [for (c, i) in consumers: uami[i].properties.clientId]
output principalIds array = [for (c, i) in consumers: uami[i].properties.principalId]
output resourceIds array = [for (c, i) in consumers: uami[i].id]
