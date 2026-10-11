param([switch]$RealModel,[int]$PilotCases = 0)
. "$PSScriptRoot/Common.ps1"
& "$PSScriptRoot/Database.ps1" -Action Start
& "$PSScriptRoot/AsyncServices.ps1" -Action Start
Use-OfflineModel
if ($RealModel) {
    Import-ModelKey
    if (!$env:DASHSCOPE_APIKEY) { throw 'DASHSCOPE_APIKEY is required for the separately recorded real-model sample' }
}
$env:PYTHONPATH = "$ProjectRoot/src;$ProjectRoot/scripts"
Push-Location $ProjectRoot
try {
    $arguments = @('-m', 'evaluation.run')
    if ($RealModel) { $arguments += '--real' }
    if ($PilotCases -gt 0) { $arguments += @('--limit', "$PilotCases") }
    & $PythonExe @arguments
    Assert-NativeSuccess 'Isolated evaluation'
    & $PythonExe -m evaluation.report
    Assert-NativeSuccess 'Evaluation evidence report'
} finally { Pop-Location }
