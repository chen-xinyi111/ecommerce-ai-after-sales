param([ValidateSet('Setup','Build','Dev')][string]$Action = 'Build')
. "$PSScriptRoot/Common.ps1"
$nodeRoot = Join-Path $RuntimeRoot 'tools/node'
$nodeExe = Join-Path $nodeRoot 'node.exe'
$pnpmCli = Join-Path $nodeRoot 'pnpm/bin/pnpm.cjs'
$frontendRoot = Join-Path $ProjectRoot 'frontend'

if ($Action -eq 'Setup') {
    New-Item -ItemType Directory -Force $nodeRoot | Out-Null
    $downloadRoot = Join-Path $RuntimeRoot 'downloads/frontend'
    New-Item -ItemType Directory -Force $downloadRoot | Out-Null
    if (!(Test-Path -LiteralPath $nodeExe)) {
        $nodeVersion = '24.19.0'
        $archiveName = "node-v$nodeVersion-win-x64.zip"
        $archive = Join-Path $downloadRoot $archiveName
        $nodeUrl = "https://nodejs.org/dist/v$nodeVersion"
        Invoke-WebRequest "$nodeUrl/$archiveName" -OutFile $archive
        $checksums = (Invoke-WebRequest "$nodeUrl/SHASUMS256.txt").Content
        $line = ($checksums -split "`n" | Where-Object { $_.Trim().EndsWith($archiveName) } | Select-Object -First 1)
        if (!$line -or (Get-FileHash -LiteralPath $archive -Algorithm SHA256).Hash.ToLower() -ne ($line -split '\s+')[0]) { throw 'Node archive checksum mismatch' }
        Expand-Archive -LiteralPath $archive -DestinationPath $downloadRoot -Force
        Copy-Item -LiteralPath (Join-Path $downloadRoot "node-v$nodeVersion-win-x64/node.exe") -Destination $nodeExe
    }
    if (!(Test-Path -LiteralPath $pnpmCli)) {
        $metadata = Invoke-RestMethod 'https://registry.npmjs.org/pnpm/11.19.0'
        $archive = Join-Path $downloadRoot 'pnpm-11.19.0.tgz'
        Invoke-WebRequest $metadata.dist.tarball -OutFile $archive
        $hasher = [Security.Cryptography.SHA512]::Create()
        try { $integrity = 'sha512-' + [Convert]::ToBase64String($hasher.ComputeHash([IO.File]::ReadAllBytes($archive))) }
        finally { $hasher.Dispose() }
        if ($integrity -ne $metadata.dist.integrity) { throw 'pnpm archive integrity mismatch' }
        New-Item -ItemType Directory -Force (Join-Path $nodeRoot 'pnpm') | Out-Null
        & tar.exe -xzf $archive -C (Join-Path $nodeRoot 'pnpm') --strip-components=1
        Assert-NativeSuccess 'pnpm extraction'
    }
}
if (!(Test-Path -LiteralPath $nodeExe) -or !(Test-Path -LiteralPath $pnpmCli)) { throw 'Run Frontend.ps1 -Action Setup first' }
$env:PATH = $nodeRoot + [IO.Path]::PathSeparator + $env:PATH
Push-Location $frontendRoot
try {
    if ($Action -eq 'Setup') {
        & $nodeExe $pnpmCli install --frozen-lockfile
        Assert-NativeSuccess 'Frontend locked dependency installation'
    } elseif ($Action -eq 'Build') {
        $previousFake = $env:USE_FAKE_MODEL
        $previousModel = $env:DEFAULT_MODEL
        try {
            Use-OfflineModel
            Push-Location $ProjectRoot
            try { & $PythonExe scripts/export_business_api.py; Assert-NativeSuccess 'Business OpenAPI export' }
            finally { Pop-Location }
        } finally { $env:USE_FAKE_MODEL = $previousFake; $env:DEFAULT_MODEL = $previousModel }
        & $nodeExe $pnpmCli run types
        Assert-NativeSuccess 'Frontend API type generation'
        & $nodeExe $pnpmCli run build
        Assert-NativeSuccess 'Frontend type check and build'
    } else {
        & $nodeExe $pnpmCli run dev
        Assert-NativeSuccess 'Frontend dev server'
    }
} finally { Pop-Location }
