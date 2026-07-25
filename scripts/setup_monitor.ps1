# Bootstrap the momentum monitor on a fresh Windows machine.
#
#   powershell -ExecutionPolicy Bypass -File scripts\setup_monitor.ps1
#
# Creates a venv, installs only the monitor's dependencies, scaffolds the
# optional env file, and runs preflight checks. Safe to re-run.

$ErrorActionPreference = "Continue"

$Repo = Split-Path -Parent $PSScriptRoot
$Venv = Join-Path $Repo ".venv-monitor"

function Ok   ($m) { Write-Host "  [OK] $m"   -ForegroundColor Green }
function Warn ($m) { Write-Host "  [!]  $m"   -ForegroundColor Yellow }
function Bad  ($m) { Write-Host "  [X]  $m"   -ForegroundColor Red }
function Hdr  ($m) { Write-Host "`n$m" -ForegroundColor White; Write-Host ("-" * 66) }

Hdr "1. Python"

# 3.10 floor: alpaca_trader.py uses `bool | None` in a def signature without
# `from __future__ import annotations`, a runtime TypeError before 3.10.
$Py = $null
foreach ($c in @("python3.14","python3.13","python3.12","python3.11","python3.10","python","py")) {
    $exe = Get-Command $c -ErrorAction SilentlyContinue
    if ($exe) {
        try {
            & $exe.Source -c "import sys; sys.exit(0 if sys.version_info >= (3,10) else 1)" 2>$null
            if ($LASTEXITCODE -eq 0) { $Py = $exe.Source; break }
        } catch { }
    }
}

if (-not $Py) {
    Bad "No Python >= 3.10 found."
    Write-Host "     Install from https://www.python.org/downloads/windows/"
    Write-Host "     Tick 'Add python.exe to PATH' in the installer."
    exit 1
}
Ok "$(& $Py -V) at $Py"

Hdr "2. Virtual environment"
if (Test-Path $Venv) {
    Ok "reusing $Venv"
} else {
    & $Py -m venv $Venv
    if ($LASTEXITCODE -ne 0) { Bad "venv creation failed"; exit 1 }
    Ok "created $Venv"
}
$VPy = Join-Path $Venv "Scripts\python.exe"

Hdr "3. Dependencies"
& $VPy -m pip install --upgrade pip --quiet
$Req = Join-Path $Repo "momentum-monitor\requirements-monitor.txt"
if (-not (Test-Path $Req)) { Bad "missing $Req"; exit 1 }
# Deliberately NOT the repo-root requirements.txt — it installs the entire
# project (fastapi, pandas, opencv, pytesseract) that the monitor never imports.
& $VPy -m pip install -r $Req --quiet
if ($LASTEXITCODE -eq 0) { Ok "installed from requirements-monitor.txt" }
else { Warn "some packages failed - rerun without --quiet to see details" }

Hdr "4. Configuration"
$EnvFile    = Join-Path $Repo "signal_engine.env"
$EnvExample = Join-Path $Repo "signal_engine.env.example"
if (Test-Path $EnvFile) {
    Ok "signal_engine.env present"
} elseif (Test-Path $EnvExample) {
    Copy-Item $EnvExample $EnvFile
    Ok "created signal_engine.env from example (credentials blank)"
    Warn "monitor runs read-only until ALPACA_API_KEY/SECRET are filled in"
} else {
    Warn "no signal_engine.env.example - monitor will run read-only"
}
if (Test-Path (Join-Path $Repo "momentum-monitor\momentum_config.json")) {
    Ok "momentum_config.json present"
} else {
    Warn "momentum_config.json missing - built-in defaults will be used"
}

Hdr "5. Preflight"
& $VPy -c @"
import importlib, sys

def check(mod, label, required=False):
    try:
        importlib.import_module(mod)
        print(f'  [OK] {label}')
        return True
    except Exception:
        print(f'  {\"[X] \" if required else \"[!] \"}{label} - not installed')
        return False

rich_ok = check('rich', 'rich (required - UI)', required=True)
check('pyautogui', 'pyautogui (TradingView hotkeys)')
check('pygetwindow', 'pygetwindow (browser window discovery)')
check('alpaca', 'alpaca-py (B/S keys + P&L)')
check('plyer', 'plyer (desktop toasts)')
if not rich_ok:
    print()
    print('  rich is required. Install it before launching.')
    sys.exit(1)
"@

Hdr "Next"
Write-Host @"
  Launch:   $Venv\Scripts\python.exe momentum-monitor\momentum_signal.py
  Run it from a real console window (cmd or PowerShell), not an IDE pane -
  single-key hotkeys are read via msvcrt and need a real console.

  Before the 1-9 keys will work:
    - Brave or Chrome running
    - a TradingView chart pinned at tab BRAVE_TV_TAB (default 1)
    - Windows uses Ctrl+<n> for tabs and Alt+W to save (Mac uses Cmd/Option)

  See momentum-monitor\DEPLOYMENT.md for the full checklist.
"@
