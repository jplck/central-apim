// Azure Databricks workspace for a Genie agent that Microsoft Agent 365 syncs into
// its registry via external "Registry sync" (supported platform: "Databricks Genie").
//
// Only the workspace is provisionable here. The SQL warehouse, the Genie space, and
// the service principal that Agent 365 authenticates with are workspace-internal and
// are set up out-of-band via the README runbook (Genie spaces are UI-authored — there
// is no create API — so this is manual by necessity, not by choice). The Genie space
// runs over Databricks' built-in `samples` catalog, so no storage account / Unity
// Catalog table / access connector is needed for the demo.
param location string
param token string
param tags object

// Databricks needs a managed resource group it owns; the resource provider creates it,
// so it must not pre-exist. Unique per environment via the shared token.
var managedRgId = subscriptionResourceId('Microsoft.Resources/resourceGroups', 'rg-dbw-managed-${token}')

resource workspace 'Microsoft.Databricks/workspaces@2024-05-01' = {
  #disable-next-line BCP334 // 'dbw-' + 13-char uniqueString token = 17 chars, within limits.
  name: 'dbw-${token}'
  location: location
  tags: tags
  // Premium: needed for serverless SQL (Genie) + secret ACLs for the sync service principal.
  sku: { name: 'premium' }
  properties: {
    managedResourceGroupId: managedRgId
    publicNetworkAccess: 'Enabled' // all-public, like the rest of the sample
  }
}

output workspaceName string = workspace.name
output workspaceId string = workspace.id
// Per-workspace host (e.g. adb-<id>.<n>.azuredatabricks.net); the sync connection wants the https URL.
output workspaceUrl string = 'https://${workspace.properties.workspaceUrl}'
