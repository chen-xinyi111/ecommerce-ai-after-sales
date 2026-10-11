param([ValidateSet('Setup','Start','Stop')][string]$Action = 'Start')
. "$PSScriptRoot/Common.ps1"
$asyncRoot = Join-Path $RuntimeRoot 'async'
$javaRoot = Join-Path $asyncRoot 'java'
$kafkaRoot = Join-Path $asyncRoot 'kafka_2.13-3.9.1'
$redisRoot = Join-Path $asyncRoot 'redis'
New-Item -ItemType Directory -Force $asyncRoot | Out-Null
if ($Action -eq 'Setup') {
    $downloads = Join-Path $asyncRoot 'downloads'
    New-Item -ItemType Directory -Force $downloads | Out-Null
    if (!(Test-Path "$javaRoot/bin/java.exe")) {
        $assets = Invoke-RestMethod 'https://api.adoptium.net/v3/assets/latest/21/hotspot?architecture=x64&image_type=jre&os=windows'
        $package = $assets[0].binary.package
        $archive = Join-Path $downloads 'java.zip'
        Invoke-WebRequest $package.link -OutFile $archive
        if ((Get-FileHash $archive -Algorithm SHA256).Hash.ToLower() -ne $package.checksum) { throw 'Java checksum mismatch' }
        Expand-Archive $archive -DestinationPath "$downloads/java" -Force
        $folder = Get-ChildItem "$downloads/java" -Directory | Select-Object -First 1
        if (!$folder.FullName.StartsWith([IO.Path]::GetFullPath($asyncRoot),[StringComparison]::OrdinalIgnoreCase) -or ![IO.Path]::GetFullPath($javaRoot).StartsWith([IO.Path]::GetFullPath($asyncRoot),[StringComparison]::OrdinalIgnoreCase)) { throw 'Java paths must stay inside the project runtime' }
        Move-Item -LiteralPath $folder.FullName -Destination $javaRoot
    }
    if (!(Test-Path "$kafkaRoot/libs")) {
        $url = 'https://archive.apache.org/dist/kafka/3.9.1/kafka_2.13-3.9.1.tgz'
        $archive = Join-Path $downloads 'kafka.tgz'
        Invoke-WebRequest $url -OutFile $archive
        $hash = (Invoke-WebRequest -UseBasicParsing "$url.sha512").Content
        $expected = ($hash -replace '\s','').ToLower()
        $actual = (Get-FileHash $archive -Algorithm SHA512).Hash.ToLower()
        if (!$expected.Contains($actual)) { throw 'Kafka checksum mismatch' }
        & tar.exe -xzf $archive -C $asyncRoot
        Assert-NativeSuccess 'Kafka extraction'
    }
    if (!(Test-Path "$redisRoot/redis-server.exe")) {
        $release = Invoke-RestMethod 'https://api.github.com/repos/redis-windows/redis-windows/releases/tags/7.4.9'
        $asset = $release.assets | Where-Object { $_.name -eq 'Redis-7.4.9-Windows-x64-msys2.zip' } | Select-Object -First 1
        $archive = Join-Path $downloads 'redis.zip'
        Invoke-WebRequest $asset.browser_download_url -OutFile $archive
        $actual = (Get-FileHash $archive -Algorithm SHA256).Hash.ToLower()
        if ($asset.digest) { if ($asset.digest -ne "sha256:$actual") { throw 'Redis checksum mismatch' } }
        elseif (!$release.body.ToLower().Contains($actual)) { throw 'Redis checksum missing or mismatched' }
        Expand-Archive $archive -DestinationPath $redisRoot -Force
        $nested = Join-Path $redisRoot 'Redis-7.4.9-Windows-x64-msys2'
        if (Test-Path $nested) { Copy-Item -Path (Join-Path $nested '*') -Destination $redisRoot -Recurse -Force }
    }
    Write-Output 'Local Java, Apache Kafka 3.9.1 and community Redis Windows 7.4.9 ready'
    exit 0
}
if ($Action -eq 'Stop') {
    foreach ($name in @('kafka','redis')) {
        $pidPath = Join-Path $asyncRoot "$name.pid"
        if (Test-Path $pidPath) {
            $servicePid = [int](Get-Content $pidPath)
            $process = Get-CimInstance Win32_Process -Filter "ProcessId=$servicePid" -ErrorAction SilentlyContinue
            if ($process -and $process.ExecutablePath -and $process.ExecutablePath.StartsWith($asyncRoot,[StringComparison]::OrdinalIgnoreCase)) { Stop-Process -Id $servicePid -Force }
        }
    }
    exit 0
}
function Test-Port([int]$Port) {
    $client = [Net.Sockets.TcpClient]::new()
    try { $client.Connect('127.0.0.1',$Port); return $true } catch { return $false } finally { $client.Dispose() }
}
if ((Test-Port 56379) -and (Test-Port 59092)) { Write-Output 'Redis :56379 and Kafka :59092 ready'; exit 0 }
if (!(Test-Path "$javaRoot/bin/java.exe") -or !(Test-Path "$redisRoot/redis-server.exe") -or !(Test-Path "$kafkaRoot/libs")) { throw 'Run AsyncServices.ps1 -Action Setup first' }
if (!(Test-Port 56379)) {
    $redisConf = Join-Path $redisRoot 'redis.local.conf'
    [IO.File]::WriteAllLines($redisConf,@('bind 127.0.0.1','port 56379','protected-mode yes','save ""','appendonly no'),[Text.UTF8Encoding]::new($false))
    $process = Start-Process -FilePath "$redisRoot/redis-server.exe" -ArgumentList 'redis.local.conf' -WorkingDirectory $redisRoot -WindowStyle Hidden -PassThru -RedirectStandardOutput "$asyncRoot/redis.stdout.log" -RedirectStandardError "$asyncRoot/redis.stderr.log"
    Set-Content "$asyncRoot/redis.pid" $process.Id
}
if (!(Test-Port 59092)) {
    $kafkaConf = Join-Path $asyncRoot 'server.properties'
    $logDir = (Join-Path $asyncRoot 'kafka-data').Replace('\','/')
    [IO.File]::WriteAllLines($kafkaConf,@('process.roles=broker,controller','node.id=1','controller.quorum.voters=1@127.0.0.1:59093','listeners=PLAINTEXT://127.0.0.1:59092,CONTROLLER://127.0.0.1:59093','advertised.listeners=PLAINTEXT://127.0.0.1:59092','controller.listener.names=CONTROLLER','listener.security.protocol.map=CONTROLLER:PLAINTEXT,PLAINTEXT:PLAINTEXT','inter.broker.listener.name=PLAINTEXT',"log.dirs=$logDir",'offsets.topic.replication.factor=1','transaction.state.log.replication.factor=1','transaction.state.log.min.isr=1','num.partitions=4','group.initial.rebalance.delay.ms=0','log.retention.hours=168'),[Text.UTF8Encoding]::new($false))
    if (!(Test-Path "$logDir/meta.properties")) {
        & "$javaRoot/bin/java.exe" -cp "$kafkaRoot/libs/*" kafka.tools.StorageTool format -t 'YWdlbnQtY29tbWVyY2UtMzE' -c $kafkaConf
        Assert-NativeSuccess 'Kafka storage format'
    }
    $classpath = '"' + $kafkaRoot + '/libs/*"'
    $quotedConfig = '"' + $kafkaConf + '"'
    $loggingConf = Join-Path $asyncRoot 'log4j.properties'
    [IO.File]::WriteAllLines($loggingConf,@('log4j.rootLogger=INFO, stdout','log4j.appender.stdout=org.apache.log4j.ConsoleAppender','log4j.appender.stdout.layout=org.apache.log4j.PatternLayout','log4j.appender.stdout.layout.ConversionPattern=%d %-5p %c - %m%n'),[Text.UTF8Encoding]::new($false))
    $loggingOption = '-Dlog4j.configuration=' + ([Uri]$loggingConf).AbsoluteUri
    $arguments = @('-Xms256m','-Xmx512m',$loggingOption,'-cp',$classpath,'kafka.Kafka',$quotedConfig)
    $process = Start-Process -FilePath "$javaRoot/bin/java.exe" -ArgumentList $arguments -WorkingDirectory $kafkaRoot -WindowStyle Hidden -PassThru -RedirectStandardOutput "$asyncRoot/kafka.stdout.log" -RedirectStandardError "$asyncRoot/kafka.stderr.log"
    Set-Content "$asyncRoot/kafka.pid" $process.Id
}
for ($attempt=0; $attempt -lt 40; $attempt++) {
    if ((Test-Port 56379) -and (Test-Port 59092)) { Write-Output 'Redis :56379 and Kafka :59092 ready'; exit 0 }
    Start-Sleep -Seconds 1
}
throw 'Async services readiness failed; inspect .runtime/async/*.log'
