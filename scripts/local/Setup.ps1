param([switch]$SkipDownloads)
. "$PSScriptRoot/Common.ps1"
New-Item -ItemType Directory -Force -Path "$RuntimeRoot/tools" | Out-Null
if (!(Test-Path -LiteralPath $UvExe)) {
    if ($SkipDownloads) { throw 'uv is missing' }
    Invoke-WebRequest 'https://github.com/astral-sh/uv/releases/download/0.12.5/uv-x86_64-pc-windows-msvc.zip' -OutFile "$RuntimeRoot/tools/uv.zip"
    Expand-Archive -LiteralPath "$RuntimeRoot/tools/uv.zip" -DestinationPath "$RuntimeRoot/tools" -Force
}
if (!(Test-Path -LiteralPath "$PgBin/pg_ctl.exe")) {
    if ($SkipDownloads) { throw 'PostgreSQL is missing' }
    Invoke-WebRequest 'https://get.enterprisedb.com/postgresql/postgresql-16.15-1-windows-x64-binaries.zip' -OutFile "$RuntimeRoot/tools/postgresql.zip"
    Expand-Archive -LiteralPath "$RuntimeRoot/tools/postgresql.zip" -DestinationPath "$RuntimeRoot/postgres" -Force
}
Push-Location $ProjectRoot
try {
    & $UvExe python install 3.13
    Assert-NativeSuccess 'Python installation'
    & $UvExe sync --frozen --python 3.13
    Assert-NativeSuccess 'Dependency installation'
    $localEnv = Join-Path $ProjectRoot '.env'
    if (!(Test-Path -LiteralPath $localEnv)) {
        $dbPassword = [Convert]::ToHexString([Security.Cryptography.RandomNumberGenerator]::GetBytes(24))
        $internalSecret = [Convert]::ToHexString([Security.Cryptography.RandomNumberGenerator]::GetBytes(32))
        $contents = @(
            'HOST=127.0.0.1','PORT=8080','MODE=local','COMMERCE_ENABLED=true',
            'DATABASE_TYPE=postgres','POSTGRES_HOST=127.0.0.1','POSTGRES_PORT=55432',
            'POSTGRES_USER=commerce',"POSTGRES_PASSWORD=$dbPassword",
            'POSTGRES_DB=agent_commerce_demo',"AUTH_SECRET=$internalSecret",
            'DEFAULT_MODEL=qwen3.8-max','DASHSCOPE_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1',
            'COOKIE_SECURE=false'
        )
        [IO.File]::WriteAllLines($localEnv,$contents,[Text.UTF8Encoding]::new($false))
    }
    Import-ModelKey
    Write-Output "Environment ready. DASHSCOPE_APIKEY present=$([bool]$env:DASHSCOPE_APIKEY)"
} finally { Pop-Location }
