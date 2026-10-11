param([switch]$Database)
. "$PSScriptRoot/Common.ps1"
$servicePath = Join-Path $ProjectRoot 'src/run_service.py'
$projectProcesses = Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | Where-Object { $_.Name -like 'python*' -and $_.ExecutablePath -and $_.ExecutablePath.StartsWith($ProjectRoot,[StringComparison]::OrdinalIgnoreCase) -and $_.CommandLine -and $_.CommandLine.Contains($servicePath) }
foreach ($projectProcess in $projectProcesses) {
    Stop-Process -Id $projectProcess.ProcessId -ErrorAction SilentlyContinue
    Write-Output "Project API stopped, PID=$($projectProcess.ProcessId)"
}
$pidFile = Join-Path $RuntimeRoot 'api.pid'
if (Test-Path -LiteralPath $pidFile) {
    $apiPid = [int](Get-Content -LiteralPath $pidFile)
    $apiProcess = Get-CimInstance Win32_Process -Filter "ProcessId=$apiPid" -ErrorAction SilentlyContinue
    if ($apiProcess -and $apiProcess.ExecutablePath.StartsWith($ProjectRoot) -and $apiProcess.CommandLine.Contains('run_service.py')) {
        $children = Get-CimInstance Win32_Process -Filter "ParentProcessId=$apiPid" | Where-Object { $_.CommandLine -and $_.CommandLine.Contains((Join-Path $ProjectRoot 'src/run_service.py')) }
        foreach ($child in $children) { Stop-Process -Id $child.ProcessId -ErrorAction SilentlyContinue }
        Stop-Process -Id $apiPid -ErrorAction SilentlyContinue
        Write-Output 'API stopped'
    }
    Remove-Item -LiteralPath $pidFile
}
& "$PSScriptRoot/SimBusiness.ps1" -Action Stop
if ($Database) { & "$PSScriptRoot/Database.ps1" -Action Stop }
