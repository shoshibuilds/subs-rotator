@echo off
setlocal
python "%~dp0router.py" --mode codex %*
endlocal
