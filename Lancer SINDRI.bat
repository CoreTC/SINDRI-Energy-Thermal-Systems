@echo off
cd /d "%~dp0"
title SINDRI

net session >nul 2>&1
if %errorLevel% == 0 goto :isAdmin

:: Pas admin - relancer en admin (UAC)
powershell -NoProfile -NonInteractive -Command "Start-Process -FilePath '%~f0' -Verb RunAs" >nul 2>&1
exit /b

:isAdmin
title SINDRI [ADMIN]
echo.
echo   SINDRI v3 - Energy - Thermal - Systems
echo   =======================================
echo.
echo   Ouverture dans le navigateur...
echo   Pour fermer : Ctrl+C ici ou ferme cette fenetre
echo.

:: Detection portable de Python : essaie 'py' (launcher officiel), puis 'python'
set PYEXE=
where py >nul 2>&1 && set PYEXE=py
if not defined PYEXE (
    where python >nul 2>&1 && set PYEXE=python
)
if not defined PYEXE (
    echo   [X] Python introuvable. Lance Install.bat d'abord pour l'installer.
    pause
    exit /b 1
)

%PYEXE% "%~dp0pulse.py"
pause
