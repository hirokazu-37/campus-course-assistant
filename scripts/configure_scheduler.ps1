param(
    [string]$ProjectRoot = (Split-Path -Parent $PSScriptRoot),
    [string]$DataDir = '',
    [string]$PythonExe = 'python',
    [string]$TaskName = '',
    [switch]$Apply
)
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
$ProjectRoot = [IO.Path]::GetFullPath($ProjectRoot)
if (-not $DataDir) { $DataDir = Join-Path $ProjectRoot 'data' }
$DataDir = [IO.Path]::GetFullPath($DataDir)
$pythonCommand = Get-Command $PythonExe -ErrorAction SilentlyContinue
if ($pythonCommand) { $resolvedPython = $pythonCommand.Source }
elseif ($PythonExe -eq 'python' -and (Test-Path -LiteralPath (Join-Path $env:SystemRoot 'py.exe'))) {
    $resolvedPython = (& (Join-Path $env:SystemRoot 'py.exe') -3 -c 'import sys; print(sys.executable)' | Select-Object -Last 1)
} else { throw 'Python not found. Provide -PythonExe with an absolute Python 3.11+ path.' }
if (-not $resolvedPython -or -not (Test-Path -LiteralPath $resolvedPython -PathType Leaf)) { throw 'Resolved Python executable does not exist' }
$LASTEXITCODE = $null
& $resolvedPython -c 'import sys; sys.exit(0 if sys.version_info >= (3,11) else 1)'
if ($null -eq $LASTEXITCODE) { throw 'Python preflight returned no native exit code in this host. Use standard Windows PowerShell with an explicit Python executable; no task was changed.' }
if ($LASTEXITCODE -ne 0) { throw 'Python 3.11+ is required; no task was changed' }
$config = Get-Content -LiteralPath (Join-Path $DataDir 'config.json') -Raw -Encoding utf8 | ConvertFrom-Json
$clock = '09:00'
if ($config.reminder_preferences) { $clock = [string]$config.reminder_preferences.daily_check_time }
if ($clock -notmatch '^(?:[01][0-9]|2[0-3]):[0-5][0-9]$') { throw 'Invalid daily_check_time' }
$zone = Get-TimeZone
if ($zone.SupportsDaylightSavingTime) { throw 'DST host timezone requires manual trigger review; this helper supports fixed offsets only' }
$offset = [TimeSpan]::Parse(([string]$config.utc_offset).TrimStart('+'))
$today = [DateTimeOffset]::UtcNow.ToOffset($offset).Date
$clockValue = [DateTime]::ParseExact($clock, 'HH:mm', [Globalization.CultureInfo]::InvariantCulture)
$configuredAt = [DateTimeOffset]::new($today.AddHours($clockValue.Hour).AddMinutes($clockValue.Minute), $offset)
$localAt = $configuredAt.ToLocalTime().DateTime
$identity = [Security.Principal.WindowsIdentity]::GetCurrent().Name
if (-not $TaskName) {
    $sha = [Security.Cryptography.SHA256]::Create()
    try { $hash = [BitConverter]::ToString($sha.ComputeHash([Text.Encoding]::UTF8.GetBytes($DataDir))).Replace('-','').Substring(0,8) }
    finally { $sha.Dispose() }
    $TaskName = 'CampusCourseAssistant-' + $hash
}
if ($TaskName -match '[\\/]' -or $TaskName.Length -gt 200) { throw 'Invalid task name' }
$shell = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
$wrapper = Join-Path $ProjectRoot 'scripts\run_reminders.ps1'
$arguments = '-NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy Bypass -File "{0}" -ProjectRoot "{1}" -DataDir "{2}" -PythonExe "{3}" -Trigger scheduled' -f $wrapper,$ProjectRoot,$DataDir,$resolvedPython
$marker = 'Campus Course Assistant | ' + $DataDir
$preview = @{task_name=$TaskName; execute=$shell; arguments=$arguments; working_directory=$ProjectRoot;
    configured_time=$clock; configured_offset=$config.utc_offset; windows_timezone=$zone.Id; local_trigger_time=$localAt.ToString('HH:mm');
    additional_trigger='current user logon'; user=$identity; logon_type='Interactive'; run_level='Limited';
    start_when_available=$true; multiple_instances='IgnoreNew'; reminders_enabled=$config.reminders_enabled; apply_requested=[bool]$Apply}
$preview | ConvertTo-Json -Depth 4
if (-not $Apply) {
    Write-Output 'PREVIEW ONLY: no Windows scheduled task was created or changed. Use -Apply and type the confirmation phrase to register.'
    exit 0
}
if (-not $config.reminders_enabled) { throw 'Enable reminders in config.json before registering' }
$existing = Get-ScheduledTask -TaskName $TaskName -TaskPath '\' -ErrorAction SilentlyContinue
if ($existing -and $existing.Description -ne $marker) { throw 'Task name belongs to another task; choose a different name' }
$verb = 'REGISTER'
if ($existing) { $verb = 'UPDATE' }
$phrase = $verb + ' ' + $TaskName
$answer = Read-Host ('Type exactly [' + $phrase + '] to change the Windows scheduled task; anything else cancels')
if ($answer -cne $phrase) { throw 'Cancelled: no scheduled task changed' }
$action = New-ScheduledTaskAction -Execute $shell -Argument $arguments -WorkingDirectory $ProjectRoot
$triggers = @((New-ScheduledTaskTrigger -Daily -At $localAt), (New-ScheduledTaskTrigger -AtLogOn -User $identity))
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -MultipleInstances IgnoreNew -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit (New-TimeSpan -Minutes 5)
$principal = New-ScheduledTaskPrincipal -UserId $identity -LogonType Interactive -RunLevel Limited
$definition = New-ScheduledTask -Action $action -Trigger $triggers -Settings $settings -Principal $principal -Description $marker
Register-ScheduledTask -TaskName $TaskName -TaskPath '\' -InputObject $definition -Force | Select-Object TaskName,State
