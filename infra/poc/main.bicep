// The POC data pipeline on Azure, as its own service (its own resource group, registry, vault, identity and job).
// Its database datatrust_poc lives either on a server that already exists (existingPgHost, e.g. the Sales DB server, with
// its own login) or on a server created here. DataTrust AI is deployed separately (infra/datatrust) and only connects
// to datatrust_poc as a monitored source.
//
//   ADLS "<prefix>lake…" landing/raw/<batch>/_READY ──► Event Grid ──► queue ──► Container Apps Job "<prefix>-pipeline"
//        (PySpark ETL) ──► PostgreSQL "datatrust_poc"  bronze / silver / gold / audit
// Deployed in two passes by infra/poc/deploy.ps1:
//   1. deployJob = false -> registry, key vault, identity, PostgreSQL, logs, Container Apps environment
//   2. deployJob = true  -> the scheduled pipeline job, after its image is built and the secrets are in Key Vault
// Secret VALUES never pass through this template: Key Vault holds them, the job references them by name.

targetScope = 'resourceGroup'

param location string = resourceGroup().location

@description('Short lowercase name used in every resource name of the POC service.')
@maxLength(12)
param prefix string = 'dtpoc'

@description('false on the first pass (infrastructure only), true once the image and secrets exist.')
param deployJob bool = false

@description('Tag of the datatrust-pipeline image in this service\'s registry.')
param imageTag string = 'v1'

@description('event = the job starts when a _READY marker is uploaded (Event Grid -> queue); schedule = it runs every jobSchedule.')
@allowed([ 'event', 'schedule' ])
param jobTrigger string = 'event'

@description('schedule mode: how often the job looks for new ADLS batches (cron, UTC). Each run costs about 35 s of 2 vCPU.')
param jobSchedule string = '*/15 * * * *'

@description('event mode: a second, daily run that picks up any batch whose upload event was missed (cron, UTC). Empty = none.')
param safetyNetSchedule string = '0 2 * * *'

@description('Create this service\'s own ADLS Gen2 account for the landing files (needed for the upload trigger). false = use the ADLS account named in jobSettings.')
param ownLandingStorage bool = true
param landingContainer string = 'landing'
param readyQueueName string = 'adls-ready'

@description('Host name of an existing server that holds datatrust_poc (deploy.ps1 creates the database and its login there). Empty = create a server here.')
param existingPgHost string = ''

@description('The login the job uses: the POC login on an existing server, or the administrator of a server created here.')
param pgAdminUser string = 'pocadmin'
@secure()
@description('Administrator password of a server created here (deploy.ps1 generates it and keeps it in this service\'s Key Vault as PG-PASSWORD).')
param pgAdminPassword string = ''
param pgVersion string = '17'
param pgSkuName string = 'Standard_B1ms'
param pgSkuTier string = 'Burstable'
param pgStorageGB int = 32

@description('Object id of the person running the deployment: gets Key Vault Secrets Officer so deploy.ps1 can write the secrets.')
param deployerObjectId string = ''

@description('Your public IP address, allowed through the PostgreSQL firewall for the one-off data copy. Empty = none.')
param clientIp string = ''

@description('Plain settings for the pipeline job: [{ name, value }] (ADLS account, container, folder, WRITE_MODE).')
param jobSettings array = []

@description('Names of job settings whose values are in Key Vault (ADLS_ACCOUNT_KEY / ADLS_CONNECTION_STRING).')
param jobSecretNames array = []

param jobCpu string = '2.0'
param jobMemory string = '4Gi'

var suffix = uniqueString(resourceGroup().id)
var tags = { app: 'datatrust-poc' }
var jobName = '${prefix}-pipeline'
var createPgServer = empty(existingPgHost)

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

resource kvJobReader 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
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

// Lets Azure services reach the server: this service's job, and DataTrust AI (a different service) monitoring it.
// A demo setting: for production, use private networking or allow only DataTrust's outbound IPs.
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

resource pocDb 'Microsoft.DBforPostgreSQL/flexibleServers/databases@2025-08-01' = if (createPgServer) {
  parent: pg
  name: 'datatrust_poc'
  properties: { charset: 'UTF8', collation: 'en_US.utf8' }
  dependsOn: [ pgAllowClient ]
}

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

var pgHost = createPgServer ? pg!.properties.fullyQualifiedDomainName : existingPgHost

// ---------------------------------------------------------------- landing files (this service's own ADLS Gen2 account)
//   upload <batch>/_READY ──► Event Grid (BlobCreated, *_READY only) ──► queue "adls-ready" ──► starts the pipeline job
resource lake 'Microsoft.Storage/storageAccounts@2023-05-01' = if (ownLandingStorage) {
  name: take(toLower('${prefix}lake${suffix}'), 24)
  location: location
  tags: tags
  kind: 'StorageV2'
  sku: { name: 'Standard_LRS' }
  properties: {
    isHnsEnabled: true
    minimumTlsVersion: 'TLS1_2'
    allowBlobPublicAccess: false
    supportsHttpsTrafficOnly: true
    accessTier: 'Hot'
  }
}

resource lakeBlobs 'Microsoft.Storage/storageAccounts/blobServices@2023-05-01' = if (ownLandingStorage) {
  parent: lake
  name: 'default'
}

resource lakeContainer 'Microsoft.Storage/storageAccounts/blobServices/containers@2023-05-01' = if (ownLandingStorage) {
  parent: lakeBlobs
  name: landingContainer
  properties: { publicAccess: 'None' }
}

resource lakeQueues 'Microsoft.Storage/storageAccounts/queueServices@2023-05-01' = if (ownLandingStorage) {
  parent: lake
  name: 'default'
}

resource readyQueue 'Microsoft.Storage/storageAccounts/queueServices/queues@2023-05-01' = if (ownLandingStorage) {
  parent: lakeQueues
  name: readyQueueName
}

resource lakeEvents 'Microsoft.EventGrid/systemTopics@2022-06-15' = if (ownLandingStorage) {
  name: '${prefix}-lake-events'
  location: location
  tags: tags
  properties: {
    source: lake.id
    topicType: 'Microsoft.Storage.StorageAccounts'
  }
}

resource readyToQueue 'Microsoft.EventGrid/systemTopics/eventSubscriptions@2022-06-15' = if (ownLandingStorage) {
  parent: lakeEvents
  name: 'ready-marker-to-queue'
  properties: {
    destination: {
      endpointType: 'StorageQueue'
      properties: {
        resourceId: lake.id
        queueName: readyQueueName
        queueMessageTimeToLiveInSeconds: 604800
      }
    }
    filter: {
      includedEventTypes: [ 'Microsoft.Storage.BlobCreated' ]
      subjectBeginsWith: '/blobServices/default/containers/${landingContainer}/blobs/'
      subjectEndsWith: '/_READY'
    }
    eventDeliverySchema: 'EventGridSchema'
  }
  dependsOn: [ readyQueue ]
}

// The account key stays inside Azure: written straight from the storage account into this service's Key Vault.
resource lakeKeySecret 'Microsoft.KeyVault/vaults/secrets@2023-07-01' = if (ownLandingStorage) {
  parent: kv
  name: 'ADLS-ACCOUNT-KEY'
  properties: { value: lake!.listKeys().keys[0].value }
}

resource lakeQueueSecret 'Microsoft.KeyVault/vaults/secrets@2023-07-01' = if (ownLandingStorage) {
  parent: kv
  name: 'ADLS-QUEUE-CONNECTION'
  properties: {
    value: 'DefaultEndpointsProtocol=https;AccountName=${lake.name};AccountKey=${lake!.listKeys().keys[0].value};EndpointSuffix=${environment().suffixes.storage}'
  }
}

// ---------------------------------------------------------------- the pipeline job
var useEvents = ownLandingStorage && jobTrigger == 'event'
var allJobSecretNames = union([ 'PG_PASSWORD' ], ownLandingStorage ? [ 'ADLS_ACCOUNT_KEY', 'ADLS_QUEUE_CONNECTION' ] : [], jobSecretNames)
var lakeSettings = ownLandingStorage ? [
  { name: 'ADLS_ACCOUNT_NAME', value: lake.name }
  { name: 'ADLS_CONTAINER', value: landingContainer }
  { name: 'ADLS_QUEUE_NAME', value: readyQueueName }
] : []

var jobSecrets = [for n in allJobSecretNames: {
  name: toLower(replace(n, '_', '-'))
  keyVaultUrl: 'https://${kv.name}${environment().suffixes.keyvaultDns}/secrets/${replace(n, '_', '-')}'
  identity: identity.id
}]

var jobTemplate = {
  containers: [
    {
      name: 'pipeline'
      image: '${acr.properties.loginServer}/datatrust-pipeline:${imageTag}'
      command: [ 'python', 'adls_pipeline_worker.py', '--once' ]
      resources: { cpu: json(jobCpu), memory: jobMemory }
      env: concat([
        { name: 'SOURCE_MODE', value: 'adls' }
        { name: 'PG_HOST', value: pgHost }
        { name: 'PG_PORT', value: '5432' }
        { name: 'PG_DATABASE', value: 'datatrust_poc' }
        { name: 'PG_USER', value: pgAdminUser }
        { name: 'PGSSLMODE', value: 'require' }
        { name: 'AZURE_CLIENT_ID', value: identity.properties.clientId }
      ], lakeSettings, jobSettings, map(allJobSecretNames, n => {
        name: n
        secretRef: toLower(replace(n, '_', '-'))
      }))
    }
  ]
}

// A job, not an always-on app: billed only while a run is going. In event mode Azure watches the queue (free) and starts
// a run only when a _READY marker has been uploaded. One run at a time; a batch that a slow run still holds is skipped by
// any other run (batch_status.claim_batch).
resource job 'Microsoft.App/jobs@2024-03-01' = if (deployJob) {
  name: jobName
  location: location
  tags: tags
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: { '${identity.id}': {} }
  }
  properties: {
    environmentId: env.id
    configuration: union({
      triggerType: useEvents ? 'Event' : 'Schedule'
      replicaTimeout: 3600
      replicaRetryLimit: 0
      registries: [ { server: acr.properties.loginServer, identity: identity.id } ]
      secrets: jobSecrets
    }, useEvents ? {
      eventTriggerConfig: {
        parallelism: 1
        replicaCompletionCount: 1
        scale: {
          minExecutions: 0
          maxExecutions: 1
          pollingInterval: 30
          rules: [
            {
              name: 'ready-marker'
              type: 'azure-queue'
              metadata: { accountName: lake.name, queueName: readyQueueName, queueLength: '1' }
              auth: [ { secretRef: 'adls-queue-connection', triggerParameter: 'connection' } ]
            }
          ]
        }
      }
    } : {
      scheduleTriggerConfig: { cronExpression: jobSchedule, parallelism: 1, replicaCompletionCount: 1 }
    })
    template: jobTemplate
  }
  dependsOn: [ acrPull, kvJobReader, pocDb, lakeKeySecret, lakeQueueSecret, readyToQueue ]
}

// Event mode only: a daily run in case an upload event was ever lost. With nothing new it ends in about 35 seconds.
resource safetyNet 'Microsoft.App/jobs@2024-03-01' = if (deployJob && useEvents && !empty(safetyNetSchedule)) {
  name: '${jobName}-daily'
  location: location
  tags: tags
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: { '${identity.id}': {} }
  }
  properties: {
    environmentId: env.id
    configuration: {
      triggerType: 'Schedule'
      replicaTimeout: 3600
      replicaRetryLimit: 0
      scheduleTriggerConfig: { cronExpression: safetyNetSchedule, parallelism: 1, replicaCompletionCount: 1 }
      registries: [ { server: acr.properties.loginServer, identity: identity.id } ]
      secrets: jobSecrets
    }
    template: jobTemplate
  }
  dependsOn: [ acrPull, kvJobReader, pocDb, lakeKeySecret, lakeQueueSecret ]
}

output acrName string = acr.name
output keyVaultName string = kv.name
output pgServerName string = createPgServer ? pg.name : ''
output pgHost string = pgHost
output pgAdminUser string = pgAdminUser
output pgDatabase string = 'datatrust_poc'
output identityPrincipalId string = identity.properties.principalId
output jobName string = jobName
output landingAccount string = ownLandingStorage ? lake.name : ''
output landingContainer string = landingContainer
