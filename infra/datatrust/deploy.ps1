<#
Deploys DataTrust AI to Azure as its own service. The POC data pipeline is a different service (infra/poc/deploy.ps1):
deploy that first; DataTrust then monitors its database like any other source.

    az login
    .\infra\poc\deploy.ps1                         # the POC data pipeline (once, before DataTrust)
    .\infra\datatrust\deploy.ps1                   # first time: creates everything, copies the local datatrust_app once
    .\infra\datatrust\deploy.ps1 -SkipDataCopy     # later: new image + settings, data untouched

What it creates, in resource group rg-datatrust (only for this service):
  PostgreSQL Flexible Server 17 with DataTrust's own database datatrust_app, a container registry, a key vault,
  a managed identity, a Log Analytics workspace, a Container Apps environment and the web app (exactly 1 replica).

The monitored POC database is found in the POC service's resource group (-PocResourceGroup): its host and login come from
that deployment and its password from that service's Key Vault, copied into this service's Key Vault as PG-PASSWORD.
For a POC database somewhere else, pass -PocHost and -PocUser, with its password in $env:POC_PG_PASSWORD.

Database
  By default DataTrust's own database datatrust_app goes on the existing server pg-sales-hv (-SharedServer), with its own
  login (-AppLogin datatrust_owner) that owns it; no other login may connect to it. The script signs in once as that
  server's admin (password in .env, -SharedServerAdminPasswordEnv) to create them. -NewServer creates a server instead.

Hosting
  This subscription allows only one Container Apps environment, so by default the web app runs on the POC service's
  environment (-SharedEnvironment dtpoc-env in -PocResourceGroup) and in its region. Everything else is DataTrust's own:
  resource group, PostgreSQL server, key vault, registry, identity and the app. -OwnEnvironment creates its own instead.

Options
  -EnableChatAlerts     also send Google Chat alerts and the daily briefing from Azure (off by default, so the
                        cloud copy and your local server do not both post to the same space)
  -WithSqlServerDriver  build the web image with the Microsoft ODBC driver (SQL Server sources)
  -SkipDataCopy         do not copy the local database (use on every deployment after the first)
  -Yes                  do not ask before creating resources
#>
param(
    [string]$ResourceGroup = "rg-datatrust",
    [string]$Location = "southeastasia",
    [string]$SharedEnvironment = "dtpoc-env",
    [string]$SharedServer = "pg-sales-hv",
    [string]$SharedServerResourceGroup = "datatrust_ai",
    [string]$SharedServerAdminPasswordEnv = "CONN_SALESDB_PASSWORD",
    [ValidatePattern('^[a-z_][a-z0-9_]{2,40}$')][string]$AppLogin = "datatrust_owner",
    [switch]$NewServer,
    [switch]$OwnEnvironment,
    [string]$PgSku = "Standard_B2s",
    [ValidatePattern('^[a-z][a-z0-9]{2,11}$')][string]$Prefix = "datatrust",
    [string]$ImageTag = ("v" + (Get-Date -Format "yyyyMMdd-HHmm")),
    [string]$PocResourceGroup = "rg-datatrust-poc",
    [string]$PocHost = "",
    [string]$PocUser = "",
    [string]$PocDatabase = "datatrust_poc",
    [switch]$EnableChatAlerts,
    [switch]$WithSqlServerDriver,
    [switch]$SkipDataCopy,
    [switch]$Yes
)
$ErrorActionPreference = "Stop"
$Root = Resolve-Path (Join-Path $PSScriptRoot "..\..")
. (Join-Path $PSScriptRoot "..\common.ps1")
$env:PYTHONIOENCODING = "utf-8"   # az acr build streams logs with non-ASCII characters
$AppTag = "datatrust-ai"

# ------------------------------------------------------------------ 0. checks
Step "DataTrust AI: checking tools and sign-in"
$account = Assert-AzReady
$envFile = Join-Path $Root ".env"
if (-not (Test-Path $envFile)) { throw ".env not found at $envFile" }
$dot = Read-DotEnv $envFile

Step "Finding the monitored POC database"
$pocPassword = $env:POC_PG_PASSWORD
if (-not $PocHost) {
    $pocOut = (Get-AzQuiet deployment group show --resource-group $PocResourceGroup --name dtpoc-infra --query properties.outputs --output json) | Out-String
    if (-not $pocOut.Trim()) {
        throw "No POC service found in resource group $PocResourceGroup. Deploy it first (.\infra\poc\deploy.ps1), or pass -PocHost and -PocUser with `$env:POC_PG_PASSWORD."
    }
    $pocOut = $pocOut | ConvertFrom-Json
    $PocHost = $pocOut.pgHost.value; $PocUser = $pocOut.pgAdminUser.value; $PocDatabase = $pocOut.pgDatabase.value
    if (-not $pocPassword) { $pocPassword = Get-KvSecret $pocOut.keyVaultName.value "PG_PASSWORD" }
}
if (-not $PocUser) { throw "Pass -PocUser with -PocHost." }
if (-not $pocPassword) { throw "Could not read the POC database password (Key Vault of $PocResourceGroup). Set `$env:POC_PG_PASSWORD and run again." }
Write-Host "POC database : $PocHost / $PocDatabase (login $PocUser)"

# "Retry failed batch" in DataTrust starts the POC pipeline: when the POC job starts on uploads (a queue), DataTrust may add a
# message to that one queue (role Storage Queue Data Message Sender for its identity; no key). Otherwise the retry waits
# for the job's next scheduled run.
$pocQueue = $null
$trigger = Get-AzQuiet containerapp job show --resource-group $PocResourceGroup --name dtpoc-pipeline --query properties.configuration.triggerType --output tsv
if ($trigger -eq "Event") {
    $lakeJson = (Get-AzQuiet storage account list --resource-group $PocResourceGroup --query "[?tags.app=='datatrust-poc'] | [0].{id:id, queue:primaryEndpoints.queue}" --output json) | Out-String
    if ($lakeJson.Trim() -and $lakeJson.Trim() -ne "null") {
        $lakeInfo = $lakeJson | ConvertFrom-Json
        $pocQueue = @{ url = "$($lakeInfo.queue)adls-ready"; scope = "$($lakeInfo.id)/queueServices/default/queues/adls-ready" }
        Write-Host "POC pipeline : starts on upload; 'Retry failed batch' will start it through $($pocQueue.url)"
    }
}

$sharedEnv = $null
if (-not $OwnEnvironment) {
    $e = (Get-AzQuiet containerapp env show --resource-group $PocResourceGroup --name $SharedEnvironment `
            --query "{id:id, domain:properties.defaultDomain, location:location}" --output json) | Out-String
    if (-not $e.Trim()) { throw "Container Apps environment $SharedEnvironment not found in $PocResourceGroup. Deploy the POC first, or pass -OwnEnvironment." }
    $sharedEnv = $e | ConvertFrom-Json
    $Location = ($sharedEnv.location -replace '\s', '').ToLower()       # an app must be in its environment's region
    Write-Host "Hosting      : the existing Container Apps environment $SharedEnvironment ($Location)"
}

$shared = $null
if (-not $NewServer) {
    $shared = Get-SharedServer $SharedServerResourceGroup $SharedServer
    $sharedAdminPassword = if ($env:SHARED_PG_ADMIN_PASSWORD) { $env:SHARED_PG_ADMIN_PASSWORD } else { $dot[$SharedServerAdminPasswordEnv] }
    if (-not $sharedAdminPassword) { throw "The admin password of $SharedServer is needed once, to create DataTrust's database and login: set $SharedServerAdminPasswordEnv in .env or `$env:SHARED_PG_ADMIN_PASSWORD." }
    Write-Host "Database     : datatrust_app on the existing server $SharedServer ($($shared.host)), login $AppLogin; other databases there are not touched"
}

# Never copied: local-only values (databases, ports, Kafka, Ollama on localhost), the POC pipeline's settings, values set below.
$skip = '^(PG_|APP_PG|PGSSLMODE$|APP_PGSSLMODE$|WEB_|KAFKA_|SOURCE_MODE$|WRITE_MODE$|SPARK_|ADLS_|OLLAMA_URL$|PORTAL_COOKIE_SECURE$|PORTAL_AUTH_ENABLED$|DATATRUST_PUBLIC_URL$|AZURE_CLIENT_ID$|PORTAL_SECRET$)'
if (-not $EnableChatAlerts) { $skip = $skip.TrimEnd(')') + '|GOOGLE_CHAT_WEBHOOK_URL$|BRIEFING_CHAT_AT$)' }
$secretPattern = '(PASSWORD|SECRET|_KEY|WEBHOOK_URL|TOKEN|CONNECTION_STRING)$'
$webSettings = @(); $webSecrets = [ordered]@{}
foreach ($k in $dot.Keys) {
    $v = $dot[$k]
    if (-not $v -or $k -match $skip) { continue }
    if ($k -match $secretPattern) { $webSecrets[$k] = $v } else { $webSettings += @{ name = $k; value = $v } }
}
if ($pocQueue) { $webSettings += @{ name = "PIPELINE_TRIGGER_QUEUE_URL"; value = $pocQueue.url } }
if ($dot["LLM_PROVIDER"] -eq "ollama") { Write-Host "Note: LLM_PROVIDER=ollama points at localhost; set OLLAMA_URL to a server Azure can reach, or the AI features fall back to their plain versions." -ForegroundColor Yellow }

Write-Host "Service      : DataTrust AI (separate from the POC data pipeline)"
Write-Host "Subscription : $($account.name) ($($account.id))"
Write-Host "Resource grp : $ResourceGroup in $Location"
Write-Host "Image tag    : $ImageTag"
Write-Host "Data copy    : $(if ($SkipDataCopy) { 'no' } else { 'yes, local datatrust_app -> Azure (replaces what is there)' })"
Write-Host "Chat alerts  : $(if ($EnableChatAlerts) { 'on' } else { 'off' })"
Write-Host "Settings     : $($webSettings.Count) plain, $($webSecrets.Count) secret(s) into Key Vault: $(@($webSecrets.Keys) -join ', ')"
Write-Host "Creates (if missing): $(if (-not $shared) {"PostgreSQL Flexible Server ($PgSku), "})Container Registry Basic, Key Vault, $(if (-not $sharedEnv) {'Log Analytics, Container Apps environment, '})web app (1 vCPU, 2 GB, always on)."
Write-Host "These resources cost money while they exist. Remove this service later with: az group delete -n $ResourceGroup" -ForegroundColor Yellow
Confirm-Plan -Yes:$Yes

# ------------------------------------------------------------------ 1. infrastructure
Step "Resource group"
Invoke-Az group create --name $ResourceGroup --location $Location --tags app=$AppTag --output none

$vault = Find-Vault $ResourceGroup $AppTag                 # keep the passwords of an earlier run
$appPassword = Get-KvSecret $vault "APP_PG_PASSWORD"
$portalSecret = Get-KvSecret $vault "PORTAL_SECRET"
if (-not $appPassword) { $appPassword = New-Password }
if (-not $portalSecret) { $portalSecret = if ($dot["PORTAL_SECRET"]) { $dot["PORTAL_SECRET"] } else { New-Password 48 } }

$me = (Invoke-Az ad signed-in-user show --query id --output tsv)
$clientIp = if ($SkipDataCopy -and -not $shared) { "" } else { Get-PublicIp }
$template = Join-Path $PSScriptRoot "main.bicep"
$params = @{
    location = $Location; prefix = $Prefix; imageTag = $ImageTag; pgSkuName = $PgSku
    pocHost = $PocHost; pocUser = $PocUser; pocDatabase = $PocDatabase
    deployerObjectId = $me; clientIp = $clientIp
    webSettings = @($webSettings); webSecretNames = @($webSecrets.Keys)
}
if ($sharedEnv) { $params.existingEnvironmentId = $sharedEnv.id; $params.existingEnvironmentDomain = $sharedEnv.domain }
$secureArgs = @("pgAdminPassword=$appPassword")
if ($shared) { $params.existingPgHost = $shared.host; $params.pgAdminUser = $AppLogin; $secureArgs = @() }

Step "Infrastructure (registry, key vault$(if (-not $shared) {', PostgreSQL'})$(if (-not $sharedEnv) {', Container Apps environment'})): a few minutes the first time"
$params.deployApps = $false
$o = Invoke-BicepDeployment $ResourceGroup "datatrust-infra" $template $params $secureArgs
$acr = $o.acrName.value; $kv = $o.keyVaultName.value; $pgHost = $o.pgHost.value; $pgUser = $o.pgAdminUser.value
Write-Host "Registry $acr, Key Vault $kv, PostgreSQL $pgHost"

if ($pocQueue) {
    Step "Letting DataTrust start the POC pipeline (send-only access to its queue)"
    $existing = Get-AzQuiet role assignment list --assignee $o.identityPrincipalId.value --scope $pocQueue.scope --role "Storage Queue Data Message Sender" --query "[0].id" --output tsv
    if (-not $existing) {
        Invoke-Az role assignment create --assignee-object-id $o.identityPrincipalId.value --assignee-principal-type ServicePrincipal `
            --role "Storage Queue Data Message Sender" --scope $pocQueue.scope --output none
    }
}

if ($shared) {
    Step "Database datatrust_app and login $AppLogin on $SharedServer"
    Initialize-SharedDatabase $shared $SharedServer $SharedServerResourceGroup $sharedAdminPassword "datatrust_app" $AppLogin $appPassword $clientIp "datatrust-app-deployer"
}

# ------------------------------------------------------------------ 2. secrets
Step "Secrets into this service's Key Vault"
Set-KvSecret $kv "APP_PG_PASSWORD" $appPassword
Set-KvSecret $kv "PG_PASSWORD" $pocPassword; Write-Host "   PG_PASSWORD (the monitored POC database)"
Set-KvSecret $kv "PORTAL_SECRET" $portalSecret
foreach ($k in $webSecrets.Keys) { Set-KvSecret $kv $k $webSecrets[$k]; Write-Host "   $k" }

# ------------------------------------------------------------------ 3. image
Step "Building the web image in this service's registry ($ImageTag)"
Push-Location $Root
try {
    Publish-Image $acr "datatrust-web:$ImageTag" "Dockerfile" "." @(if ($WithSqlServerDriver) { "WITH_MSSQL=true" })
}
finally { Pop-Location }

# ------------------------------------------------------------------ 4. data
if (-not $SkipDataCopy) {
    Step "Copying DataTrust's local database into Azure"
    $env:AZ_PG_PASSWORD = $appPassword
    try {
        & (Join-Path $PSScriptRoot "..\copy-database.ps1") -LocalPrefix APP_PG -LocalDefaultDb datatrust_app -AzureHost $pgHost -AzureUser $pgUser `
            -AzureDatabase datatrust_app -EnvFile $envFile

        Write-Host "Pointing the POC connection at the POC service's server ..."
        $localPocHost = Get-Setting $dot "PG_HOST" "localhost"
        $sql = "UPDATE portal.connections SET host = '$PocHost', port = 5432, database = '$PocDatabase', username = '$PocUser', " +
               "name = CASE WHEN name = 'DataTrust POC (local)' THEN 'DataTrust POC' ELSE name END " +
               "WHERE password_env = 'PG_PASSWORD' AND host IN ('localhost', '127.0.0.1', '$localPocHost');"
        $env:PGPASSWORD = $appPassword; $env:PGSSLMODE = "require"
        & { $ErrorActionPreference = "Continue"; & (Find-PgTool "psql.exe") -h $pgHost -p 5432 -U $pgUser -d datatrust_app -v ON_ERROR_STOP=1 -c $sql 2>&1 | ForEach-Object { "   $_" } }  # stderr notices must not stop the script: judged by the exit code below
        if ($LASTEXITCODE -ne 0) { throw "Could not update portal.connections." }
    }
    finally {
        Remove-Item Env:AZ_PG_PASSWORD, Env:PGPASSWORD, Env:PGSSLMODE -ErrorAction SilentlyContinue
    }
}

# ------------------------------------------------------------------ 5. web app
Step "Web app"
$params.deployApps = $true
$o = Invoke-BicepDeployment $ResourceGroup "datatrust-apps" $template $params @("pgAdminPassword=$appPassword")
$url = $o.publicUrl.value

Step "Waiting for $url/health"
$ok = $false
for ($i = 1; $i -le 30; $i++) {
    try { $r = Invoke-WebRequest -Uri "$url/health" -UseBasicParsing -TimeoutSec 15; if ($r.StatusCode -eq 200) { $ok = $true; break } } catch { }
    Start-Sleep -Seconds 10
}
$ips = (Get-AzQuiet resource show --resource-group $ResourceGroup --name $o.webAppName.value --resource-type Microsoft.App/containerApps --query "properties.outboundIpAddresses" --output tsv)

Write-Host ""
if ($ok) { Write-Host "DataTrust AI is running: $url" -ForegroundColor Green }
else { Write-Host "Deployed, but /health did not answer yet. Logs: az containerapp logs show -n $($o.webAppName.value) -g $ResourceGroup --follow" -ForegroundColor Yellow }
Write-Host "Sign in with your existing DataTrust account (the user table was copied)."
Write-Host "Outbound IP addresses of the app, to allow in firewalls of the databases DataTrust monitors (e.g. Sales DB):"
Write-Host "   $(($ips -join ' ').Trim())"
if ($dot["GOOGLE_CLIENT_ID"]) { Write-Host "Google sign-in: add $url/api/auth/google/callback as an authorized redirect URI." }
Write-Host "Remove this service: az group delete -n $ResourceGroup   (the POC service stays)"
