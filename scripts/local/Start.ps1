param([switch]$Offline,[ValidateSet('kafka','postgres')][string]$Transport = 'kafka',[ValidateSet('database','http')][string]$BusinessBackend = 'http')
. "$PSScriptRoot/Common.ps1"
$env:ASYNC_TRANSPORT = $Transport
if ($Transport -eq 'kafka') { & "$PSScriptRoot/AsyncServices.ps1" -Action Start }
Import-ModelKey
if ($Offline) { Use-OfflineModel }
else {
    if (!$env:DASHSCOPE_APIKEY) { throw 'DASHSCOPE_APIKEY is not visible; use -Offline for deterministic acceptance only' }
    $env:USE_FAKE_MODEL = 'false'
    $env:DEFAULT_MODEL = 'qwen3.8-max'
}
& "$PSScriptRoot/Database.ps1" -Action Start
& "$PSScriptRoot/Database.ps1" -Action Migrate
$env:BUSINESS_BACKEND = $BusinessBackend
if ($BusinessBackend -eq 'http') { & "$PSScriptRoot/SimBusiness.ps1" -Action Start }
$pidFile = Join-Path $RuntimeRoot 'api.pid'
if (Test-Path -LiteralPath $pidFile) {
    $existingPid = [int](Get-Content -LiteralPath $pidFile)
    $existing = Get-CimInstance Win32_Process -Filter "ProcessId=$existingPid" -ErrorAction SilentlyContinue
    if ($existing -and $existing.ExecutablePath -eq $PythonExe) {
        $version = Invoke-RestMethod 'http://127.0.0.1:8080/api/v1/version'
        if ($version.version -ne '3.3' -or $version.transport -ne $Transport) { throw 'API version or transport differs; restart with Workstation.ps1' }
        Write-Output "API is already running, PID=$existingPid"
        exit 0
    }
}
$process = Start-Process -FilePath $PythonExe -ArgumentList @('"' + (Join-Path $ProjectRoot 'src/run_service.py') + '"') -WorkingDirectory $ProjectRoot -WindowStyle Hidden -PassThru -RedirectStandardOutput "$RuntimeRoot/api.stdout.log" -RedirectStandardError "$RuntimeRoot/api.stderr.log"
Set-Content -LiteralPath $pidFile -Value $process.Id
for ($attempt = 0; $attempt -lt 45; $attempt++) {
    $process.Refresh()
    if ($process.HasExited) { throw 'API failed; inspect .runtime/api.stderr.log' }
    try {
        $health = Invoke-RestMethod 'http://127.0.0.1:8080/api/v1/health'
        if ($health.status -eq 'ok') {
            $version = Invoke-RestMethod 'http://127.0.0.1:8080/api/v1/version'
            if ($version.version -ne '3.3' -or $version.transport -ne $Transport) { throw 'API is not the requested version or transport' }
            Write-Output "API ready: http://127.0.0.1:8080/docs (PID=$($process.Id), Offline=$Offline)"
            exit 0
        }
    } catch {}
    Start-Sleep -Seconds 1
}
throw 'API readiness timed out; inspect .runtime/api.stderr.log'
