param([switch]$Offline,[switch]$SkipBuild,[ValidateSet('kafka','postgres')][string]$Transport = 'kafka')
. "$PSScriptRoot/Common.ps1"
& "$PSScriptRoot/Database.ps1" -Action Start
& "$PSScriptRoot/Database.ps1" -Action Migrate
& $PythonExe -m commerce.seed_1301
Assert-NativeSuccess 'Preview policy import'
& $PythonExe -m commerce.acceptance_seed
Assert-NativeSuccess 'Non-destructive acceptance order import'
if (!$SkipBuild) { & "$PSScriptRoot/Frontend.ps1" -Action Build }
if (!(Test-Path -LiteralPath (Join-Path $ProjectRoot 'frontend/dist/index.html'))) { throw 'Frontend build is missing' }
& "$PSScriptRoot/Stop.ps1"
& "$PSScriptRoot/Start.ps1" -Offline:$Offline -Transport $Transport
Write-Output 'Workstation: http://127.0.0.1:8080/app/'
