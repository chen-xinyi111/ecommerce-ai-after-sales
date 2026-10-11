param([ValidateSet('Start','Stop','Status','Check')][string]$Action = 'Start',[string]$DockerExecutable = '')
. "$PSScriptRoot/Common.ps1"
if (!$DockerExecutable) {
    $dockerCommand = Get-Command docker -ErrorAction SilentlyContinue
    if ($dockerCommand) { $DockerExecutable = $dockerCommand.Source }
    else {
        $dockerCandidates = @(
            (Join-Path $env:LOCALAPPDATA 'Programs/DockerDesktop/resources/bin/docker.exe'),
            (Join-Path $env:ProgramFiles 'Docker/Docker/resources/bin/docker.exe')
        )
        $DockerExecutable = $dockerCandidates | Where-Object { Test-Path -LiteralPath $_ } | Select-Object -First 1
    }
}
if (!$DockerExecutable -or !(Test-Path -LiteralPath $DockerExecutable)) { throw 'Docker executable was not found. Specify -DockerExecutable with its installed path.' }
$configFile = Join-Path $ProjectRoot 'docker/.env.commerce'
if (!(Test-Path -LiteralPath $configFile)) {
    $dbSecret = [Convert]::ToHexString([Security.Cryptography.RandomNumberGenerator]::GetBytes(24))
    $authSecret = [Convert]::ToHexString([Security.Cryptography.RandomNumberGenerator]::GetBytes(32))
    [IO.File]::WriteAllText($configFile, "COMMERCE_HTTP_PORT=18080`nCOMMERCE_DB_PASSWORD=$dbSecret`nCOMMERCE_AUTH_SECRET=$authSecret`nUSE_FAKE_MODEL=true`nDEFAULT_MODEL=fake`nDASHSCOPE_APIKEY=`n", [Text.UTF8Encoding]::new($false))
}
Push-Location $ProjectRoot
try {
    $options = @('compose','--env-file',$configFile,'-f','compose.yaml')
    switch ($Action) {
        'Start' { & $DockerExecutable @options up --build -d --wait --wait-timeout 600 }
        'Stop' { & $DockerExecutable @options down }
        'Status' { & $DockerExecutable @options ps }
        'Check' { & $DockerExecutable @options config --quiet }
    }
    Assert-NativeSuccess "Docker $Action"
} finally { Pop-Location }
