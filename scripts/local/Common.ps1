$ErrorActionPreference = 'Stop'
$ProjectRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '../..'))
$RuntimeRoot = Join-Path $ProjectRoot '.runtime'
$PythonExe = Join-Path $ProjectRoot '.venv/Scripts/python.exe'
$UvExe = Join-Path $RuntimeRoot 'tools/uv.exe'
$PgBin = Join-Path $RuntimeRoot 'postgres/pgsql/bin'
$PgData = Join-Path $RuntimeRoot 'postgres/data'
$env:UV_PYTHON_INSTALL_DIR = Join-Path $RuntimeRoot 'python'
$env:UV_PYTHON_BIN_DIR = Join-Path $RuntimeRoot 'python-bin'
$env:UV_CACHE_DIR = Join-Path $RuntimeRoot 'cache/uv'
$env:PYTHONPATH = Join-Path $ProjectRoot 'src'
$env:PYTHONUTF8 = '1'
$env:COMMERCE_ENABLED = 'true'

function Get-LocalSetting([string]$Name) {
    $line = Get-Content -LiteralPath (Join-Path $ProjectRoot '.env') | Where-Object { $_.StartsWith("$Name=") } | Select-Object -First 1
    if (!$line) { throw "Missing local setting: $Name" }
    return $line.Substring($Name.Length + 1)
}
function Assert-NativeSuccess([string]$Action) {
    if ($LASTEXITCODE -ne 0) { throw "$Action failed with exit code $LASTEXITCODE" }
}
function Import-ModelKey {
    if (!$env:DASHSCOPE_APIKEY) {
        $env:DASHSCOPE_APIKEY = [Environment]::GetEnvironmentVariable('DASHSCOPE_APIKEY', 'User')
    }
}
function Use-OfflineModel {
    $env:USE_FAKE_MODEL = 'true'
    $env:DEFAULT_MODEL = 'fake'
}
