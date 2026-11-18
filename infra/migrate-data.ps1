<#
Copies the two local databases into Azure PostgreSQL, once:
    datatrust_poc  (the POC pipeline's data: bronze, silver, gold, audit)
    datatrust_app  (DataTrust's own data: logins, connections, rules, ADF telemetry, incidents)
Then points the POC connection in datatrust_app at the Azure server.

Normally run by deploy.ps1. To run it alone:
    $env:AZ_PG_PASSWORD = "<admin password from Key Vault secret PG-PASSWORD>"
    .\infra\migrate-data.ps1 -AzureHost <server>.postgres.database.azure.com -AzureUser datatrustadmin

Needs the PostgreSQL 17 client tools (installed with PostgreSQL in C:\Program Files\PostgreSQL\17\bin)
and your IP allowed in the Azure server's firewall (deploy.ps1 adds it).
Existing tables in the Azure databases are replaced (pg_restore --clean).
#>
param(
    [Parameter(Mandatory = $true)][string]$AzureHost,
    [Parameter(Mandatory = $true)][string]$AzureUser,
    [string]$AzurePassword = $env:AZ_PG_PASSWORD,
    [string]$EnvFile = (Join-Path $PSScriptRoot "..\.env")
)
$ErrorActionPreference = "Stop"
if (-not $AzurePassword) { throw "Set the Azure admin password in AZ_PG_PASSWORD (or pass -AzurePassword)." }

function Read-DotEnv([string]$path) {
    $h = @{}
    foreach ($line in Get-Content -LiteralPath $path -Encoding UTF8) {
        if ($line -match '^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$') {
            $v = $Matches[2].Trim()
            if ($v.Length -ge 2 -and (($v.StartsWith('"') -and $v.EndsWith('"')) -or ($v.StartsWith("'") -and $v.EndsWith("'")))) { $v = $v.Substring(1, $v.Length - 2) }
            $h[$Matches[1]] = $v
        }
    }
    return $h
}
function Get-Setting($envMap, [string]$name, [string]$fallback) {
    if ($envMap.ContainsKey($name) -and $envMap[$name]) { return $envMap[$name] }
    return $fallback
}

$bin = Get-ChildItem "C:\Program Files\PostgreSQL\*\bin\pg_dump.exe" -ErrorAction SilentlyContinue |
       Sort-Object { [int]($_.Directory.Parent.Name -replace '\D', '') } -Descending | Select-Object -First 1
if (-not $bin) { throw "pg_dump.exe not found. Install the PostgreSQL 17 client tools." }
$pgDump = $bin.FullName
$pgRestore = Join-Path $bin.DirectoryName "pg_restore.exe"
$psql = Join-Path $bin.DirectoryName "psql.exe"

$e = Read-DotEnv $EnvFile
$local = @{
    poc = @{ host = (Get-Setting $e "PG_HOST" "localhost"); port = (Get-Setting $e "PG_PORT" "5432"); user = (Get-Setting $e "PG_USER" "postgres")
             password = (Get-Setting $e "PG_PASSWORD" ""); db = (Get-Setting $e "PG_DATABASE" "datatrust_poc") }
}
$local.app = @{ host = (Get-Setting $e "APP_PG_HOST" $local.poc.host); port = (Get-Setting $e "APP_PG_PORT" $local.poc.port)
                user = (Get-Setting $e "APP_PG_USER" $local.poc.user); password = (Get-Setting $e "APP_PG_PASSWORD" $local.poc.password)
                db = (Get-Setting $e "APP_PG_DATABASE" "datatrust_app") }

$work = Join-Path $env:TEMP ("datatrust-migrate-" + [guid]::NewGuid().ToString("N").Substring(0, 8))
New-Item -ItemType Directory -Path $work | Out-Null
try {
    foreach ($pair in @(@("poc", "datatrust_poc"), @("app", "datatrust_app"))) {
        $src = $local[$pair[0]]; $target = $pair[1]
        $file = Join-Path $work "$target.dump"

        Write-Host "Dumping local $($src.db) ..."
        $env:PGPASSWORD = $src.password; $env:PGSSLMODE = "prefer"
        & $pgDump --format=custom --no-owner --no-privileges -h $src.host -p $src.port -U $src.user -d $src.db -f $file
        if ($LASTEXITCODE -ne 0) { throw "pg_dump of $($src.db) failed." }

        Write-Host "Restoring into Azure $target ..."
        $env:PGPASSWORD = $AzurePassword; $env:PGSSLMODE = "require"
        $log = Join-Path $work "$target.restore.log"
        # pg_restore reports on stderr; in Windows PowerShell that must not count as a script error
        $ErrorActionPreference = "Continue"
        & $pgRestore --no-owner --no-privileges --clean --if-exists -h $AzureHost -p 5432 -U $AzureUser -d $target $file 2> $log
        $ErrorActionPreference = "Stop"
        $errors = @(Select-String -Path $log -Pattern "^pg_restore: error" -ErrorAction SilentlyContinue)
        if ($errors.Count -gt 0) {
            Write-Host "  $($errors.Count) item(s) were not restored:" -ForegroundColor Yellow
            $errors | Select-Object -First 8 | ForEach-Object { Write-Host "    $($_.Line)" -ForegroundColor Yellow }
            Write-Host "  (Event triggers of the schema-change guard need a superuser; Azure refuses them. Everything else is unaffected.)" -ForegroundColor Yellow
        }
        $tables = & $psql -h $AzureHost -p 5432 -U $AzureUser -d $target -t -A -c "SELECT count(*) FROM information_schema.tables WHERE table_schema NOT IN ('pg_catalog','information_schema')"
        Write-Host "  $target in Azure now has $($tables.Trim()) tables and views."
    }

    Write-Host "Pointing the POC connection at the Azure server ..."
    $env:PGPASSWORD = $AzurePassword; $env:PGSSLMODE = "require"
    $sql = "UPDATE portal.connections SET host = '$AzureHost', port = 5432, database = 'datatrust_poc', username = '$AzureUser', " +
           "name = CASE WHEN name = 'DataTrust POC (local)' THEN 'DataTrust POC' ELSE name END " +
           "WHERE password_env = 'PG_PASSWORD' AND host IN ('localhost', '127.0.0.1', '$($local.poc.host)');"
    & $psql -h $AzureHost -p 5432 -U $AzureUser -d datatrust_app -v ON_ERROR_STOP=1 -c $sql
    if ($LASTEXITCODE -ne 0) { throw "Could not update portal.connections." }
}
finally {
    Remove-Item -Recurse -Force $work -ErrorAction SilentlyContinue
    Remove-Item Env:PGPASSWORD -ErrorAction SilentlyContinue
    Remove-Item Env:PGSSLMODE -ErrorAction SilentlyContinue
}
Write-Host "Data copy finished." -ForegroundColor Green
