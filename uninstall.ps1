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
  # A redirected or roaming profile can leave one of these empty, and Join-Path
  # throws on that, which under ErrorActionPreference Stop used to end the whole
  # uninstall before it had removed anything at all.
  if ([string]::IsNullOrEmpty($place)) { continue }
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

# -Cli adds whichever folder the install came from, which is the clone when it was
# run from one, so both candidates have to come back off.
$ours = @($appDir)
if ($PSScriptRoot) { $ours += $PSScriptRoot }

$userPath = [Environment]::GetEnvironmentVariable('Path', 'User')
if ($userPath) {
  $entries = @($userPath -split ';' | Where-Object { $_ })
  $kept = @($entries | Where-Object { $ours -notcontains $_.TrimEnd('\') -and $ours -notcontains $_ })
  if ($kept.Count -ne $entries.Count) {
    [Environment]::SetEnvironmentVariable('Path', ($kept -join ';'), 'User')
    Write-Host 'removed Revenant from your user PATH'
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
