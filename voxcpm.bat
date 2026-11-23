@echo off
setlocal EnableExtensions
rem Entrada global de VoxCPM. Toda la logica vive en voxcpm_manager.py.
set "ROOT=%~dp0"
set "PY=%ROOT%venv_voxcpm\Scripts\python.exe"
if not exist "%PY%" set "PY=python"
"%PY%" "%ROOT%app\voxcpm_manager.py" %*
set "CODIGO=%ERRORLEVEL%"
if not "%CODIGO%"=="0" pause
exit /b %CODIGO%
