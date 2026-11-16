<#
  flowide installer — Windows (PowerShell)
  Usage (one line):
    irm https://raw.githubusercontent.com/professordt/flowide/main/install.ps1 | iex
  Or from a clone:
    powershell -ExecutionPolicy Bypass -File .\install.ps1

  NOTE: This script was written and reviewed on Linux and has NOT been executed.
#>

$ErrorActionPreference = "Stop"

$RepoZip = "https://codeload.github.com/professordt/flowide/zip/refs/heads/main"
$Dest     = Join-Path $env:LOCALAPPDATA "flowide"
$PkgDir   = Join-Path $Dest "flowide"
$BinDir   = Join-Path $Dest "bin"

function Fail([string]$msg) {
    Write-Host "flowide install: $msg" -ForegroundColor Red
    exit 1
}

# ---- 1. Python 3.9+ -------------------------------------------------------
$pyExe  = $null
$pyArgs = @()
foreach ($cand in @("python", "python3", "py")) {
    $cmd = Get-Command $cand -ErrorAction SilentlyContinue
    if ($null -eq $cmd) { continue }
    try {
        & $cmd.Source -c "import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)" 2>$null
        if ($LASTEXITCODE -eq 0) {
            $pyExe = $cmd.Source
            if ($cand -eq "py") { $pyArgs = @("-3") }
            break
        }
    } catch {
        continue
    }
}
if ($null -eq $pyExe) {
    Fail "Python 3.9 or newer not found. Install it from https://www.python.org/downloads/ and rerun."
}

# ---- 2. Locate source (clone) or download it -----------------------------
$Src = $null
if ($PSScriptRoot -and (Test-Path (Join-Path $PSScriptRoot "pyproject.toml"))) {
    $Src = $PSScriptRoot
    Write-Host "[1/3] Using local source at $Src"
} else {
    Write-Host "[1/3] Downloading flowide source..."
    $tmp = Join-Path ([System.IO.Path]::GetTempPath()) ("flowide-" + [guid]::NewGuid().ToString("N"))
    New-Item -ItemType Directory -Path $tmp | Out-Null
    $zip = Join-Path $tmp "flowide.zip"
    try {
        Invoke-WebRequest -Uri $RepoZip -OutFile $zip -UseBasicParsing
        Expand-Archive -Path $zip -DestinationPath $tmp -Force
    } catch {
        Fail "could not download or unpack $RepoZip ($($_.Exception.Message))"
    }
    $Src = Join-Path $tmp "flowide-main"
    if (-not (Test-Path (Join-Path $Src "pyproject.toml"))) {
        Fail "unexpected archive layout (no pyproject.toml)"
    }
}

# ---- 3. Install modules + shim -------------------------------------------
Write-Host "[2/3] Installing..."
New-Item -ItemType Directory -Path $PkgDir -Force | Out-Null
New-Item -ItemType Directory -Path $BinDir -Force | Out-Null
Copy-Item -Path (Join-Path $Src "src\flowide\*") -Destination $PkgDir -Recurse -Force

$pyLine = '"' + $pyExe + '"'
if ($pyArgs.Count -gt 0) { $pyLine += " " + ($pyArgs -join " ") }

$shim = @"
@echo off
set "PYTHONPATH=%LOCALAPPDATA%\flowide;%PYTHONPATH%"
$pyLine -m flowide %*
"@
Set-Content -Path (Join-Path $BinDir "flow.cmd") -Value $shim -Encoding ASCII

# ---- 4. User PATH (additive, idempotent; never Machine) -------------------
$userPath = [Environment]::GetEnvironmentVariable("Path", "User")
if ($null -eq $userPath) { $userPath = "" }
$parts = $userPath -split ';' | Where-Object { $_ -ne "" }
if ($parts -notcontains $BinDir) {
    $newPath = (@($parts) + $BinDir) -join ';'
    [Environment]::SetEnvironmentVariable("Path", $newPath, "User")
    Write-Host "      added $BinDir to the User PATH"
}

# ---- 5. Done -------------------------------------------------------------
Write-Host "[3/3] Done." -ForegroundColor Green
Write-Host "      flow is installed. Open a NEW terminal window, then run:"
Write-Host "      flow up"
Write-Host ""
Write-Host "      No administrator rights required. Path: $Dest"
