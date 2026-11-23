<#
Copies ONE local PostgreSQL database into an Azure PostgreSQL server (pg_dump | pg_restore --clean). Used by:
    infra/poc/deploy.ps1        -> datatrust_poc  (the POC pipeline's data: bronze, silver, gold, audit)
    infra/datatrust/deploy.ps1  -> datatrust_app  (DataTrust's own data: logins, connections, rules, telemetry, incidents)

To run it alone:
    $env:AZ_PG_PASSWORD = "<admin password of the Azure server, from its Key Vault>"
    .\infra\copy-database.ps1 -LocalPrefix PG -LocalDefaultDb datatrust_poc -AzureHost <server>.postgres.database.azure.com -AzureUser pocadmin -AzureDatabase datatrust_poc

-LocalPrefix picks the .env settings of the local source: PG (PG_HOST, PG_DATABASE, ...) or APP_PG (APP_PG_HOST, ...,
falling back to PG_* like the app does). Existing tables in the Azure database are replaced.
Needs the PostgreSQL client tools (C:\Program Files\PostgreSQL\<n>\bin) and your IP in the Azure server's firewall.
#>
param(
    [Parameter(Mandatory = $true)][ValidateSet("PG", "APP_PG")][string]$LocalPrefix,
    [Parameter(Mandatory = $true)][string]$LocalDefaultDb,
    [Parameter(Mandatory = $true)][string]$AzureHost,
    [Parameter(Mandatory = $true)][string]$AzureUser,
    [Parameter(Mandatory = $true)][string]$AzureDatabase,
    [string]$AzurePassword = $env:AZ_PG_PASSWORD,
    [string[]]$ExcludeSchemas = @(),
    [string[]]$ExcludeTables = @(),
    [string]$EnvFile = (Join-Path $PSScriptRoot "..\.env")
)
$ErrorActionPreference = "Stop"
. (Join-Path $PSScriptRoot "common.ps1")
if (-not $AzurePassword) { throw "Set the Azure admin password in AZ_PG_PASSWORD (or pass -AzurePassword)." }

$pgDump = Find-PgTool "pg_dump.exe"
$pgRestore = Find-PgTool "pg_restore.exe"
$psql = Find-PgTool "psql.exe"

$e = Read-DotEnv $EnvFile
$src = @{ host = (Get-Setting $e "PG_HOST" "localhost"); port = (Get-Setting $e "PG_PORT" "5432"); user = (Get-Setting $e "PG_USER" "postgres")
          password = (Get-Setting $e "PG_PASSWORD" ""); db = (Get-Setting $e "PG_DATABASE" $LocalDefaultDb) }
if ($LocalPrefix -eq "APP_PG") {
    $src = @{ host = (Get-Setting $e "APP_PG_HOST" $src.host); port = (Get-Setting $e "APP_PG_PORT" $src.port); user = (Get-Setting $e "APP_PG_USER" $src.user)
              password = (Get-Setting $e "APP_PG_PASSWORD" $src.password); db = (Get-Setting $e "APP_PG_DATABASE" $LocalDefaultDb) }
}

$work = Join-Path $env:TEMP ("dt-copy-" + [guid]::NewGuid().ToString("N").Substring(0, 8))
New-Item -ItemType Directory -Path $work | Out-Null
try {
    $file = Join-Path $work "$AzureDatabase.dump"
    Write-Host "Dumping local $($src.db) ..."
    $dumpArgs = @("--format=custom", "--no-owner", "--no-privileges", "-h", $src.host, "-p", $src.port, "-U", $src.user, "-d", $src.db, "-f", $file)
    foreach ($s in $ExcludeSchemas) { $dumpArgs += "--exclude-schema=$s" }
    foreach ($t in $ExcludeTables) { $dumpArgs += "--exclude-table=$t" }
    if ($ExcludeSchemas.Count -or $ExcludeTables.Count) { Write-Host "  left out: $(($ExcludeSchemas + $ExcludeTables) -join ', ')" }
    $env:PGPASSWORD = $src.password; $env:PGSSLMODE = "prefer"
    & { $ErrorActionPreference = "Continue"; & $pgDump @dumpArgs 2>&1 | ForEach-Object { "  $_" } }  # stderr notices must not stop the script: judged by the exit code below
    if ($LASTEXITCODE -ne 0) { throw "pg_dump of $($src.db) failed." }

    Write-Host "Restoring into Azure $AzureDatabase on $AzureHost ..."
    $env:PGPASSWORD = $AzurePassword; $env:PGSSLMODE = "require"
    $log = Join-Path $work "restore.log"
    $ErrorActionPreference = "Continue"          # pg_restore reports on stderr; in Windows PowerShell that must not stop the script
    & $pgRestore --no-owner --no-privileges --clean --if-exists -h $AzureHost -p 5432 -U $AzureUser -d $AzureDatabase $file 2> $log
    $ErrorActionPreference = "Stop"
    $errors = @(Select-String -Path $log -Pattern "^pg_restore: error" -ErrorAction SilentlyContinue)
    if ($errors.Count -gt 0) {
        Write-Host "  $($errors.Count) item(s) were not restored:" -ForegroundColor Yellow
        $errors | Select-Object -First 8 | ForEach-Object { Write-Host "    $($_.Line)" -ForegroundColor Yellow }
        Write-Host "  (Event triggers of the schema-change guard need a superuser; Azure refuses them. Everything else is unaffected.)" -ForegroundColor Yellow
    }
    $tables = & { $ErrorActionPreference = "Continue"; & $psql -h $AzureHost -p 5432 -U $AzureUser -d $AzureDatabase -t -A -c "SELECT count(*) FROM information_schema.tables WHERE table_schema NOT IN ('pg_catalog','information_schema')" 2>$null }
    Write-Host "  $AzureDatabase in Azure now has $("$tables".Trim()) tables and views."
}
finally {
    Remove-Item -Recurse -Force $work -ErrorAction SilentlyContinue
    Remove-Item Env:PGPASSWORD -ErrorAction SilentlyContinue
    Remove-Item Env:PGSSLMODE -ErrorAction SilentlyContinue
}
