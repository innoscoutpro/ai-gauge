#requires -version 7
<#
Build a standalone Windows .exe using PyInstaller.

Usage:
    .\build.ps1            # one-folder build (recommended)
    .\build.ps1 -OneFile   # single-file build (slower startup)
#>
param(
    [switch]$OneFile
)

$ErrorActionPreference = "Stop"

$venvPython = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
if (-not (Test-Path $venvPython)) {
    Write-Error "Virtualenv not found at .venv. Run: py -m venv .venv; .venv\Scripts\pip install -e .[dev]"
}

& $venvPython -m pip install --quiet pyinstaller
if ($LASTEXITCODE -ne 0) { Write-Error "Could not install/verify PyInstaller." }

$versionInfo = Join-Path $PSScriptRoot "build\pyinstaller-version-info.txt"
& $venvPython (Join-Path $PSScriptRoot "tools\write_pyinstaller_version_info.py") $versionInfo
if ($LASTEXITCODE -ne 0) { Write-Error "Could not generate executable version metadata." }
$appIcon = Join-Path $PSScriptRoot "src\aigauge\assets\aigaugeicon.ico"

if ($OneFile) {
    $targetExe = Join-Path $PSScriptRoot "dist\ai-gauge.exe"
    if (Test-Path $targetExe) {
        try {
            Remove-Item -LiteralPath $targetExe -Force -ErrorAction Stop
        } catch {
            Write-Error "Cannot replace dist\ai-gauge.exe. Close any running ai-gauge.exe process, then build again."
        }
    }
}

$args = @(
    "-m", "PyInstaller",
    "--noconfirm",
    "--clean",
    "--windowed",
    "--noupx",
    "--name", "ai-gauge",
    "--version-file", $versionInfo,
    "--icon", $appIcon,
    "--paths", "src",
    "--collect-data", "aigauge",
    "--collect-all", "PyQt6.QtWebEngineWidgets",
    "--collect-all", "PyQt6.QtWebEngineCore",
    "pyinstaller_entry.py"
)
if ($OneFile) { $args += "--onefile" }

& $venvPython @args
if ($LASTEXITCODE -ne 0) {
    Write-Error "PyInstaller build failed. If dist\ai-gauge\ai-gauge.exe is locked, close the running app and try again."
}

# The helper is an unsigned console binary that MCP clients launch headlessly,
# so a Defender/SmartScreen block is silent. Give it the same product/version
# resource the GUI carries rather than shipping metadata-less bytes.
$mcpVersionInfo = Join-Path $PSScriptRoot "build\pyinstaller-version-info-mcp.txt"
& $venvPython (Join-Path $PSScriptRoot "tools\write_pyinstaller_version_info.py") $mcpVersionInfo "ai-gauge-mcp"
if ($LASTEXITCODE -ne 0) { Write-Error "Could not generate MCP helper version metadata." }

$mcpArgs = @(
    "-m", "PyInstaller",
    "--noconfirm",
    "--clean",
    "--console",
    "--onefile",
    "--noupx",
    "--name", "ai-gauge-mcp",
    "--version-file", $mcpVersionInfo,
    "--paths", "src",
    "pyinstaller_mcp_entry.py"
)
& $venvPython @mcpArgs
if ($LASTEXITCODE -ne 0) { Write-Error "MCP helper build failed." }
if (-not $OneFile) {
    $mcpSource = Join-Path $PSScriptRoot "dist\ai-gauge-mcp.exe"
    $mcpTarget = Join-Path $PSScriptRoot "dist\ai-gauge\ai-gauge-mcp.exe"
    try {
        Move-Item -LiteralPath $mcpSource -Destination $mcpTarget -Force -ErrorAction Stop
    } catch {
        Write-Error "Cannot place dist\ai-gauge\ai-gauge-mcp.exe. Close any MCP client using the helper, then build again. ($_)"
    }
}

# release.yml smoke-tests and packages the helper at a fixed path. Fail here,
# with the path named, rather than partway through a tag release.
$expectedMcp = if ($OneFile) {
    Join-Path $PSScriptRoot "dist\ai-gauge-mcp.exe"
} else {
    Join-Path $PSScriptRoot "dist\ai-gauge\ai-gauge-mcp.exe"
}
if (-not (Test-Path -LiteralPath $expectedMcp)) {
    Write-Error "MCP helper missing at expected release path: $expectedMcp"
}

# --collect-all on the WebEngine modules also drags in Chromium's debug
# resource packs, the DevTools front-end, and every Qt translation — ~140 MB
# the app never loads. Strip them before the folder is archived (issue #7).
# One-file builds are already packed by this point, so there is nothing to do.
if (-not $OneFile) {
    Write-Host ""
    Write-Host "Pruning unused Qt/Chromium payload..."
    & $venvPython (Join-Path $PSScriptRoot "tools\prune_bundle.py") (Join-Path $PSScriptRoot "dist\ai-gauge")
    if ($LASTEXITCODE -ne 0) { Write-Error "Bundle prune failed." }
}

Write-Host ""
Write-Host "Build complete." -ForegroundColor Green
if ($OneFile) {
    Write-Host "Binary: dist\ai-gauge.exe"
    Write-Host "MCP helper: dist\ai-gauge-mcp.exe"
} else {
    Write-Host "Folder: dist\ai-gauge\  (run ai-gauge.exe inside)"
    Write-Host "MCP helper: dist\ai-gauge\ai-gauge-mcp.exe"
}
