param([ValidateSet('Init','Start','Stop','Migrate','Seed','Reset')][string]$Action = 'Start',[string]$Confirm = '')
. "$PSScriptRoot/Common.ps1"
Push-Location $ProjectRoot
try {
    $port = Get-LocalSetting 'POSTGRES_PORT'
    if ((Get-LocalSetting 'POSTGRES_DB') -ne 'agent_commerce_demo' -or (Get-LocalSetting 'POSTGRES_HOST') -ne '127.0.0.1') {
        throw 'Local database helper only operates on the dedicated loopback demo database'
    }
    switch ($Action) {
        'Init' {
            if (!(Test-Path -LiteralPath "$PgData/PG_VERSION")) {
                $pwFile = Join-Path $RuntimeRoot 'postgres/init-password'
                [IO.File]::WriteAllText($pwFile,(Get-LocalSetting 'POSTGRES_PASSWORD'),[Text.UTF8Encoding]::new($false))
                try {
                    & "$PgBin/initdb.exe" -D $PgData -U commerce -A scram-sha-256 "--pwfile=$pwFile" --encoding=UTF8 --locale=C
                    Assert-NativeSuccess 'initdb'
                } finally { Remove-Item -LiteralPath $pwFile -ErrorAction SilentlyContinue }
                Add-Content -LiteralPath "$PgData/postgresql.conf" -Value ([Environment]::NewLine + "listen_addresses = '127.0.0.1'" + [Environment]::NewLine + "port = $port")
            }
            & "$PSScriptRoot/Database.ps1" -Action Start
            $env:PGPASSWORD = Get-LocalSetting 'POSTGRES_PASSWORD'
            try {
                $exists = & "$PgBin/psql.exe" -h 127.0.0.1 -p $port -U commerce -d postgres -tAc "SELECT 1 FROM pg_database WHERE datname='agent_commerce_demo'"
                Assert-NativeSuccess 'database lookup'
                if (!$exists) {
                    & "$PgBin/createdb.exe" -h 127.0.0.1 -p $port -U commerce agent_commerce_demo
                    Assert-NativeSuccess 'createdb'
                }
            } finally { Remove-Item Env:PGPASSWORD -ErrorAction SilentlyContinue }
        }
        'Start' {
            & "$PgBin/pg_ctl.exe" status -D $PgData 2>$null | Out-Null
            if ($LASTEXITCODE -ne 0) {
                & "$PgBin/pg_ctl.exe" start -D $PgData -l "$RuntimeRoot/postgres/postgres.log" -w -t 30
                Assert-NativeSuccess 'PostgreSQL start'
            }
            Write-Output "PostgreSQL ready on 127.0.0.1:$port"
        }
        'Stop' {
            & "$PgBin/pg_ctl.exe" stop -D $PgData -m fast -w
            Assert-NativeSuccess 'PostgreSQL stop'
        }
        'Migrate' {
            & $PythonExe -m commerce.migrate
            Assert-NativeSuccess 'migration'
        }
        'Seed' {
            & $PythonExe -m commerce.seed
            Assert-NativeSuccess 'seed import'
        }
        'Reset' {
            $running = Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -and $_.Name -like 'python*' -and $_.CommandLine.Contains((Join-Path $ProjectRoot 'src/run_service.py')) }
            if ($running) { throw 'Stop the API before resetting the demo database' }
            if ($Confirm -ne 'agent_commerce_demo') { throw 'Reset requires -Confirm agent_commerce_demo' }
            & $PythonExe -m commerce.reset --confirm $Confirm
            Assert-NativeSuccess 'demo reset'
        }
    }
} finally { Pop-Location }
