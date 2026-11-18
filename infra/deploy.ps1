<#
Deploys DataTrust AI to Azure Container Apps, end to end. Safe to run again (it updates what exists).

    az login
    .\infra\deploy.ps1                                   # first deployment, copies the local databases once
    .\infra\deploy.ps1 -SkipDataCopy                     # later: new image + settings, data untouched

What it does
  1. infrastructure (infra/main.bicep): container registry, key vault, managed identity, PostgreSQL 17,
     log workspace, Container Apps environment
  2. secrets from your .env into Key Vault (values go by file, never on a command line or into the template)
  3. builds the web image in the registry (az acr build: Docker is not needed on this computer)
  4. copies datatrust_poc and datatrust_app from your local PostgreSQL (infra/migrate-data.ps1)
  5. the web container app, then waits for /health

Options
  -EnableChatAlerts     also send Google Chat alerts and the daily briefing from Azure (off by default, so the
                        cloud copy and your local server do not both post to the same space)
  -WithPipelineWorker   also run the ADLS pipeline worker (PySpark ETL) as a second container app
  -WithSqlServerDriver  build the web image with the Microsoft ODBC driver (SQL Server sources)
  -SkipDataCopy         do not copy the local databases (use on every deployment after the first)
  -Yes                  do not ask before creating resources
#>
param(
    [string]$ResourceGroup = "rg-datatrust-demo",
    [string]$Location = "centralindia",
    [ValidatePattern('^[a-z][a-z0-9]{2,11}$')][string]$Prefix = "datatrust",
    [string]$ImageTag = ("v" + (Get-Date -Format "yyyyMMdd-HHmm")),
    [switch]$EnableChatAlerts,
    [switch]$WithPipelineWorker,
    [switch]$WithSqlServerDriver,
    [switch]$SkipDataCopy,
    [switch]$Yes
)
$ErrorActionPreference = "Stop"
$Root = Resolve-Path (Join-Path $PSScriptRoot "..")
$env:PYTHONIOENCODING = "utf-8"   # az acr build streams logs with non-ASCII characters

function Step([string]$t) { Write-Host ""; Write-Host "==> $t" -ForegroundColor Cyan }
function Invoke-Az {
    $out = & az @args
    if ($LASTEXITCODE -ne 0) { throw "az $($args[0]) $($args[1]) failed (exit $LASTEXITCODE)." }
    return $out
}
# For probes that may fail (not signed in, secret not there yet): never stops the script, returns $null on failure.
function Get-AzQuiet {
    $ErrorActionPreference = "Continue"
    $out = & az @args 2>$null
    if ($LASTEXITCODE -ne 0) { return $null }
    return $out
}
function Read-DotEnv([string]$path) {
    $h = [ordered]@{}
    foreach ($line in Get-Content -LiteralPath $path -Encoding UTF8) {
        if ($line -match '^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$') {
            $v = $Matches[2].Trim()
            if ($v.Length -ge 2 -and (($v.StartsWith('"') -and $v.EndsWith('"')) -or ($v.StartsWith("'") -and $v.EndsWith("'")))) { $v = $v.Substring(1, $v.Length - 2) }
            $h[$Matches[1]] = $v
        }
    }
    return $h
}
function New-Password([int]$n = 28) {
    $chars = [char[]]'ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz23456789'
    $bytes = New-Object byte[] $n
    [Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($bytes)
    return (-join ($bytes | ForEach-Object { $chars[$_ % $chars.Length] })) + "Aa9"
}
function Write-NoBom([string]$path, [string]$text) { [IO.File]::WriteAllText($path, $text, (New-Object Text.UTF8Encoding($false))) }
function Set-KvSecret([string]$vault, [string]$name, [string]$value) {
    $kvName = $name -replace '_', '-'
    $tmp = [IO.Path]::GetTempFileName()
    try {
        Write-NoBom $tmp $value
        $ErrorActionPreference = "Continue"
        for ($i = 1; $i -le 12; $i++) {
            & az keyvault secret set --vault-name $vault --name $kvName --file $tmp --encoding utf-8 --output none
            if ($LASTEXITCODE -eq 0) { return }
            Write-Host "   waiting for Key Vault permissions to take effect ($i/12) ..." -ForegroundColor DarkGray
            Start-Sleep -Seconds 15
        }
        throw "Could not write $kvName to Key Vault $vault."
    }
    finally { Remove-Item $tmp -Force -ErrorAction SilentlyContinue }
}

# ------------------------------------------------------------------ 0. checks
Step "Checking tools and sign-in"
if (-not (Get-Command az -ErrorAction SilentlyContinue)) { throw "Azure CLI not found. Install it: winget install -e --id Microsoft.AzureCLI" }
$account = (Get-AzQuiet account show --output json) | Out-String
if (-not $account.Trim()) { throw "Not signed in. Run: az login   (then az account set --subscription <name>)" }
$account = $account | ConvertFrom-Json
$envFile = Join-Path $Root ".env"
if (-not (Test-Path $envFile)) { throw ".env not found at $envFile" }
$dot = Read-DotEnv $envFile

Write-Host "Subscription : $($account.name) ($($account.id))"
Write-Host "Resource grp : $ResourceGroup in $Location"
Write-Host "Image tag    : $ImageTag"
Write-Host "Data copy    : $(if ($SkipDataCopy) { 'no' } else { 'yes, local datatrust_poc + datatrust_app -> Azure (replaces what is there)' })"
Write-Host "Chat alerts  : $(if ($EnableChatAlerts) { 'on' } else { 'off' })"
Write-Host "Creates (if missing): Container Registry Basic, Key Vault, PostgreSQL Flexible Server (Standard_B1ms), Log Analytics, Container Apps (1 web replica$(if ($WithPipelineWorker) {', 1 pipeline worker'}))."
Write-Host "These resources cost money while they exist. Remove everything later with: az group delete -n $ResourceGroup" -ForegroundColor Yellow
if (-not $Yes) {
    $answer = Read-Host "Continue? (y/n)"
    if ($answer -notmatch '^(y|yes)$') { Write-Host "Stopped; nothing was created."; exit 1 }
}

# ------------------------------------------------------------------ 1. settings from .env
# Never copied: local-only values (database, ports, Kafka, Ollama on localhost) and values the template sets itself.
$skip = '^(PG_|APP_PG|PGSSLMODE$|WEB_|KAFKA_|SOURCE_MODE$|WRITE_MODE$|SPARK_|OLLAMA_URL$|PORTAL_COOKIE_SECURE$|PORTAL_AUTH_ENABLED$|DATATRUST_PUBLIC_URL$|AZURE_CLIENT_ID$|PORTAL_SECRET$|ADLS_)'
if (-not $EnableChatAlerts) { $skip = $skip.TrimEnd(')') + '|GOOGLE_CHAT_WEBHOOK_URL$|BRIEFING_CHAT_AT$)' }
$secretPattern = '(PASSWORD|SECRET|_KEY|WEBHOOK_URL|TOKEN|CONNECTION_STRING)$'

$webSettings = @(); $webSecrets = [ordered]@{}
foreach ($k in $dot.Keys) {
    $v = $dot[$k]
    if (-not $v -or $k -match $skip) { continue }
    if ($k -match $secretPattern) { $webSecrets[$k] = $v } else { $webSettings += @{ name = $k; value = $v } }
}
if ($dot["LLM_PROVIDER"] -eq "ollama") { Write-Host "Note: LLM_PROVIDER=ollama points at localhost; set OLLAMA_URL to a server Azure can reach, or the AI features fall back to their plain versions." -ForegroundColor Yellow }

$workerSettings = @(); $workerSecrets = [ordered]@{}
if ($WithPipelineWorker) {
    foreach ($k in @("ADLS_ACCOUNT_NAME", "ADLS_CONTAINER", "ADLS_FOLDER", "ADLS_WORKER_POLL_SECONDS")) { if ($dot[$k]) { $workerSettings += @{ name = $k; value = $dot[$k] } } }
    foreach ($k in @("ADLS_ACCOUNT_KEY", "ADLS_CONNECTION_STRING")) { if ($dot[$k]) { $workerSecrets[$k] = $dot[$k] } }
}

# ------------------------------------------------------------------ 2. infrastructure
Step "Resource group"
Invoke-Az group create --name $ResourceGroup --location $Location --tags app=datatrust-ai --output none

# Reuse the PostgreSQL admin password and cookie secret of an earlier run, so a rerun does not change them.
$pgPassword = $null; $portalSecret = $null
$existingKv = (Get-AzQuiet keyvault list --resource-group $ResourceGroup --query "[?tags.app=='datatrust-ai'].name | [0]" --output tsv)
if ($existingKv) {
    $pgPassword = (Get-AzQuiet keyvault secret show --vault-name $existingKv --name PG-PASSWORD --query value --output tsv)
    $portalSecret = (Get-AzQuiet keyvault secret show --vault-name $existingKv --name PORTAL-SECRET --query value --output tsv)
}
if (-not $pgPassword) { $pgPassword = New-Password }
if (-not $portalSecret) { $portalSecret = if ($dot["PORTAL_SECRET"]) { $dot["PORTAL_SECRET"] } else { New-Password 48 } }

$me = (Invoke-Az ad signed-in-user show --query id --output tsv)
$clientIp = ""
if (-not $SkipDataCopy) {
    try { $clientIp = (Invoke-RestMethod -Uri "https://api.ipify.org" -TimeoutSec 10).ToString().Trim() } catch { Write-Host "Could not find this computer's public IP; the data copy may be blocked by the firewall." -ForegroundColor Yellow }
}

$paramFile = Join-Path $env:TEMP "datatrust-params-$([guid]::NewGuid().ToString('N').Substring(0,8)).json"
function Write-Params([bool]$apps) {
    $p = @{
        '$schema' = "https://schema.management.azure.com/schemas/2019-04-01/deploymentParameters.json#"
        contentVersion = "1.0.0.0"
        parameters = @{
            location = @{ value = $Location }
            prefix = @{ value = $Prefix }
            deployApps = @{ value = $apps }
            imageTag = @{ value = $ImageTag }
            workerImageTag = @{ value = $ImageTag }
            deployerObjectId = @{ value = $me }
            clientIp = @{ value = $clientIp }
            webSettings = @{ value = @($webSettings) }
            webSecretNames = @{ value = @($webSecrets.Keys) }
            deployWorker = @{ value = [bool]$WithPipelineWorker }
            workerSettings = @{ value = @($workerSettings) }
            workerSecretNames = @{ value = @($workerSecrets.Keys) }
        }
    }
    Write-NoBom $paramFile ($p | ConvertTo-Json -Depth 8)
}

Step "Infrastructure (registry, key vault, PostgreSQL, Container Apps environment): about 10 minutes the first time"
Write-Params $false
$out = Invoke-Az deployment group create --resource-group $ResourceGroup --name "datatrust-infra" `
        --template-file (Join-Path $PSScriptRoot "main.bicep") --parameters "@$paramFile" --parameters "pgAdminPassword=$pgPassword" `
        --query properties.outputs --output json
$o = ($out | Out-String | ConvertFrom-Json)
$acr = $o.acrName.value; $kv = $o.keyVaultName.value; $pgHost = $o.pgHost.value; $pgUser = $o.pgAdminUser.value
Write-Host "Registry $acr, Key Vault $kv, PostgreSQL $pgHost"

# ------------------------------------------------------------------ 3. secrets
Step "Secrets into Key Vault"
Set-KvSecret $kv "PG_PASSWORD" $pgPassword
Set-KvSecret $kv "PORTAL_SECRET" $portalSecret
foreach ($k in $webSecrets.Keys) { Set-KvSecret $kv $k $webSecrets[$k]; Write-Host "   $k" }
foreach ($k in $workerSecrets.Keys) { Set-KvSecret $kv $k $workerSecrets[$k]; Write-Host "   $k" }

# ------------------------------------------------------------------ 4. images
Step "Building the web image in the registry ($ImageTag)"
Push-Location $Root
try {
    $buildArgs = @("acr", "build", "--registry", $acr, "--image", "datatrust-web:$ImageTag", "--file", "Dockerfile")
    if ($WithSqlServerDriver) { $buildArgs += @("--build-arg", "WITH_MSSQL=true") }
    Invoke-Az @($buildArgs + @(".")) | Out-Host
    if ($WithPipelineWorker) {
        Step "Building the pipeline worker image"
        Invoke-Az acr build --registry $acr --image "datatrust-pipeline:$ImageTag" --file Dockerfile.pipeline . | Out-Host
    }
}
finally { Pop-Location }

# ------------------------------------------------------------------ 5. data
if (-not $SkipDataCopy) {
    Step "Copying the local databases into Azure PostgreSQL"
    $env:AZ_PG_PASSWORD = $pgPassword
    try { & (Join-Path $PSScriptRoot "migrate-data.ps1") -AzureHost $pgHost -AzureUser $pgUser -EnvFile $envFile }
    finally { Remove-Item Env:AZ_PG_PASSWORD -ErrorAction SilentlyContinue }
}

# ------------------------------------------------------------------ 6. apps
Step "Container app(s)"
Write-Params $true
$out = Invoke-Az deployment group create --resource-group $ResourceGroup --name "datatrust-apps" `
        --template-file (Join-Path $PSScriptRoot "main.bicep") --parameters "@$paramFile" --parameters "pgAdminPassword=$pgPassword" `
        --query properties.outputs --output json
Remove-Item $paramFile -Force -ErrorAction SilentlyContinue
$o = ($out | Out-String | ConvertFrom-Json)
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
Write-Host "Remove everything: az group delete -n $ResourceGroup"
