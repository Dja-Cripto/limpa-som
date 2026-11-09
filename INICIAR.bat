@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0"
title Limpa Som

if exist ".venv\Scripts\python.exe" goto run

echo.
echo Primeira vez: preparando o programa (1 a 2 minutos, precisa de internet)...
echo.
set "PY="
where py >nul 2>nul
if not errorlevel 1 set "PY=py -3"
if defined PY goto have_python
where python >nul 2>nul
if not errorlevel 1 set "PY=python"
if defined PY goto have_python
echo [X] Python nao encontrado.
echo     Instale em https://www.python.org/downloads/  (marque "Add python.exe to PATH")
echo     ou abra o terminal e rode:  winget install Python.Python.3.12
pause
exit /b 1

:have_python
%PY% -m venv .venv
if errorlevel 1 goto fail
".venv\Scripts\python.exe" -m pip install --upgrade pip --quiet
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 goto fail_deps
goto run

:fail_deps
rmdir /s /q .venv

:fail
echo.
echo [X] Nao consegui preparar o programa. Confira a internet e tente de novo.
pause
exit /b 1

:run
rem Sem argumentos: abre a janela. Com argumentos (--console, --autoteste, --simular...): roda aqui no terminal.
if not "%~1"=="" goto console
".venv\Scripts\python.exe" -c "import tkinter, limpa_som.gui" 2>nul
if errorlevel 1 goto gui_fail
start "" ".venv\Scripts\pythonw.exe" -m limpa_som
exit /b 0

:gui_fail
echo.
echo Nao consegui abrir a janela. Abrindo no modo texto...
".venv\Scripts\python.exe" -m limpa_som --console
echo.
pause
exit /b 0

:console
".venv\Scripts\python.exe" -m limpa_som %*
echo.
pause
