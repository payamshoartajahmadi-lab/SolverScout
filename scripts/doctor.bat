@echo off
setlocal
set "PMC_PY=%~dp0..\.venv\Scripts\python.exe"
if not exist "%PMC_PY%" (
    echo Run scripts\setup.bat first.
    exit /b 3
)
"%PMC_PY%" -m p_mc_optimizer doctor %*
exit /b %ERRORLEVEL%
