// DataTrust AI on Azure: everything the web app (and, optionally, the ADLS pipeline worker) needs.
// Deployed in two passes by infra/deploy.ps1:
//   1. deployApps = false  -> registry, key vault, identity, PostgreSQL, logs, Container Apps environment
//   2. deployApps = true   -> the container app(s), after the image is built and the secrets are in Key Vault
// Secret VALUES never pass through this template: Key Vault holds them, the apps reference them by name.

targetScope = 'resourceGroup'

@description('Azure region. Defaults to the resource group\'s region.')
param location string = resourceGroup().location

@description('Short lowercase name used in every resource name.')
@maxLength(12)
param prefix string = 'datatrust'

@description('false on the first pass (infrastructure only), true once the image and secrets exist.')
param deployApps bool = false

@description('Tag of the datatrust-web image in the registry.')
param imageTag string = 'v1'

@description('PostgreSQL administrator login.')
param pgAdminUser string = 'datatrustadmin'

@secure()
@description('PostgreSQL administrator password (deploy.ps1 generates it and keeps it in Key Vault as PG-PASSWORD).')
param pgAdminPassword string

@description('PostgreSQL major version. The local databases are PostgreSQL 17.')
param pgVersion string = '17'

@description('PostgreSQL compute size. Standard_B1ms (Burstable) is the cheapest; use Standard_B2s or GeneralPurpose for more load.')
param pgSkuName string = 'Standard_B1ms'
param pgSkuTier string = 'Burstable'
param pgStorageGB int = 32

@description('Object id of the person running the deployment: gets Key Vault Secrets Officer so deploy.ps1 can write the secrets.')
param deployerObjectId string = ''

@description('Your public IP address, allowed through the PostgreSQL firewall for the one-off data copy. Empty = none.')
param clientIp string = ''

@description('Plain (non-secret) settings for the web app: [{ name, value }].')
param webSettings array = []

@description('Names of web app settings whose values are in Key Vault, e.g. ["GEMINI_API_KEY"]. The Key Vault secret is the name with _ replaced by -.')
param webSecretNames array = []

@description('Also run the ADLS pipeline worker (PySpark ETL) as a second container app.')
param deployWorker bool = false
param workerImageTag string = 'v1'
param workerSettings array = []
param workerSecretNames array = []

param webCpu string = '1.0'
param webMemory string = '2Gi'

var suffix = uniqueString(resourceGroup().id)
var acrName = toLower('${prefix}acr${suffix}')
var kvName = '${prefix}-kv-${take(suffix, 8)}'
var pgName = '${prefix}-pg-${suffix}'
var webName = '${prefix}-web'
var workerName = '${prefix}-pipeline'
var tags = { app: 'datatrust-ai' }

// --------------------------------------------------------------------------------------------- shared
resource logs 'Microsoft.OperationalInsights/workspaces@2022-10-01' = {
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
  name: acrName
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
  name: kvName
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

// --------------------------------------------------------------------------------------------- PostgreSQL
resource pg 'Microsoft.DBforPostgreSQL/flexibleServers@2025-08-01' = {
  name: pgName
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

// Lets Azure services (the container apps) reach the server. A demo setting: for production, use private networking.
resource pgAllowAzure 'Microsoft.DBforPostgreSQL/flexibleServers/firewallRules@2025-08-01' = {
  parent: pg
  name: 'AllowAzureServices'
  properties: { startIpAddress: '0.0.0.0', endIpAddress: '0.0.0.0' }
}

resource pgAllowClient 'Microsoft.DBforPostgreSQL/flexibleServers/firewallRules@2025-08-01' = if (!empty(clientIp)) {
  parent: pg
  name: 'DeployerClient'
  properties: { startIpAddress: clientIp, endIpAddress: clientIp }
  dependsOn: [ pgAllowAzure ]
}

resource pocDb 'Microsoft.DBforPostgreSQL/flexibleServers/databases@2025-08-01' = {
  parent: pg
  name: 'datatrust_poc'
  properties: { charset: 'UTF8', collation: 'en_US.utf8' }
  dependsOn: [ pgAllowClient ]
}

resource appDb 'Microsoft.DBforPostgreSQL/flexibleServers/databases@2025-08-01' = {
  parent: pg
  name: 'datatrust_app'
  properties: { charset: 'UTF8', collation: 'en_US.utf8' }
  dependsOn: [ pocDb ]
}

// --------------------------------------------------------------------------------------------- Container Apps
resource env 'Microsoft.App/managedEnvironments@2024-03-01' = {
  name: '${prefix}-env'
  location: location
  tags: tags
  properties: {
    appLogsConfiguration: {
      destination: 'log-analytics'
      logAnalyticsConfiguration: {
        customerId: logs.properties.customerId
        sharedKey: logs.listKeys().primarySharedKey
      }
    }
  }
}

var publicUrl = 'https://${webName}.${env.properties.defaultDomain}'

// Settings every deployment sets, whatever .env says. PG_PASSWORD is the Azure admin password (Key Vault).
var fixedWebSettings = [
  { name: 'WEB_HOST', value: '0.0.0.0' }
  { name: 'WEB_PORT', value: '8000' }
  { name: 'PG_HOST', value: pg.properties.fullyQualifiedDomainName }
  { name: 'PG_PORT', value: '5432' }
  { name: 'PG_DATABASE', value: 'datatrust_poc' }
  { name: 'PG_USER', value: pgAdminUser }
  { name: 'PGSSLMODE', value: 'require' }
  { name: 'APP_PG_DATABASE', value: 'datatrust_app' }
  { name: 'PORTAL_AUTH_ENABLED', value: 'true' }
  { name: 'PORTAL_COOKIE_SECURE', value: 'true' }
  { name: 'DATATRUST_PUBLIC_URL', value: publicUrl }
  { name: 'AZURE_CLIENT_ID', value: identity.properties.clientId }
]
var allWebSecretNames = union([ 'PG_PASSWORD', 'PORTAL_SECRET' ], webSecretNames)

resource web 'Microsoft.App/containerApps@2024-03-01' = if (deployApps) {
  name: webName
  location: location
  tags: tags
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: { '${identity.id}': {} }
  }
  properties: {
    managedEnvironmentId: env.id
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

resource worker 'Microsoft.App/containerApps@2024-03-01' = if (deployApps && deployWorker) {
  name: workerName
  location: location
  tags: tags
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: { '${identity.id}': {} }
  }
  properties: {
    managedEnvironmentId: env.id
    configuration: {
      activeRevisionsMode: 'Single'
      registries: [ { server: acr.properties.loginServer, identity: identity.id } ]
      secrets: [for n in union([ 'PG_PASSWORD' ], workerSecretNames): {
        name: toLower(replace(n, '_', '-'))
        keyVaultUrl: '${kv.properties.vaultUri}secrets/${replace(n, '_', '-')}'
        identity: identity.id
      }]
    }
    template: {
      containers: [
        {
          name: 'pipeline'
          image: '${acr.properties.loginServer}/datatrust-pipeline:${workerImageTag}'
          resources: { cpu: json('2.0'), memory: '4Gi' }
          env: concat([
            { name: 'SOURCE_MODE', value: 'adls' }
            { name: 'PG_HOST', value: pg.properties.fullyQualifiedDomainName }
            { name: 'PG_PORT', value: '5432' }
            { name: 'PG_DATABASE', value: 'datatrust_poc' }
            { name: 'PG_USER', value: pgAdminUser }
            { name: 'PGSSLMODE', value: 'require' }
            { name: 'WRITE_MODE', value: 'python' }
            { name: 'AZURE_CLIENT_ID', value: identity.properties.clientId }
          ], workerSettings, map(union([ 'PG_PASSWORD' ], workerSecretNames), n => {
            name: n
            secretRef: toLower(replace(n, '_', '-'))
          }))
        }
      ]
      // Exactly one: two workers could pick up the same ready batch.
      scale: { minReplicas: 1, maxReplicas: 1 }
    }
  }
  dependsOn: [ acrPull, kvAppReader, appDb ]
}

output acrName string = acr.name
output acrLoginServer string = acr.properties.loginServer
output keyVaultName string = kv.name
output pgServerName string = pg.name
output pgHost string = pg.properties.fullyQualifiedDomainName
output pgAdminUser string = pgAdminUser
output identityPrincipalId string = identity.properties.principalId
output publicUrl string = publicUrl
output webAppName string = webName
