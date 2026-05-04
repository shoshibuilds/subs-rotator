@echo off
setlocal
python "%~dp0rotator.py" --mode gemma %*
endlocal
