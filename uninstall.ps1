<#
.SYNOPSIS
  Removes what install.ps1 added.

.DESCRIPTION
  A clone is never touched. A copy the installer downloaded carries a marker file,
  and only a folder with that marker is deleted.
#>
$ErrorActionPreference = 'Stop'

$appName = 'Revenant'
$markerName = '.revenant-managed'
$removed = $false

$places = @(
  [Environment]::GetFolderPath('Desktop'),
  (Join-Path $env:APPDATA 'Microsoft\Windows\Start Menu\Programs')
)
foreach ($place in $places) {
  $link = Join-Path $place "$appName.lnk"
  if (Test-Path $link) {
    Remove-Item $link -Force
    Write-Host "removed $link"
    $removed = $true
  }
}

$uninstallKey = "HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\$appName"
if (Test-Path $uninstallKey) {
  Remove-Item $uninstallKey -Recurse -Force
  Write-Host 'removed the Settings > Apps entry'
  $removed = $true
}

$appDir = Join-Path $env:LOCALAPPDATA "Programs\$appName"

$userPath = [Environment]::GetEnvironmentVariable('Path', 'User')
if ($userPath) {
  $kept = @($userPath -split ';' | Where-Object { $_ -and $_ -ne $appDir })
  if ($kept.Count -ne @($userPath -split ';' | Where-Object { $_ }).Count) {
    [Environment]::SetEnvironmentVariable('Path', ($kept -join ';'), 'User')
    Write-Host "removed $appDir from your user PATH"
    $removed = $true
  }
}

if (Test-Path (Join-Path $appDir $markerName)) {
  Remove-Item $appDir -Recurse -Force
  Write-Host "removed $appDir"
  $removed = $true
} elseif (Test-Path $appDir) {
  Write-Host "left $appDir alone: no marker, so it is not a copy this installer made"
}

if (-not $removed) { Write-Host 'nothing to remove' }

$state = Join-Path $env:LOCALAPPDATA $appName
if (Test-Path $state) {
  Write-Host "Revenant's own state lives in $state - delete it by hand if you want it gone."
}
