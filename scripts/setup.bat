@echo off
setlocal
cd /d "%~dp0.."
if defined PMC_PYTHON312 (
    "%PMC_PYTHON312%" -c "import sys; sys.exit(sys.version_info[:2] != (3, 12))" || exit /b 3
    "%PMC_PYTHON312%" -m venv .venv || exit /b 3
) else (
    py -3.12 -m venv .venv || exit /b 3
)
echo Installing locked packages. Internet access may be required.
".venv\Scripts\python.exe" -m pip install -r requirements.lock || exit /b 3
".venv\Scripts\python.exe" -m pip install --no-deps -e . || exit /b 3
".venv\Scripts\python.exe" -m pip check || exit /b 3
set "PMC_SMOKE=%TEMP%\pmc-smoke-%RANDOM%-%RANDOM%"
mkdir "%PMC_SMOKE%" || exit /b 3
".venv\Scripts\python.exe" -m p_mc_optimizer doctor --data-dir "%PMC_SMOKE%"
if errorlevel 1 goto failed
".venv\Scripts\python.exe" -m p_mc_optimizer solve --data-dir "%PMC_SMOKE%" --problem examples/quadratic_2d.json --policy fixed_solver --solver de.original --budget 1002
if errorlevel 1 goto failed
rmdir /s /q "%PMC_SMOKE%"
exit /b 0
:failed
set "PMC_CODE=%ERRORLEVEL%"
rmdir /s /q "%PMC_SMOKE%"
exit /b %PMC_CODE%
