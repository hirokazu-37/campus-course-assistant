param(
    [string]$ProjectRoot = (Split-Path -Parent $PSScriptRoot),
    [string]$DataDir = '',
    [string]$PythonExe = 'python',
    [ValidateSet('manual','scheduled')][string]$Trigger = 'manual'
)
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
$env:PYTHONIOENCODING = 'utf-8'
$ProjectRoot = [IO.Path]::GetFullPath($ProjectRoot)
if (-not $DataDir) { $DataDir = Join-Path $ProjectRoot 'data' }
$DataDir = [IO.Path]::GetFullPath($DataDir)
$logDirectory = Join-Path $DataDir 'logs\scheduler-runs'
New-Item -ItemType Directory -Path $logDirectory -Force | Out-Null
$runId = [Guid]::NewGuid().ToString('N')
$logPath = Join-Path $logDirectory (('{0}-{1}.log' -f [DateTime]::UtcNow.ToString('yyyyMMddTHHmmssfffffffZ'), $runId))
@{event='wrapper_started'; at=[DateTimeOffset]::UtcNow.ToString('o'); run_id=$runId; trigger=$Trigger; project=$ProjectRoot; data=$DataDir; python=$PythonExe} |
    ConvertTo-Json -Compress | Out-File -LiteralPath $logPath -Encoding utf8
$resultCode = 1
Push-Location -LiteralPath $ProjectRoot
try {
    & $PythonExe -m course_assistant --data-dir $DataDir remind check --trigger $Trigger 2>&1 |
        Out-File -LiteralPath $logPath -Encoding utf8 -Append
    $resultCode = $LASTEXITCODE
} catch {
    ('wrapper_error: ' + $_.Exception.Message) | Out-File -LiteralPath $logPath -Encoding utf8 -Append
} finally {
    Pop-Location
    @{event='wrapper_finished'; at=[DateTimeOffset]::UtcNow.ToString('o'); exit_code=$resultCode; run_id=$runId} |
        ConvertTo-Json -Compress | Out-File -LiteralPath $logPath -Encoding utf8 -Append
}
Write-Output $logPath
exit $resultCode
