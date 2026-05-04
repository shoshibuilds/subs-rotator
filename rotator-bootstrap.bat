@echo off
setlocal EnableExtensions

set "ROOT=%~dp0"
set "PAPERCLIP_USER=%USERNAME%"
set "APPDATA_NPM=%APPDATA%\npm"
set "PATH=%APPDATA_NPM%;%PATH%"

echo [1/6] Checking prerequisites...

where python >nul 2>nul
if errorlevel 1 (
    echo Python not found. Trying winget install...
    where winget >nul 2>nul
    if errorlevel 1 (
        echo winget is not available. Install Python 3.11+ manually and re-run this file.
        exit /b 1
    )
    winget install --id Python.Python.3.11 -e --silent --accept-package-agreements --accept-source-agreements
    if errorlevel 1 (
        echo Python install failed. Please install Python manually and re-run this file.
        exit /b 1
    )
)

where node >nul 2>nul
if errorlevel 1 (
    echo Node.js not found. Trying winget install...
    where winget >nul 2>nul
    if errorlevel 1 (
        echo winget is not available. Install Node.js LTS manually and re-run this file.
        exit /b 1
    )
    winget install --id OpenJS.NodeJS.LTS -e --silent --accept-package-agreements --accept-source-agreements
    if errorlevel 1 (
        echo Node.js install failed. Please install Node.js LTS manually and re-run this file.
        exit /b 1
    )
)

where npm >nul 2>nul
if errorlevel 1 (
    echo npm not found. Re-open this shell after Node installation and re-run this file.
    exit /b 1
)

echo [2/6] Making npm shims available...
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
    "$npmBin = Join-Path $env:APPDATA 'npm';" ^
    "$userPath = [Environment]::GetEnvironmentVariable('Path','User');" ^
    "if ([string]::IsNullOrWhiteSpace($userPath)) { $userPath = '' }" ^
    "if ($userPath -notlike ('*' + $npmBin + '*')) {" ^
    "  [Environment]::SetEnvironmentVariable('Path', ($userPath.TrimEnd(';') + ';' + $npmBin).Trim(';'), 'User')" ^
    "}" ^
    "$env:Path = $npmBin + ';' + $env:Path"

if not exist "%APPDATA_NPM%" mkdir "%APPDATA_NPM%"

echo [3/6] Ensuring Codex CLI is installed...
where codex >nul 2>nul
if errorlevel 1 (
    npm install -g @openai/codex
    if errorlevel 1 (
        echo Codex CLI install failed.
        exit /b 1
    )
)

echo [4/6] Ensuring Claude CLI is installed when available...
where claude >nul 2>nul
if errorlevel 1 (
    npm install -g @anthropic-ai/claude-code
)

echo [5/6] Preparing local rotator folders...
if not exist "%USERPROFILE%\.codex" mkdir "%USERPROFILE%\.codex"
if not exist "%USERPROFILE%\.claude" mkdir "%USERPROFILE%\.claude"

echo [6/6] Starting rotator manager...
start "" /max pythonw "%ROOT%rotator_manager.py"

endlocal
