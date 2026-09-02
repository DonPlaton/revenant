<#
.SYNOPSIS
  Installs Revenant as a desktop app on Windows.

.DESCRIPTION
  Two ways in, and it works out which one you used:

    - Run from a clone, and the shortcuts point at that folder. Nothing is copied.
    - Pipe it from the web, and it downloads the app into
      %LOCALAPPDATA%\Programs\Revenant first, then points the shortcuts there.

  Either way you get a Desktop and a Start Menu shortcut with the app icon, and
  uninstall.ps1 removes exactly what was added.

.PARAMETER NativeWindow
  Also install pywebview, so the app opens in its own frameless window rather than
  a chromeless browser window. Failing to install it is not fatal.

.PARAMETER Cli
  Also put the install folder on your user PATH, so `revenant` works in any shell.

.PARAMETER InstallPython
  If no suitable Python is found, install one with winget rather than stopping.

.PARAMETER Ref
  Branch, tag or commit to download when running from the web. Default: main.
#>
[CmdletBinding()]
param(
  [switch]$NativeWindow,
  [switch]$Cli,
  [switch]$InstallPython,
  [string]$Ref = 'main'
)

$ErrorActionPreference = 'Stop'
$repo = 'DonPlaton/revenant'
$appName = 'Revenant'
# Written into a copied install so the uninstaller knows the folder is ours to delete.
$markerName = '.revenant-managed'

function Write-Step([string]$text) { Write-Host "  $text" -ForegroundColor DarkGray }

# --------------------------------------------------------------------------- #
# python
# --------------------------------------------------------------------------- #

function Find-Python {
  <#
    Returns the path to a python.exe that is at least 3.10, or $null.

    Every candidate is run rather than trusted: the `python.exe` Windows ships in
    WindowsApps is a stub that opens the Store, and it sits on PATH looking real.
  #>
  # Probing means running programs that may complain on stderr. Under PowerShell 7
  # with ErrorActionPreference Stop that becomes a thrown error, which would reject
  # a perfectly good interpreter.
  $previous = $ErrorActionPreference
  $ErrorActionPreference = 'SilentlyContinue'
  try {
    return Find-PythonCore
  } finally {
    $ErrorActionPreference = $previous
  }
}

function Find-PythonCore {
  $seen = @{}
  $candidates = @()
  foreach ($name in @('python', 'python3')) {
    foreach ($found in (Get-Command $name -All -ErrorAction SilentlyContinue)) {
      $candidates += $found.Source
    }
  }
  # The launcher knows about installs that never touched PATH.
  if (Get-Command py -ErrorAction SilentlyContinue) {
    foreach ($tag in @('-3.14', '-3.13', '-3.12', '-3.11', '-3.10', '-3')) {
      try {
        $path = & py $tag -c 'import sys; print(sys.executable)' 2>$null
        if ($LASTEXITCODE -eq 0 -and $path) { $candidates += $path.Trim() }
      } catch { }
    }
  }
  foreach ($path in $candidates) {
    if (-not $path -or $seen.ContainsKey($path)) { continue }
    $seen[$path] = $true
    try {
      & $path -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' 2>$null
      if ($LASTEXITCODE -eq 0) { return $path }
    } catch { }
  }
  return $null
}

$python = Find-Python

if (-not $python -and $InstallPython) {
  if (-not (Get-Command winget -ErrorAction SilentlyContinue)) {
    throw 'winget is not available, so Python cannot be installed automatically. Get it from https://python.org/downloads'
  }
  Write-Host 'Installing Python 3.13 with winget...' -ForegroundColor DarkGray
  & winget install --exact --id Python.Python.3.13 --source winget `
    --accept-package-agreements --accept-source-agreements
  # winget puts it on PATH for new processes, not this one.
  $env:Path = [Environment]::GetEnvironmentVariable('Path', 'Machine') + ';' +
              [Environment]::GetEnvironmentVariable('Path', 'User')
  $python = Find-Python
}

if (-not $python) {
  Write-Host ''
  Write-Host 'Revenant needs Python 3.10 or newer, and none was found.' -ForegroundColor Yellow
  Write-Host ''
  Write-Host '  winget install --exact --id Python.Python.3.13'
  Write-Host ''
  Write-Host 'or download it from https://python.org/downloads, then run this again.'
  Write-Host 'To install Python as part of this, add -InstallPython.'
  exit 1
}

# pythonw.exe runs the app without a console window behind it.
$pythonw = Join-Path (Split-Path $python) 'pythonw.exe'
if (-not (Test-Path $pythonw)) { $pythonw = $python }

# --------------------------------------------------------------------------- #
# where the app is going to live
# --------------------------------------------------------------------------- #

$source = $PSScriptRoot
$fromClone = $source -and (Test-Path (Join-Path $source 'revenant_gui.py'))

if ($fromClone) {
  $here = $source
  Write-Host "Installing Revenant from $here" -ForegroundColor Cyan
} else {
  # Piped from the web: there is no folder yet, so make one.
  $here = Join-Path $env:LOCALAPPDATA "Programs\$appName"
  Write-Host "Downloading Revenant ($Ref) to $here" -ForegroundColor Cyan

  $staging = Join-Path ([System.IO.Path]::GetTempPath()) ("revenant-" + [Guid]::NewGuid().ToString('N'))
  New-Item -ItemType Directory -Path $staging -Force | Out-Null
  try {
    $archive = Join-Path $staging 'source.zip'
    # This form of the URL takes a branch, a tag or a commit alike. The override is
    # for installing from a mirror, and for testing this path without GitHub.
    $url = $env:REVENANT_ARCHIVE_URL
    if (-not $url) { $url = "https://github.com/$repo/archive/$Ref.zip" }
    [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
    Invoke-WebRequest -Uri $url -OutFile $archive -UseBasicParsing
    Expand-Archive -Path $archive -DestinationPath $staging -Force

    $unpacked = Get-ChildItem -Path $staging -Directory | Select-Object -First 1
    if (-not $unpacked) { throw "The download did not contain a source folder." }

    if (Test-Path $here) {
      if (-not (Test-Path (Join-Path $here $markerName))) {
        throw "$here already exists and was not put there by this installer. Move it aside first."
      }
      Remove-Item $here -Recurse -Force
    }
    New-Item -ItemType Directory -Path $here -Force | Out-Null
    Copy-Item -Path (Join-Path $unpacked.FullName '*') -Destination $here -Recurse -Force
    Set-Content -Path (Join-Path $here $markerName) -Value $Ref -Encoding ASCII
    Write-Step 'downloaded'
  } finally {
    Remove-Item $staging -Recurse -Force -ErrorAction SilentlyContinue
  }
}

$target = Join-Path $here 'revenant_gui.py'
if (-not (Test-Path $target)) { throw "revenant_gui.py is missing from $here" }

# --------------------------------------------------------------------------- #
# the native window, which is optional by design
# --------------------------------------------------------------------------- #

if ($NativeWindow) {
  Write-Host 'Installing pywebview for a native window...' -ForegroundColor DarkGray
  & $python -m pip install --quiet --upgrade pywebview
  if ($LASTEXITCODE -ne 0) {
    Write-Host '  pywebview would not install; the app will open in a browser window instead.' -ForegroundColor Yellow
  }
}

# --------------------------------------------------------------------------- #
# shortcuts
# --------------------------------------------------------------------------- #

$icon = Join-Path $here 'assets\icon.ico'
$shell = New-Object -ComObject WScript.Shell
$places = @(
  [Environment]::GetFolderPath('Desktop'),
  (Join-Path $env:APPDATA 'Microsoft\Windows\Start Menu\Programs')
)

foreach ($place in $places) {
  if (-not (Test-Path $place)) { continue }
  $linkPath = Join-Path $place "$appName.lnk"
  $link = $shell.CreateShortcut($linkPath)
  $link.TargetPath       = $pythonw
  $link.Arguments        = '"{0}"' -f $target
  $link.WorkingDirectory = $here
  if (Test-Path $icon) { $link.IconLocation = $icon }
  $link.Description      = 'Bring your agent sessions back from the dead'
  $link.Save()
  Write-Step "shortcut -> $linkPath"
}

if ($Cli) {
  # Never `setx PATH "$env:PATH;..."` - that copies the machine PATH into the user
  # scope and silently truncates at 1024 characters.
  $userPath = [Environment]::GetEnvironmentVariable('Path', 'User')
  if (($userPath -split ';') -notcontains $here) {
    $updated = if ([string]::IsNullOrEmpty($userPath)) { $here } else { "$userPath;$here" }
    [Environment]::SetEnvironmentVariable('Path', $updated, 'User')
    Write-Step "added $here to your user PATH"
  } else {
    Write-Step 'already on your user PATH'
  }
}

Write-Host ''
Write-Host "$appName is installed." -ForegroundColor Green
Write-Host 'Open it from the Desktop or the Start Menu.'
if ($Cli) { Write-Host 'In a new terminal, `revenant --since 7d` works from anywhere.' }
