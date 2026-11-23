# Helpers shared by infra/poc/deploy.ps1 and infra/datatrust/deploy.ps1. Dot-source it:  . (Join-Path $PSScriptRoot "..\common.ps1")
# Only tooling is shared: the two services get separate resource groups, registries, key vaults, identities and servers.

function Step([string]$t) { Write-Host ""; Write-Host "==> $t" -ForegroundColor Cyan }

function Invoke-Az {
    # Judge by az's exit code: az writes warnings and progress to stderr, which Windows PowerShell would otherwise turn
    # into a terminating error when the output is redirected (for example into a log file).
    $ErrorActionPreference = "Continue"
    $a = @($args | ForEach-Object { $_ })          # an array passed as one argument is flattened
    $out = & az @a
    # Name only the command, never its arguments: they can hold parameter values.
    if ($LASTEXITCODE -ne 0) { throw "az $($a[0]) $($a[1]) $($a[2]) failed (exit $LASTEXITCODE)." }
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

function Get-Setting($map, [string]$name, [string]$fallback) {
    if ($map.Contains($name) -and $map[$name]) { return $map[$name] }
    return $fallback
}

function New-Password([int]$n = 28) {
    $chars = [char[]]'ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz23456789'
    $bytes = New-Object byte[] $n
    [Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($bytes)
    return (-join ($bytes | ForEach-Object { $chars[$_ % $chars.Length] })) + "Aa9"
}

function Write-NoBom([string]$path, [string]$text) { [IO.File]::WriteAllText($path, $text, (New-Object Text.UTF8Encoding($false))) }

# Checks the Azure CLI and the sign-in; returns the account (subscription) in use.
function Assert-AzReady {
    if (-not (Get-Command az -ErrorAction SilentlyContinue)) { throw "Azure CLI not found. Install it: winget install -e --id Microsoft.AzureCLI" }
    $account = (Get-AzQuiet account show --output json) | Out-String
    if (-not $account.Trim()) { throw "Not signed in. Run: az login   (then az account set --subscription <name>)" }
    return ($account | ConvertFrom-Json)
}

function Confirm-Plan([switch]$Yes) {
    if ($Yes) { return }
    $answer = Read-Host "Continue? (y/n)"
    if ($answer -notmatch '^(y|yes)$') { Write-Host "Stopped; nothing was created."; exit 1 }
}

# The key vault this script created earlier in the resource group (found by tag), or $null.
function Find-Vault([string]$resourceGroup, [string]$appTag) {
    return (Get-AzQuiet keyvault list --resource-group $resourceGroup --query "[?tags.app=='$appTag'].name | [0]" --output tsv)
}

function Get-KvSecret([string]$vault, [string]$name) {
    if (-not $vault) { return $null }
    return (Get-AzQuiet keyvault secret show --vault-name $vault --name ($name -replace '_', '-') --query value --output tsv)
}

# Writes a secret from a temporary file, so the value is never on a command line. Retries while the role assignment settles.
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

# Builds an image into the registry. First in Azure (az acr build: no Docker needed here); when the subscription does not
# allow that (ACR Tasks are blocked on some subscriptions), with the local Docker engine (Docker Desktop / Rancher Desktop).
function Publish-Image([string]$acr, [string]$image, [string]$dockerfile, [string]$context, [string[]]$buildArgs = @()) {
    $ErrorActionPreference = "Continue"
    $cloud = @("acr", "build", "--registry", $acr, "--image", $image, "--file", $dockerfile)
    foreach ($b in $buildArgs) { $cloud += @("--build-arg", $b) }
    & az @($cloud + @($context)) 2>&1 | ForEach-Object { "$_" } | Out-Host
    if ($LASTEXITCODE -eq 0) { return }

    Write-Host "Building in Azure is not available; building with the local Docker engine instead." -ForegroundColor Yellow
    if (-not (Get-Command docker -ErrorAction SilentlyContinue)) { throw "No local Docker either. Install Docker Desktop or Rancher Desktop, start it, and run again." }
    & docker info --format "{{.ServerVersion}}" *> $null
    if ($LASTEXITCODE -ne 0) { throw "Docker is installed but its engine is not running. Start Docker Desktop / Rancher Desktop and run again." }
    $server = (Invoke-Az acr show --name $acr --query loginServer --output tsv)
    & az acr login --name $acr 2>&1 | ForEach-Object { "$_" } | Out-Host
    if ($LASTEXITCODE -ne 0) { throw "az acr login to $acr failed." }
    $local = @("build", "--platform", "linux/amd64", "-t", "$server/$image", "-f", $dockerfile)
    foreach ($b in $buildArgs) { $local += @("--build-arg", $b) }
    & docker @($local + @($context)) 2>&1 | ForEach-Object { "$_" } | Out-Host
    if ($LASTEXITCODE -ne 0) { throw "docker build of $image failed." }
    & docker push "$server/$image" 2>&1 | ForEach-Object { "$_" } | Out-Host
    if ($LASTEXITCODE -ne 0) { throw "docker push of $image to $server failed." }
}

# Gets an existing PostgreSQL server ({host, admin, version, location}) or throws.
function Get-SharedServer([string]$resourceGroup, [string]$name) {
    $s = (Get-AzQuiet postgres flexible-server show --resource-group $resourceGroup --name $name `
            --query "{host:fullyQualifiedDomainName, admin:administratorLogin, version:version, location:location}" --output json) | Out-String
    if (-not $s.Trim()) { throw "PostgreSQL server $name not found in resource group $resourceGroup." }
    return ($s | ConvertFrom-Json)
}

# On an existing server: a database owned by its own login, which only that login (and the server admin) may connect to.
# Signs in once as the server admin. The SQL goes through a temporary file, so no password is on a command line.
function Initialize-SharedDatabase($server, [string]$serverName, [string]$serverResourceGroup, [string]$adminPassword,
                                   [string]$database, [string]$login, [string]$loginPassword, [string]$clientIp, [string]$ruleName) {
    if ($clientIp) {
        Invoke-Az postgres flexible-server firewall-rule create --resource-group $serverResourceGroup --server-name $serverName `
            --name $ruleName --start-ip-address $clientIp --end-ip-address $clientIp --output none
        Write-Host "   firewall rule $ruleName allows this computer ($clientIp)"
    }
    $sqlText = @"
SET client_min_messages TO warning;
SELECT format('CREATE ROLE %I LOGIN', '$login') WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '$login') \gexec
ALTER ROLE "$login" WITH LOGIN PASSWORD '$loginPassword';
GRANT "$login" TO "$($server.admin)";
SELECT 'CREATE DATABASE $database OWNER "$login"' WHERE NOT EXISTS (SELECT 1 FROM pg_database WHERE datname = '$database') \gexec
REVOKE CONNECT, TEMPORARY ON DATABASE $database FROM PUBLIC;
SELECT '   database ' || datname || ' owned by ' || pg_get_userbyid(datdba) || ', other logins may not connect' FROM pg_database WHERE datname = '$database';
"@
    $tmp = [IO.Path]::GetTempFileName()
    try {
        Write-NoBom $tmp $sqlText
        $env:PGPASSWORD = $adminPassword; $env:PGSSLMODE = "require"
        & { $ErrorActionPreference = "Continue"; & (Find-PgTool "psql.exe") -h $server.host -p 5432 -U $server.admin -d postgres -v ON_ERROR_STOP=1 -q -t -A -f $tmp 2>&1 | ForEach-Object { "$_" } }
        if ($LASTEXITCODE -ne 0) { throw "Could not create database $database or login $login on $serverName." }
    }
    finally {
        Remove-Item $tmp -Force -ErrorAction SilentlyContinue
        Remove-Item Env:PGPASSWORD, Env:PGSSLMODE -ErrorAction SilentlyContinue
    }
}

# Full path of a PostgreSQL client tool (pg_dump.exe, pg_restore.exe, psql.exe) from the newest local PostgreSQL install.
function Find-PgTool([string]$exe) {
    $bin = Get-ChildItem "C:\Program Files\PostgreSQL\*\bin\$exe" -ErrorAction SilentlyContinue |
           Sort-Object { [int]($_.Directory.Parent.Name -replace '\D', '') } -Descending | Select-Object -First 1
    if (-not $bin) { throw "$exe not found. Install the PostgreSQL 17 client tools." }
    return $bin.FullName
}

function Get-PublicIp {
    try { return (Invoke-RestMethod -Uri "https://api.ipify.org" -TimeoutSec 10).ToString().Trim() }
    catch { Write-Host "Could not find this computer's public IP; the data copy may be blocked by the firewall." -ForegroundColor Yellow; return "" }
}

# Runs one Bicep deployment from a parameter object and returns its outputs. Every value, secure ones included
# ("name=value" in $secureArgs), goes into a temporary parameter file that is deleted afterwards: never on a command line.
function Invoke-BicepDeployment([string]$resourceGroup, [string]$name, [string]$template, [hashtable]$parameters, [string[]]$secureArgs) {
    $p = @{ '$schema' = "https://schema.management.azure.com/schemas/2019-04-01/deploymentParameters.json#"; contentVersion = "1.0.0.0"; parameters = @{} }
    foreach ($k in $parameters.Keys) { $p.parameters[$k] = @{ value = $parameters[$k] } }
    foreach ($s in $secureArgs) { $i = $s.IndexOf("="); $p.parameters[$s.Substring(0, $i)] = @{ value = $s.Substring($i + 1) } }
    $file = Join-Path $env:TEMP "dt-params-$([guid]::NewGuid().ToString('N').Substring(0,8)).json"
    Write-NoBom $file ($p | ConvertTo-Json -Depth 8)
    try {
        $azArgs = @("deployment", "group", "create", "--resource-group", $resourceGroup, "--name", $name, "--template-file", $template, "--parameters", "@$file")
        $out = Invoke-Az @($azArgs + @("--query", "properties.outputs", "--output", "json"))
        return ($out | Out-String | ConvertFrom-Json)
    }
    finally { Remove-Item $file -Force -ErrorAction SilentlyContinue }
}
