<#
.SYNOPSIS
  Removes the Revenant shortcuts. The folder itself is left alone.
#>
$ErrorActionPreference = 'Stop'
$places = @(
  [Environment]::GetFolderPath('Desktop'),
  (Join-Path $env:APPDATA 'Microsoft\Windows\Start Menu\Programs')
)
foreach ($place in $places) {
  $link = Join-Path $place 'Revenant.lnk'
  if (Test-Path $link) { Remove-Item $link -Force; Write-Host "removed $link" }
}
$state = Join-Path $env:LOCALAPPDATA 'Revenant'
if (Test-Path $state) {
  Write-Host "Revenant's own state lives in $state - delete it manually if you want it gone."
}
