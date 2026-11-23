<#
Deploys the POC data pipeline to Azure as its own service. DataTrust AI is a separate service (infra/datatrust/deploy.ps1),
deployed after this one; it only reads (and, when an admin approves, fixes) this service's database as a monitored source.

    az login
    .\infra\poc\deploy.ps1                    # first time: creates everything, copies the local datatrust_poc once
    .\infra\poc\deploy.ps1 -SkipDataCopy      # later: new pipeline image + settings, data untouched

What it creates, in resource group rg-datatrust-poc (only for this service):
  a container registry, a key vault, a managed identity, a Log Analytics workspace, a Container Apps environment, the
  service's own ADLS Gen2 account for the landing files, and the pipeline as a Container Apps Job (ADLS batches -> PySpark
  ETL -> bronze / silver / gold), billed only while it runs.

When the pipeline runs
  By default (-Trigger event) it starts when a batch's _READY marker is uploaded to the landing account: Event Grid puts a
  message in a queue, and Azure starts the job within about a minute. A second job runs once a day (-SafetyNet) in case an
  upload event was ever lost. With -Trigger schedule it runs every -Schedule instead.
  The landing files of the ADLS account in .env are copied into the new account once (files already there are skipped).
  With -UseEnvAdls the job reads the ADLS account in .env instead (no upload trigger: it must run on a schedule).

Where the database goes
  By default, on the PostgreSQL server that already holds the Sales DB (-SharedServer pg-sales-hv): the script creates the
  database datatrust_poc there with its own login (-PocLogin poc_pipeline), which owns it and is what the job uses. It
  signs in once as that server's admin to do so, with the password in .env (-SharedServerAdminPasswordEnv). No other
  database on that server is touched. With -NewServer it creates a separate PostgreSQL server for the POC instead.

Options
  -SharedServer, -SharedServerResourceGroup   the existing server (default pg-sales-hv in datatrust_ai)
  -NewServer                                  create a PostgreSQL server for the POC instead (size: -PgSku)
  -Trigger event|schedule   start the job on upload (default) or on a timetable
  -SafetyNet "0 2 * * *"    event mode: the daily catch-up run (cron, UTC); "" = none
  -Schedule "*/15 * * * *"  schedule mode: how often the job looks for new ADLS batches (cron, UTC)
  -UseEnvAdls               read the ADLS account in .env instead of creating one (implies -Trigger schedule)
  -SkipLandingCopy          do not copy the landing files from the .env account
  -NoPipelineJob            only the database (no ADLS pipeline job)
  -SkipDataCopy             do not copy the local database (use on every deployment after the first)
  -Yes                      do not ask before creating resources
#>
param(
    [string]$ResourceGroup = "rg-datatrust-poc",
    [string]$Location = "southeastasia",
    [string]$SharedServer = "pg-sales-hv",
    [string]$SharedServerResourceGroup = "datatrust_ai",
    [string]$SharedServerAdminPasswordEnv = "CONN_SALESDB_PASSWORD",
    [ValidatePattern('^[a-z_][a-z0-9_]{2,40}$')][string]$PocLogin = "poc_pipeline",
    [switch]$NewServer,
    [string]$PgSku = "Standard_B2s",
    [ValidatePattern('^[a-z][a-z0-9]{2,11}$')][string]$Prefix = "dtpoc",
    [string]$ImageTag = ("v" + (Get-Date -Format "yyyyMMdd-HHmm")),
    [ValidateSet("event", "schedule")][string]$Trigger = "event",
    [string]$SafetyNet = "0 2 * * *",
    [string]$Schedule = "*/15 * * * *",
    [switch]$UseEnvAdls,
    [switch]$SkipLandingCopy,
    [switch]$NoPipelineJob,
    [switch]$SkipDataCopy,
    [switch]$Yes
)
$ErrorActionPreference = "Stop"
$Root = Resolve-Path (Join-Path $PSScriptRoot "..\..")
. (Join-Path $PSScriptRoot "..\common.ps1")
$env:PYTHONIOENCODING = "utf-8"   # az acr build streams logs with non-ASCII characters
$AppTag = "datatrust-poc"

# DataTrust AI's own tables that an older version kept in the POC database. They belong to the other service: never copied here.
$DataTrustSchemas = @("portal", "telemetry", "app", "monitor")
$DataTrustTables = @("audit.remediation_log", "audit.alert_log")

# ------------------------------------------------------------------ 0. checks
Step "POC data pipeline: checking tools and sign-in"
$account = Assert-AzReady
$envFile = Join-Path $Root ".env"
if (-not (Test-Path $envFile)) { throw ".env not found at $envFile" }
$dot = Read-DotEnv $envFile

if ($UseEnvAdls) { $Trigger = "schedule" }
$landingContainer = Get-Setting $dot "ADLS_CONTAINER" "landing"
$jobSettings = @(); $jobSecrets = [ordered]@{}
if (-not $NoPipelineJob -and -not $UseEnvAdls) {
    # This service's own landing account: its name, container and key come from the template, not from .env.
    $jobSettings += @{ name = "ADLS_FOLDER"; value = (Get-Setting $dot "ADLS_FOLDER" "raw") }
    $jobSettings += @{ name = "WRITE_MODE"; value = (Get-Setting $dot "WRITE_MODE" "python") }
}
elseif (-not $NoPipelineJob) {
    foreach ($k in @("ADLS_ACCOUNT_NAME", "ADLS_CONTAINER", "ADLS_FOLDER")) { if ($dot[$k]) { $jobSettings += @{ name = $k; value = $dot[$k] } } }
    $jobSettings += @{ name = "WRITE_MODE"; value = (Get-Setting $dot "WRITE_MODE" "python") }
    if ($dot["ADLS_CONNECTION_STRING"] -and $dot["ADLS_CONNECTION_STRING"] -notmatch 'UseDevelopmentStorage') { $jobSecrets["ADLS_CONNECTION_STRING"] = $dot["ADLS_CONNECTION_STRING"] }
    if ($dot["ADLS_ACCOUNT_KEY"]) { $jobSecrets["ADLS_ACCOUNT_KEY"] = $dot["ADLS_ACCOUNT_KEY"] }
    if (-not $jobSecrets.Count -and -not $dot["ADLS_ACCOUNT_NAME"]) {
        throw "The pipeline job needs ADLS: set ADLS_ACCOUNT_NAME (and ADLS_ACCOUNT_KEY or ADLS_CONNECTION_STRING) in .env, or pass -NoPipelineJob."
    }
}
$shared = $null
if (-not $NewServer) {
    $shared = Get-SharedServer $SharedServerResourceGroup $SharedServer
    $sharedAdminPassword = if ($env:SHARED_PG_ADMIN_PASSWORD) { $env:SHARED_PG_ADMIN_PASSWORD } else { $dot[$SharedServerAdminPasswordEnv] }
    if (-not $sharedAdminPassword) { throw "The admin password of $SharedServer is needed once, to create the POC database and login: set $SharedServerAdminPasswordEnv in .env or `$env:SHARED_PG_ADMIN_PASSWORD." }
}

$adlsAuth = if (-not $UseEnvAdls) { "account key (Key Vault)" } elseif ($jobSecrets.Contains("ADLS_CONNECTION_STRING")) { "connection string (Key Vault)" } elseif ($jobSecrets.Contains("ADLS_ACCOUNT_KEY")) { "account key (Key Vault)" } else { "the job's managed identity (Storage Blob Data Contributor)" }
$when = if ($Trigger -eq "event") { "when a _READY marker is uploaded$(if ($SafetyNet) { ", plus a daily catch-up run at '$SafetyNet' (UTC)" })" } else { "every '$Schedule' (UTC)" }
$where = if ($UseEnvAdls) { "ADLS $($dot['ADLS_ACCOUNT_NAME'])/$landingContainer/$(Get-Setting $dot 'ADLS_FOLDER' 'raw')" } else { "this service's own ADLS account, $landingContainer/$(Get-Setting $dot 'ADLS_FOLDER' 'raw')" }

Write-Host "Service      : POC data pipeline (separate from DataTrust AI)"
Write-Host "Subscription : $($account.name) ($($account.id))"
Write-Host "Resource grp : $ResourceGroup in $Location"
Write-Host "Data copy    : $(if ($SkipDataCopy) { 'no' } else { 'yes, local datatrust_poc -> Azure (replaces what is there; DataTrust AI''s own tables are left out)' })"
if ($NoPipelineJob) { Write-Host "Pipeline job : no" }
else { Write-Host "Pipeline job : runs $when; reads $where; sign-in: $adlsAuth" }
if ($shared) { Write-Host "Database     : datatrust_poc on the existing server $SharedServer ($($shared.host), PostgreSQL $($shared.version)), login $PocLogin; other databases there are not touched" }
else { Write-Host "Database     : datatrust_poc on a new PostgreSQL Flexible Server ($PgSku)" }
Write-Host "Creates (if missing): Container Registry Basic, Key Vault, Log Analytics, Container Apps environment$(if (-not $UseEnvAdls) {', ADLS Gen2 account + queue + Event Grid'})$(if (-not $NoPipelineJob) {', pipeline job (2 vCPU, 4 GB while running)'})$(if (-not $shared) {', the PostgreSQL server'})."
Write-Host "These resources cost money while they exist. Remove this service later with: az group delete -n $ResourceGroup" -ForegroundColor Yellow
Confirm-Plan -Yes:$Yes

# ------------------------------------------------------------------ 1. infrastructure
Step "Resource group"
Invoke-Az group create --name $ResourceGroup --location $Location --tags app=$AppTag --output none

$pgPassword = Get-KvSecret (Find-Vault $ResourceGroup $AppTag) "PG_PASSWORD"      # keep the password of an earlier run
if (-not $pgPassword) { $pgPassword = New-Password }
$me = (Invoke-Az ad signed-in-user show --query id --output tsv)
$clientIp = if ($SkipDataCopy -and -not $shared) { "" } else { Get-PublicIp }
$template = Join-Path $PSScriptRoot "main.bicep"
$params = @{
    location = $Location; prefix = $Prefix; imageTag = $ImageTag; pgSkuName = $PgSku; jobSchedule = $Schedule
    jobTrigger = $Trigger; safetyNetSchedule = $SafetyNet; ownLandingStorage = (-not $UseEnvAdls); landingContainer = $landingContainer
    deployerObjectId = $me; clientIp = $clientIp
    jobSettings = @($jobSettings); jobSecretNames = @($jobSecrets.Keys)
}
$secureArgs = @("pgAdminPassword=$pgPassword")
if ($shared) { $params.existingPgHost = $shared.host; $params.pgAdminUser = $PocLogin; $secureArgs = @() }

Step "Infrastructure (registry, key vault, Container Apps environment$(if (-not $shared) {', PostgreSQL'})): a few minutes the first time"
$params.deployJob = $false
$o = Invoke-BicepDeployment $ResourceGroup "dtpoc-infra" $template $params $secureArgs
$acr = $o.acrName.value; $kv = $o.keyVaultName.value; $pgHost = $o.pgHost.value; $pgUser = $o.pgAdminUser.value
$lake = $o.landingAccount.value
Write-Host "Registry $acr, Key Vault $kv, PostgreSQL $pgHost$(if ($lake) { ", landing account $lake" })"

if ($shared) {
    Step "Database datatrust_poc and login $PocLogin on $SharedServer"
    Initialize-SharedDatabase $shared $SharedServer $SharedServerResourceGroup $sharedAdminPassword "datatrust_poc" $PocLogin $pgPassword $clientIp "datatrust-poc-deployer"
}

# ------------------------------------------------------------------ 2. secrets
Step "Secrets into this service's Key Vault"
Set-KvSecret $kv "PG_PASSWORD" $pgPassword
foreach ($k in $jobSecrets.Keys) { Set-KvSecret $kv $k $jobSecrets[$k]; Write-Host "   $k" }

if (-not $NoPipelineJob -and $UseEnvAdls -and -not $jobSecrets.Count) {
    Step "Letting the job's identity read the ADLS account $($dot['ADLS_ACCOUNT_NAME'])"
    $storageId = Get-AzQuiet storage account show --name $dot["ADLS_ACCOUNT_NAME"] --query id --output tsv
    $ok = $false
    if ($storageId) {
        $null = Get-AzQuiet role assignment create --assignee-object-id $o.identityPrincipalId.value --assignee-principal-type ServicePrincipal `
                    --role "Storage Blob Data Contributor" --scope $storageId --output none
        $ok = ($LASTEXITCODE -eq 0)
    }
    if (-not $ok) { Write-Host "Could not grant it (account not in this subscription, or no rights). Ask a storage admin to give identity '$Prefix-id' the role Storage Blob Data Contributor on that account, or put ADLS_ACCOUNT_KEY in .env and run again." -ForegroundColor Yellow }
}

# ------------------------------------------------------------------ 3. image
if (-not $NoPipelineJob) {
    Step "Building the pipeline image in this service's registry ($ImageTag)"
    Push-Location $Root
    try { Publish-Image $acr "datatrust-pipeline:$ImageTag" "Dockerfile.pipeline" "." }
    finally { Pop-Location }
}

# ------------------------------------------------------------------ 4. data
if (-not $SkipDataCopy) {
    Step "Copying the local POC database into Azure"
    $env:AZ_PG_PASSWORD = $pgPassword
    try {
        & (Join-Path $PSScriptRoot "..\copy-database.ps1") -LocalPrefix PG -LocalDefaultDb datatrust_poc -AzureHost $pgHost -AzureUser $pgUser `
            -AzureDatabase datatrust_poc -ExcludeSchemas $DataTrustSchemas -ExcludeTables $DataTrustTables -EnvFile $envFile
    }
    finally { Remove-Item Env:AZ_PG_PASSWORD -ErrorAction SilentlyContinue }
}

if ($lake -and -not $SkipLandingCopy -and $dot["ADLS_ACCOUNT_NAME"] -and $dot["ADLS_ACCOUNT_NAME"] -ne $lake) {
    Step "Copying the landing files from $($dot['ADLS_ACCOUNT_NAME']) into $lake (files already there are skipped)"
    $env:LANDING_DEST_KEY = Get-KvSecret $kv "ADLS_ACCOUNT_KEY"
    try {
        Push-Location $Root
        & { $ErrorActionPreference = "Continue"; & python (Join-Path $PSScriptRoot "..\copy-landing.py") $lake --container $landingContainer 2>&1 | ForEach-Object { "$_" } | Out-Host }
        if ($LASTEXITCODE -ne 0) { throw "Copying the landing files failed (exit $LASTEXITCODE)." }
    }
    finally { Pop-Location; Remove-Item Env:LANDING_DEST_KEY -ErrorAction SilentlyContinue }
}

# ------------------------------------------------------------------ 5. job
if (-not $NoPipelineJob) {
    Step "Pipeline job"
    $params.deployJob = $true
    $o = Invoke-BicepDeployment $ResourceGroup "dtpoc-job" $template $params $secureArgs
}

Write-Host ""
Write-Host "POC data pipeline is deployed." -ForegroundColor Green
Write-Host "   Database : $pgHost / datatrust_poc (login $pgUser, password in Key Vault $kv as PG-PASSWORD)"
if ($shared -and $clientIp) { Write-Host "   The firewall rule datatrust-poc-deployer on $SharedServer can be removed when you no longer copy data from this computer." }
if (-not $NoPipelineJob) {
    Write-Host "   Job      : $($o.jobName.value), runs $when"
    if ($lake) { Write-Host "   Landing  : upload batches to $lake / $landingContainer / $(Get-Setting $dot 'ADLS_FOLDER' 'raw')/<full or YYYY-MM-DD>/, then the _READY file last" }
    Write-Host "   Run now  : az containerapp job start -n $($o.jobName.value) -g $ResourceGroup"
    Write-Host "   Runs     : az containerapp job execution list -n $($o.jobName.value) -g $ResourceGroup -o table"
}
Write-Host "Next: deploy DataTrust AI, which will monitor this database:  .\infra\datatrust\deploy.ps1"
Write-Host "Remove this service: az group delete -n $ResourceGroup"
