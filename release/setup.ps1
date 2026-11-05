[CmdletBinding()]
param(
    [Parameter(Mandatory=$true)][string]$LibraryDir,
    [Parameter(Mandatory=$true)][string]$BaselineDate,
    [string]$InstallDir = (Join-Path $env:LOCALAPPDATA 'LibrarySync'),
    [string]$GitPath,
    [switch]$InitLibraryGit,
    [string]$GitUserName,
    [string]$GitUserEmail,
    [switch]$Apply,
    [switch]$DryRun,
    [ValidateRange(0,1)][int]$Limit = 1
)
$ErrorActionPreference = 'Stop'
$OutputEncoding = New-Object System.Text.UTF8Encoding($false)
[Console]::OutputEncoding = $OutputEncoding
if ($Apply -and $DryRun) { throw 'Choose Apply or DryRun, not both.' }
$sourceApp = Join-Path $PSScriptRoot 'app'
$pythonExe = Join-Path $sourceApp 'runtime\python.exe'
$installer = Join-Path $sourceApp 'release\install.py'
if (-not (Test-Path -LiteralPath $pythonExe -PathType Leaf)) {
    throw 'Use setup.ps1 from the complete Windows portable ZIP (embedded Python missing).'
}
$arguments = @('-B','-X','utf8',$installer,'--source',$sourceApp,'--install-dir',$InstallDir,
    '--library-dir',$LibraryDir,'--baseline-date',$BaselineDate,'--limit',([string]$Limit))
if ($Apply) { $arguments += '--apply' } else { $arguments += '--dry-run' }
if ($GitPath) { $arguments += @('--git-path',$GitPath) }
if ($InitLibraryGit) { $arguments += '--init-library-git' }
if ($GitUserName) { $arguments += @('--git-user-name',$GitUserName) }
if ($GitUserEmail) { $arguments += @('--git-user-email',$GitUserEmail) }
& $pythonExe @arguments
if ($LASTEXITCODE -ne 0) { throw "Setup failed with exit code $LASTEXITCODE. No sync was started." }
