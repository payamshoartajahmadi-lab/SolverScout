@echo off
setlocal
"%~dp0..\.venv\Scripts\python.exe" -m p_mc_optimizer package %*
exit /b %ERRORLEVEL%
