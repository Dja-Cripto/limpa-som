@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0"
title Limpa Som - Equalizador ao vivo

if exist ".venv\Scripts\python.exe" goto run
echo.
echo O programa ainda nao foi preparado. De dois cliques em INICIAR.bat uma vez (ele prepara tudo)
echo e depois volte aqui.
echo.
pause
exit /b 1

:run
".venv\Scripts\python.exe" -c "import tkinter, limpa_som.equalizador" 2>nul
if errorlevel 1 goto fail
start "" ".venv\Scripts\pythonw.exe" -m limpa_som.equalizador %*
exit /b 0

:fail
echo.
echo Nao consegui abrir a janela do Equalizador. Rode  INICIAR.bat --autoteste  para ver o que falta.
echo.
pause
exit /b 1
