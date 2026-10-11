param([switch]$Real)
. "$PSScriptRoot/Common.ps1"
$env:PYTHONPATH = (Join-Path $ProjectRoot 'src') + [IO.Path]::PathSeparator + (Join-Path $ProjectRoot 'scripts')
if ($Real) {
    Import-ModelKey
    if (!$env:DASHSCOPE_APIKEY) { throw 'Real evaluation requires the configured model key and authorization' }
    & $PythonExe -m evaluation.product --real-only
} else {
    Use-OfflineModel
    & $PythonExe -m evaluation.product
}
Assert-NativeSuccess 'Product evaluation'
