param([ValidateSet('Start','Stop')][string]$Action = 'Start')
. "$PSScriptRoot/Common.ps1"
$simPidPath = Join-Path $RuntimeRoot 'sim-business.pid'
$simServicePath = Join-Path $ProjectRoot 'src/run_sim_business.py'
if (Test-Path -LiteralPath $simPidPath) {
    $simPid = [int](Get-Content -LiteralPath $simPidPath)
    $existing = Get-CimInstance Win32_Process -Filter "ProcessId=$simPid" -ErrorAction SilentlyContinue
    if ($existing -and $existing.ExecutablePath.StartsWith($ProjectRoot,[StringComparison]::OrdinalIgnoreCase) -and $existing.CommandLine.Contains($simServicePath)) {
        if ($Action -eq 'Start') { Write-Output 'Simulation business service already running'; exit 0 }
        $children = Get-CimInstance Win32_Process -Filter "ParentProcessId=$simPid" | Where-Object { $_.CommandLine -and $_.CommandLine.Contains($simServicePath) }
        foreach ($child in $children) { Stop-Process -Id $child.ProcessId -ErrorAction SilentlyContinue }
        Stop-Process -Id $simPid
    }
    Remove-Item -LiteralPath $simPidPath
}
if ($Action -eq 'Stop') { exit 0 }
Import-ModelKey
$process = Start-Process -FilePath $PythonExe -ArgumentList @('"' + $simServicePath + '"') -WorkingDirectory $ProjectRoot -WindowStyle Hidden -PassThru -RedirectStandardOutput "$RuntimeRoot/sim-business.stdout.log" -RedirectStandardError "$RuntimeRoot/sim-business.stderr.log"
Set-Content -LiteralPath $simPidPath -Value $process.Id
for ($attempt = 0; $attempt -lt 30; $attempt++) {
    $process.Refresh()
    if ($process.HasExited) { throw 'Simulation business service failed; inspect .runtime/sim-business.stderr.log' }
    try {
        $result = Invoke-WebRequest 'http://127.0.0.1:8081/health' -UseBasicParsing
    } catch {
        # A protected health endpoint returning 403 proves this process is ready.
        if ($_.Exception.Response -and [int]$_.Exception.Response.StatusCode -eq 403) { Write-Output 'Simulation business service ready on 127.0.0.1:8081'; exit 0 }
    }
    Start-Sleep -Seconds 1
}
throw 'Simulation business service readiness timed out'
