// DataTrust AI on Azure, as its own service (its own resource group, server, registry, vault and identity).
// The POC data pipeline is a different service (infra/poc); DataTrust only connects to its database as a monitored source.
//
//   Browser ──HTTPS──► Container App "datatrust-web" (1 replica) ──► PostgreSQL (this service) "datatrust_app"
//                        web app + ADF collector + checks runner     logins, connections, rules, telemetry, incidents
//                        + alerts, all in one process
//                              └──── monitors ────► POC PostgreSQL "datatrust_poc" (other service), Sales DB, ADF ...
//
// Deployed in two passes by infra/datatrust/deploy.ps1:
//   1. deployApps = false -> registry, key vault, identity, PostgreSQL, logs, Container Apps environment
//   2. deployApps = true  -> the web app, after the image is built and the secrets are in Key Vault
// Secret VALUES never pass through this template: Key Vault holds them, the app references them by name.

targetScope = 'resourceGroup'

param location string = resourceGroup().location

@description('Short lowercase name used in every resource name of the DataTrust service.')
@maxLength(12)
param prefix string = 'datatrust'

@description('false on the first pass (infrastructure only), true once the image and secrets exist.')
param deployApps bool = false

@description('Tag of the datatrust-web image in this service\'s registry.')
param imageTag string = 'v1'

@description('Host name of an existing server that holds datatrust_app (deploy.ps1 creates the database and its login there). Empty = create a server here.')
param existingPgHost string = ''

@description('DataTrust\'s login: its own login on an existing server, or the administrator of a server created here.')
param pgAdminUser string = 'datatrustadmin'
@secure()
@description('Its password (deploy.ps1 generates it and keeps it in this service\'s Key Vault as APP-PG-PASSWORD).')
param pgAdminPassword string = ''
param pgVersion string = '17'
param pgSkuName string = 'Standard_B1ms'
param pgSkuTier string = 'Burstable'
param pgStorageGB int = 32

@description('The monitored POC database (another service): host, database and login. Its password is in this service\'s Key Vault as PG-PASSWORD.')
param pocHost string
param pocDatabase string = 'datatrust_poc'
param pocUser string

@description('Object id of the person running the deployment: gets Key Vault Secrets Officer so deploy.ps1 can write the secrets.')
param deployerObjectId string = ''

@description('Your public IP address, allowed through the PostgreSQL firewall for the one-off data copy. Empty = none.')
param clientIp string = ''

@description('Plain (non-secret) settings for the web app: [{ name, value }].')
param webSettings array = []

@description('Names of web app settings whose values are in Key Vault, e.g. ["GEMINI_API_KEY"]. The secret is the name with _ replaced by -.')
param webSecretNames array = []

@description('Resource id of an existing Container Apps environment to run the web app on (this subscription allows only one). Empty = create one here.')
param existingEnvironmentId string = ''
@description('That environment\'s default domain (properties.defaultDomain), for the app\'s public address.')
param existingEnvironmentDomain string = ''

param webCpu string = '1.0'
param webMemory string = '2Gi'

var suffix = uniqueString(resourceGroup().id)
var webName = '${prefix}-web'
var tags = { app: 'datatrust-ai' }
var createEnv = empty(existingEnvironmentId)
var createPgServer = empty(existingPgHost)
var appPgHost = createPgServer ? pg!.properties.fullyQualifiedDomainName : existingPgHost

resource logs 'Microsoft.OperationalInsights/workspaces@2022-10-01' = if (createEnv) {
  name: '${prefix}-logs'
  location: location
  tags: tags
  properties: {
    sku: { name: 'PerGB2018' }
    retentionInDays: 30
  }
}

resource identity 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = {
  name: '${prefix}-id'
  location: location
  tags: tags
}

resource acr 'Microsoft.ContainerRegistry/registries@2023-07-01' = {
  name: toLower('${prefix}acr${suffix}')
  location: location
  tags: tags
  sku: { name: 'Basic' }
  properties: { adminUserEnabled: false }
}

resource acrPull 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(acr.id, identity.id, 'acrpull')
  scope: acr
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', '7f951dda-4ed3-4680-a7ca-43fe172d538d')
    principalId: identity.properties.principalId
    principalType: 'ServicePrincipal'
  }
}

resource kv 'Microsoft.KeyVault/vaults@2023-07-01' = {
  name: '${prefix}-kv-${take(suffix, 8)}'
  location: location
  tags: tags
  properties: {
    tenantId: subscription().tenantId
    sku: { family: 'A', name: 'standard' }
    enableRbacAuthorization: true
    enableSoftDelete: true
    softDeleteRetentionInDays: 7
  }
}

resource kvAppReader 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(kv.id, identity.id, 'secrets-user')
  scope: kv
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', '4633458b-17de-408a-b874-0445c86b69e6')
    principalId: identity.properties.principalId
    principalType: 'ServicePrincipal'
  }
}

resource kvDeployer 'Microsoft.Authorization/roleAssignments@2022-04-01' = if (!empty(deployerObjectId)) {
  name: guid(kv.id, deployerObjectId, 'secrets-officer')
  scope: kv
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', 'b86a8fe4-44ce-4948-aee5-eccb2c155cd7')
    principalId: deployerObjectId
  }
}

resource pg 'Microsoft.DBforPostgreSQL/flexibleServers@2025-08-01' = if (createPgServer) {
  name: '${prefix}-pg-${suffix}'
  location: location
  tags: tags
  sku: { name: pgSkuName, tier: pgSkuTier }
  properties: {
    version: pgVersion
    administratorLogin: pgAdminUser
    administratorLoginPassword: pgAdminPassword
    storage: { storageSizeGB: pgStorageGB, autoGrow: 'Enabled' }
    backup: { backupRetentionDays: 7, geoRedundantBackup: 'Disabled' }
    highAvailability: { mode: 'Disabled' }
    network: { publicNetworkAccess: 'Enabled' }
    authConfig: { activeDirectoryAuth: 'Disabled', passwordAuth: 'Enabled' }
  }
}

// Lets Azure services (the web app) reach the server. A demo setting: for production, use private networking.
resource pgAllowAzure 'Microsoft.DBforPostgreSQL/flexibleServers/firewallRules@2025-08-01' = if (createPgServer) {
  parent: pg
  name: 'AllowAzureServices'
  properties: { startIpAddress: '0.0.0.0', endIpAddress: '0.0.0.0' }
}

resource pgAllowClient 'Microsoft.DBforPostgreSQL/flexibleServers/firewallRules@2025-08-01' = if (createPgServer && !empty(clientIp)) {
  parent: pg
  name: 'DeployerClient'
  properties: { startIpAddress: clientIp, endIpAddress: clientIp }
  dependsOn: [ pgAllowAzure ]
}

resource appDb 'Microsoft.DBforPostgreSQL/flexibleServers/databases@2025-08-01' = if (createPgServer) {
  parent: pg
  name: 'datatrust_app'
  properties: { charset: 'UTF8', collation: 'en_US.utf8' }
  dependsOn: [ pgAllowClient ]
}

resource env 'Microsoft.App/managedEnvironments@2024-03-01' = if (createEnv) {
  name: '${prefix}-env'
  location: location
  tags: tags
  properties: {
    appLogsConfiguration: {
      destination: 'log-analytics'
      logAnalyticsConfiguration: {
        customerId: logs!.properties.customerId
        sharedKey: logs!.listKeys().primarySharedKey
      }
    }
  }
}

var envId = createEnv ? env.id : existingEnvironmentId
var envDomain = createEnv ? env!.properties.defaultDomain : existingEnvironmentDomain
var publicUrl = 'https://${webName}.${envDomain}'

// Settings every deployment sets, whatever .env says.
//   APP_PG_*  DataTrust's own database, on this service's server (password APP-PG-PASSWORD)
//   PG_*      the monitored POC database, on the POC service's server (password PG-PASSWORD, copied from the POC vault)
var fixedWebSettings = [
  { name: 'WEB_HOST', value: '0.0.0.0' }
  { name: 'WEB_PORT', value: '8000' }
  { name: 'APP_PG_HOST', value: appPgHost }
  { name: 'APP_PG_PORT', value: '5432' }
  { name: 'APP_PG_DATABASE', value: 'datatrust_app' }
  { name: 'APP_PG_USER', value: pgAdminUser }
  { name: 'APP_PGSSLMODE', value: 'require' }
  { name: 'PG_HOST', value: pocHost }
  { name: 'PG_PORT', value: '5432' }
  { name: 'PG_DATABASE', value: pocDatabase }
  { name: 'PG_USER', value: pocUser }
  { name: 'PGSSLMODE', value: 'require' }
  { name: 'PORTAL_AUTH_ENABLED', value: 'true' }
  { name: 'PORTAL_COOKIE_SECURE', value: 'true' }
  { name: 'DATATRUST_PUBLIC_URL', value: publicUrl }
  { name: 'AZURE_CLIENT_ID', value: identity.properties.clientId }
]
var allWebSecretNames = union([ 'APP_PG_PASSWORD', 'PG_PASSWORD', 'PORTAL_SECRET' ], webSecretNames)

resource web 'Microsoft.App/containerApps@2024-03-01' = if (deployApps) {
  name: webName
  location: location
  tags: tags
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: { '${identity.id}': {} }
  }
  properties: {
    managedEnvironmentId: envId
    configuration: {
      activeRevisionsMode: 'Single'
      ingress: {
        external: true
        targetPort: 8000
        transport: 'auto'
        allowInsecure: false
      }
      registries: [ { server: acr.properties.loginServer, identity: identity.id } ]
      secrets: [for n in allWebSecretNames: {
        name: toLower(replace(n, '_', '-'))
        keyVaultUrl: '${kv.properties.vaultUri}secrets/${replace(n, '_', '-')}'
        identity: identity.id
      }]
    }
    template: {
      containers: [
        {
          name: 'web'
          image: '${acr.properties.loginServer}/datatrust-web:${imageTag}'
          resources: { cpu: json(webCpu), memory: webMemory }
          env: concat(fixedWebSettings, webSettings, map(allWebSecretNames, n => {
            name: n
            secretRef: toLower(replace(n, '_', '-'))
          }))
          probes: [
            {
              type: 'Startup'
              httpGet: { path: '/health', port: 8000 }
              initialDelaySeconds: 10
              periodSeconds: 10
              failureThreshold: 30
            }
            {
              type: 'Liveness'
              httpGet: { path: '/health', port: 8000 }
              periodSeconds: 30
              failureThreshold: 5
            }
          ]
        }
      ]
      // One replica: the ADF collector, the checks runner and the alert loop run inside the web process.
      scale: { minReplicas: 1, maxReplicas: 1 }
    }
  }
  dependsOn: [ acrPull, kvAppReader, appDb ]
}

output acrName string = acr.name
output keyVaultName string = kv.name
output pgServerName string = createPgServer ? pg.name : ''
output pgHost string = appPgHost
output pgAdminUser string = pgAdminUser
output publicUrl string = publicUrl
output webAppName string = webName
output identityPrincipalId string = identity.properties.principalId
