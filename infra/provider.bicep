// Provider: Foundry resource A (owns the real model) + API Management acting as the
// shared model gateway. Keyless end-to-end:
//   consumer project MI --(Entra token, validated by client-id)--> APIM
//   APIM system MI       --(Entra token, Cognitive Services OpenAI User)--> Foundry A
param location string
param token string
param tags object

@description('Client IDs of the consumer Foundry managed identities allowed through the gateway.')
param consumerClientIds array

param modelName string = 'gpt-4.1'
param modelVersion string = '2025-04-14'
param modelCapacity int = 50
param apiPath string = 'foundry'
param publisherEmail string = 'admin@example.com'
param publisherName string = 'Contoso AI Platform'

// --- Foundry resource A (the model provider) -------------------------------
resource foundryA 'Microsoft.CognitiveServices/accounts@2025-04-01-preview' = {
  name: 'aoai-provider-${token}'
  location: location
  tags: tags
  kind: 'AIServices'
  sku: { name: 'S0' }
  identity: { type: 'SystemAssigned' }
  properties: {
    allowProjectManagement: true
    customSubDomainName: 'aoai-provider-${token}'
    publicNetworkAccess: 'Enabled'
    disableLocalAuth: true // no access keys
  }
}

resource model 'Microsoft.CognitiveServices/accounts/deployments@2024-10-01' = {
  parent: foundryA
  name: modelName
  sku: { name: 'GlobalStandard', capacity: modelCapacity }
  properties: {
    model: { format: 'OpenAI', name: modelName, version: modelVersion }
  }
}

// --- API Management: the shared model gateway ------------------------------
resource apim 'Microsoft.ApiManagement/service@2024-05-01' = {
  name: 'apim-${token}'
  location: location
  tags: tags
  sku: { name: 'StandardV2', capacity: 1 } // v2 (or Premium) required by the Foundry BYOM feature
  identity: { type: 'SystemAssigned' }
  properties: {
    publisherEmail: publisherEmail
    publisherName: publisherName
  }
}

// AzureOpenAI-style API: /deployments/{deploymentName}/chat/completions
resource api 'Microsoft.ApiManagement/service/apis@2024-05-01' = {
  parent: apim
  name: 'foundry-models'
  properties: {
    displayName: 'Foundry Models'
    path: apiPath
    protocols: [ 'https' ]
    subscriptionRequired: false // keyless: authenticate with Entra tokens only
    // ponytail: backend = account endpoint (…cognitiveservices.azure.com) + /openai;
    // if a model 404s through APIM, switch to https://<name>.openai.azure.com/openai.
    serviceUrl: '${foundryA.properties.endpoint}openai'
  }
}

resource chatCompletions 'Microsoft.ApiManagement/service/apis/operations@2024-05-01' = {
  parent: api
  name: 'chat-completions'
  properties: {
    displayName: 'Chat Completions'
    method: 'POST'
    urlTemplate: '/deployments/{deploymentName}/chat/completions'
    templateParameters: [ { name: 'deploymentName', type: 'string', required: true } ]
  }
}

// API-level policy: validate the caller is one of the allowed consumer identities,
// then swap to APIM's own managed identity to reach the backend Foundry keyless.
var allowedAppIds = join(map(consumerClientIds, id => '<application-id>${id}</application-id>'), '')

// Phase 1 kill switch (proxy.md): after validating the caller, ask the governance proxy whether
// this caller's Entra appid is revoked, and fail closed (403) on deny / non-200 / unreachable.
// Inert until the proxy deploy hook points the `governance-proxy-url` named value at the proxy
// (default 'none' => the whole block is skipped, so the gateway is unchanged and has zero overhead).
var killSwitchXml = '<set-variable name="agentId" value="@{ var jwt = context.Request.Headers.GetValueOrDefault(&quot;Authorization&quot;, &quot;&quot;).Replace(&quot;Bearer &quot;, &quot;&quot;).AsJwt(); return jwt == null ? &quot;unknown&quot; : jwt.Claims.GetValueOrDefault(&quot;appid&quot;, jwt.Claims.GetValueOrDefault(&quot;azp&quot;, &quot;unknown&quot;)); }" /><choose><when condition="@(&quot;{{governance-proxy-url}}&quot; != &quot;none&quot;)"><send-request mode="new" response-variable-name="killResp" timeout="5" ignore-error="true"><set-url>{{governance-proxy-url}}/check</set-url><set-method>POST</set-method><set-header name="Content-Type" exists-action="override"><value>application/json</value></set-header><set-body>@{ return new JObject(new JProperty("agent_id", (string)context.Variables["agentId"])).ToString(); }</set-body></send-request><choose><when condition="@{ var r = context.Variables.GetValueOrDefault&lt;IResponse&gt;(&quot;killResp&quot;); if (r == null || r.StatusCode != 200) { return true; } try { return ((string)r.Body.As&lt;JObject&gt;(true)[&quot;verdict&quot;]) != &quot;allow&quot;; } catch { return true; } }"><return-response><set-status code="403" reason="Forbidden" /><set-header name="Content-Type" exists-action="override"><value>application/json</value></set-header><set-body>{"error":"agent revoked or governance proxy unavailable"}</set-body></return-response></when></choose></when></choose>'

var apiPolicyXml = '<policies><inbound><base /><validate-azure-ad-token tenant-id="${tenant().tenantId}" header-name="Authorization" failed-validation-httpcode="401" failed-validation-error-message="Unauthorized. A valid Entra token from an allowed Foundry resource is required."><client-application-ids>${allowedAppIds}</client-application-ids><audiences><audience>https://cognitiveservices.azure.com</audience></audiences></validate-azure-ad-token>${killSwitchXml}<authentication-managed-identity resource="https://cognitiveservices.azure.com" /><set-backend-service base-url="${foundryA.properties.endpoint}openai" /></inbound><backend><base /></backend><outbound><base /></outbound><on-error><base /></on-error></policies>'

// The proxy URL the kill switch calls. Default 'none' keeps the switch off; the proxy deploy
// hook (deploy_proxy.py) sets this to the live proxy URL when enableProxy provisions it.
resource proxyUrlNv 'Microsoft.ApiManagement/service/namedValues@2024-05-01' = {
  parent: apim
  name: 'governance-proxy-url'
  properties: {
    displayName: 'governance-proxy-url'
    value: 'none'
    secret: false
  }
}

resource apiPolicy 'Microsoft.ApiManagement/service/apis/policies@2024-05-01' = {
  parent: api
  name: 'policy'
  properties: { value: apiPolicyXml, format: 'rawxml' }
  dependsOn: [ chatCompletions, proxyUrlNv ]
}

// APIM's managed identity may call the provider Foundry's models, keyless.
var openAiUserRoleId = '5e0bd9bd-7b93-4f28-af87-19fc36ad61bd' // Cognitive Services OpenAI User
resource apimToFoundryA 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: foundryA
  name: guid(foundryA.id, apim.id, openAiUserRoleId)
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', openAiUserRoleId)
    principalId: apim.identity.principalId
    principalType: 'ServicePrincipal'
  }
}

output foundryName string = foundryA.name
output foundryEndpoint string = foundryA.properties.endpoint
output apimName string = apim.name
output apimGatewayUrl string = apim.properties.gatewayUrl
output apiPath string = apiPath
output modelName string = modelName
output modelVersion string = modelVersion
