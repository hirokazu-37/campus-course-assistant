param()
$ErrorActionPreference = 'Stop'
[Console]::InputEncoding = [System.Text.UTF8Encoding]::new($false)
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
$icon = $null
$timer = $null
try {
    $payload = [Console]::In.ReadToEnd() | ConvertFrom-Json
    if ([Diagnostics.Process]::GetCurrentProcess().SessionId -eq 0 -or -not [Environment]::UserInteractive) {
        throw 'No interactive desktop'
    }
    Add-Type -AssemblyName System.Windows.Forms
    Add-Type -AssemblyName System.Drawing
    $script:balloonObserved = $false
    $icon = New-Object System.Windows.Forms.NotifyIcon
    $icon.Icon = [System.Drawing.SystemIcons]::Information
    $icon.Text = 'Campus Course Assistant'
    $icon.add_BalloonTipShown({ $script:balloonObserved = $true })
    $icon.Visible = $true
    $context = New-Object System.Windows.Forms.ApplicationContext
    $timer = New-Object System.Windows.Forms.Timer
    $timer.Interval = 6500
    $timer.add_Tick({ $context.ExitThread() }.GetNewClosure())
    $timer.Start()
    $icon.ShowBalloonTip(5000, [string]$payload.title, [string]$payload.message, [System.Windows.Forms.ToolTipIcon]::Info)
    [System.Windows.Forms.Application]::Run($context)
    @{api_submitted=$true; balloon_shown_event=$script:balloonObserved; user_visible=$null} | ConvertTo-Json -Compress
    exit 0
} catch {
    @{api_submitted=$false; balloon_shown_event=$false; error_type=$_.Exception.GetType().Name} | ConvertTo-Json -Compress
    exit 1
} finally {
    if ($null -ne $timer) { $timer.Stop(); $timer.Dispose() }
    if ($null -ne $icon) { $icon.Visible=$false; $icon.Dispose() }
}
