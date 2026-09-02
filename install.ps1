<#
.SYNOPSIS
  Installs Revenant as a desktop app: Desktop + Start Menu shortcuts with the app icon.

.DESCRIPTION
  Nothing is copied anywhere and no PATH is modified - the shortcuts point at this
  folder. Delete them with uninstall.ps1.

.PARAMETER NativeWindow
  Also install pywebview, so the app opens in a real frameless window instead of a
  chromeless browser window.

.PARAMETER Cli
  Also create a `revenant.cmd` shim in a folder of your choice and tell you how to
  put it on PATH.
#>
[CmdletBinding()]
param(
  [switch]$NativeWindow,
  [switch]$Cli
)

$ErrorActionPreference = 'Stop'
$here = $PSScriptRoot

# Windows PowerShell 5.1 has no null-conditional operator - keep this 5.1-compatible.
$pythonw = Get-Command pythonw -ErrorAction SilentlyContinue
$python3 = Get-Command python  -ErrorAction SilentlyContinue
if     ($pythonw) { $python = $pythonw.Source }
elseif ($python3) { $python = $python3.Source }
else   { throw 'Python 3.10+ not found on PATH. Install it from python.org and run this again.' }

if ($NativeWindow) {
  Write-Host 'Installing pywebview (native window support)...' -ForegroundColor DarkGray
  if (-not $python3) { throw 'python.exe not found on PATH; cannot install pywebview.' }
  & $python3.Source -m pip install --quiet --upgrade pywebview
}

$icon   = Join-Path $here 'assets\icon.ico'
$target = Join-Path $here 'revenant_gui.py'
$shell  = New-Object -ComObject WScript.Shell

$places = @(
  [Environment]::GetFolderPath('Desktop'),
  (Join-Path $env:APPDATA 'Microsoft\Windows\Start Menu\Programs')
)

foreach ($place in $places) {
  if (-not (Test-Path $place)) { continue }
  $link = $shell.CreateShortcut((Join-Path $place 'Revenant.lnk'))
  $link.TargetPath       = $python
  $link.Arguments        = '"{0}"' -f $target
  $link.WorkingDirectory = $here
  $link.IconLocation     = $icon
  $link.Description      = 'Bring your agent sessions back from the dead'
  $link.Save()
  Write-Host ("  shortcut -> {0}" -f (Join-Path $place 'Revenant.lnk')) -ForegroundColor DarkGray
}

Write-Host ''
Write-Host 'Revenant installed.' -ForegroundColor Green
Write-Host 'Launch it from the Desktop or the Start Menu.'

if ($Cli) {
  # Never `setx PATH "$env:PATH;..."` - that copies the machine PATH into the user
  # scope and silently truncates at 1024 characters.
  $userPath = [Environment]::GetEnvironmentVariable('Path', 'User')
  if (($userPath -split ';') -notcontains $here) {
    $updated = if ([string]::IsNullOrEmpty($userPath)) { $here } else { "$userPath;$here" }
    [Environment]::SetEnvironmentVariable('Path', $updated, 'User')
    Write-Host ''
    Write-Host ("Added {0} to your user PATH." -f $here) -ForegroundColor Green
    Write-Host 'Open a new terminal, then `revenant --since 7d` works from anywhere.'
  } else {
    Write-Host ''
    Write-Host 'This folder is already on your user PATH.' -ForegroundColor DarkGray
  }
}
