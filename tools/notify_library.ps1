[CmdletBinding()]
param([string]$Title='Library Sync', [string]$Message='Completed', [string]$Report='',
      [switch]$Apply, [switch]$DryRun, [int]$Limit=1)
$ErrorActionPreference='Stop'
if (-not $Apply -or $DryRun) { Write-Output "Notification preview: $Title - $Message"; exit 0 }
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
$notice=New-Object System.Windows.Forms.NotifyIcon
try {
    $notice.Icon=[System.Drawing.SystemIcons]::Information
    $notice.Visible=$true
    $notice.BalloonTipTitle=$Title
    $notice.BalloonTipText=$Message
    $notice.ShowBalloonTip(8000)
    Start-Sleep -Seconds 8
} finally { $notice.Dispose() }
# OS notification settings may suppress the balloon; reports and latest.json are authoritative.
