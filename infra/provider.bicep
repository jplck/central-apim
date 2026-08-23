// Provider: Foundry resource A (owns the real model) + API Management acting as the
// shared model gateway. Keyless end-to-end:
//   consumer project MI --(Entra token, validated by client-id)--> APIM
//   APIM system MI       --(Entra token, Cognitive Services OpenAI User)--> Foundry A
param location string
param token string
param tags object

@description('Client IDs of the consumer Foundry managed identities allowed through the gateway.')
param consumerClientIds array

@description('Backend BASE URL of the energy MCP Container App (no path; the /mcp transport endpoint is set on the MCP server). Empty => the MCP route is not added to the gateway.')
param mcpBackendUrl string = ''

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

// The Entra-token validation shared by every gateway API: accept the allowed consumer identities
// (by client id) plus the hosted agent's Entra Agent ID *blueprint* appid (gateway-agent-appid,
// set post-deploy by create_hosted_agents.py; harmless placeholder until then). Audience =
// cognitiveservices. Lives in the reusable fragment below.
var validateXml = '<validate-azure-ad-token tenant-id="${tenant().tenantId}" header-name="Authorization" failed-validation-httpcode="401" failed-validation-error-message="Unauthorized. A valid Entra token from an allowed Foundry resource is required."><client-application-ids>${allowedAppIds}<application-id>{{gateway-agent-appid}}</application-id></client-application-ids><audiences><audience>https://cognitiveservices.azure.com</audience></audiences></validate-azure-ad-token>'

// Phase 1 kill switch (proxy.md): after validating the caller, ask the governance proxy whether
// this caller's Entra appid is revoked, and fail closed (403) on deny / non-200 / unreachable.
// Inert until the proxy deploy hook points the `governance-proxy-host` named value at the proxy.
// We store the *host* (no scheme), not a full URL: a `//` inside an APIM `@()` expression is
// misparsed as a comment and swallows the closing `)`, so the guard must compare a host string.
// Default is a non-resolving sentinel host (RFC 2606 `.invalid`) => the guard skips the whole
// block. <set-url> prepends https:// (in element text, where `//` is fine) so it stays a valid
// absolute URL, which APIM statically validates even inside a <choose> that never runs.
var killSwitchXml = '<set-variable name="agentId" value="@{ var jwt = context.Request.Headers.GetValueOrDefault(&quot;Authorization&quot;, &quot;&quot;).Replace(&quot;Bearer &quot;, &quot;&quot;).AsJwt(); return jwt == null ? &quot;unknown&quot; : (jwt.Claims.GetValueOrDefault(&quot;appid&quot;) ?? jwt.Claims.GetValueOrDefault(&quot;azp&quot;) ?? &quot;unknown&quot;); }" /><choose><when condition="@(&quot;{{governance-proxy-host}}&quot; != &quot;disabled.invalid&quot;)"><send-request mode="new" response-variable-name="killResp" timeout="5" ignore-error="true"><set-url>https://{{governance-proxy-host}}/check</set-url><set-method>POST</set-method><set-header name="Content-Type" exists-action="override"><value>application/json</value></set-header><set-body>@{ return new JObject(new JProperty("agent_id", (string)context.Variables["agentId"])).ToString(); }</set-body></send-request><choose><when condition="@{ var r = context.Variables.GetValueOrDefault&lt;IResponse&gt;(&quot;killResp&quot;); if (r == null || r.StatusCode != 200) { return true; } try { return ((string)r.Body.As&lt;JObject&gt;(true)[&quot;verdict&quot;]) != &quot;allow&quot;; } catch { return true; } }"><return-response><set-status code="403" reason="Forbidden" /><set-header name="Content-Type" exists-action="override"><value>application/json</value></set-header><set-body>{"error":"agent revoked or governance proxy unavailable"}</set-body></return-response></when></choose></when></choose>'

// The reusable governance fragment: validate the Entra token, then run the kill switch. Included
// by every API (foundry models + energy MCP) so the same auth + revocation check applies once,
// gateway-wide. Change governance here and every route inherits it.
var governanceFragmentXml = '<fragment>${validateXml}${killSwitchXml}</fragment>'

// Foundry models API: governance fragment, then swap to APIM's managed identity for the backend.
var apiPolicyXml = '<policies><inbound><base /><include-fragment fragment-id="governance-check" /><authentication-managed-identity resource="https://cognitiveservices.azure.com" /><set-backend-service base-url="${foundryA.properties.endpoint}openai" /></inbound><backend><base /></backend><outbound><base /></outbound><on-error><base /></on-error></policies>'

// The proxy *host* (no scheme) the kill switch calls. Default is a non-resolving sentinel host
// (`.invalid`) that keeps the switch off; the proxy deploy hook (deploy_proxy.py) sets this to
// the live proxy host when enableProxy provisions it. Host-only so no `//` lands in the guard
// expression (see killSwitchXml note).
resource proxyHostNv 'Microsoft.ApiManagement/service/namedValues@2024-05-01' = {
  parent: apim
  name: 'governance-proxy-host'
  properties: {
    displayName: 'governance-proxy-host'
    value: 'disabled.invalid'
    secret: false
  }
}

// The hosted agent's Entra Agent ID *blueprint* appid, allowed through the gateway so its MCP
// calls pass validate-azure-ad-token. Defaults to a never-matching placeholder; the hosted-agent
// deploy hook (create_hosted_agents.py) sets it to the real blueprint appid post-provision. One
// appid because all agent instances of a blueprint share its appid (aka client id).
resource agentAppIdNv 'Microsoft.ApiManagement/service/namedValues@2024-05-01' = {
  parent: apim
  name: 'gateway-agent-appid'
  properties: {
    displayName: 'gateway-agent-appid'
    value: '00000000-0000-0000-0000-000000000000'
    secret: false
  }
}

// Reusable governance policy fragment: validate-azure-ad-token + kill switch. Referenced by every
// API via <include-fragment fragment-id="governance-check" />. Depends on the named value because
// the kill switch references {{governance-proxy-host}} (must exist when the fragment is validated).
resource governanceFragment 'Microsoft.ApiManagement/service/policyFragments@2024-05-01' = {
  parent: apim
  name: 'governance-check'
  properties: {
    description: 'Entra token validation + dynamic kill switch (proxy.md), reusable across APIs.'
    value: governanceFragmentXml
    format: 'rawxml'
  }
  dependsOn: [ proxyHostNv, agentAppIdNv ]
}

resource apiPolicy 'Microsoft.ApiManagement/service/apis/policies@2024-05-01' = {
  parent: api
  name: 'policy'
  properties: { value: apiPolicyXml, format: 'rawxml' }
  dependsOn: [ chatCompletions, governanceFragment ]
}

// Energy MCP server route (only when the MCP demo is enabled). Exposed as a NATIVE APIM MCP server
// (type: 'mcp', passthrough to the external backend) instead of a generic HTTP passthrough, so APIM
// is MCP-protocol-aware: it surfaces the backend's tools as first-class API-tool sub-resources,
// handles streamable-HTTP transport correctly, and can be registered/discovered in API Center. The
// SAME governance fragment attaches as the API policy, so auth + kill-switch are identical to the
// model route. The MCP server itself is unauth, so APIM is its auth + kill-switch enforcement point.
// Requires api-version 2025-09-01-preview. Client endpoint: https://<apim>/energy-mcp/mcp ;
// backend = the Container App base + the /mcp transport endpoint below.
resource mcpApi 'Microsoft.ApiManagement/service/apis@2025-09-01-preview' = if (!empty(mcpBackendUrl)) {
  parent: apim
  name: 'energy-mcp'
  properties: {
    type: 'mcp'
    displayName: 'Energy MCP'
    path: 'energy-mcp'
    protocols: [ 'https' ]
    subscriptionRequired: false
    serviceUrl: mcpBackendUrl
    mcpProperties: {
      transportType: 'streamable'
      // ARM wants endpoints as an object keyed by endpoint name, not an array (the doc examples show
      // an array, but the live 2025-09-01-preview API deserializes a Dictionary<string,Endpoint>).
      endpoints: {
        message: { uriTemplate: '/mcp' }
      }
    }
  }
}

resource mcpPolicy 'Microsoft.ApiManagement/service/apis/policies@2025-09-01-preview' = if (!empty(mcpBackendUrl)) {
  parent: mcpApi
  name: 'policy'
  properties: {
    value: '<policies><inbound><base /><include-fragment fragment-id="governance-check" /></inbound><backend><base /></backend><outbound><base /></outbound><on-error><base /></on-error></policies>'
    format: 'rawxml'
  }
  dependsOn: [ governanceFragment ]
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
output mcpApiPath string = empty(mcpBackendUrl) ? '' : 'energy-mcp'
output modelName string = modelName
output modelVersion string = modelVersion
