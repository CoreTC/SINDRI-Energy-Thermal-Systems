@echo off
chcp 65001 >nul
cd /d "%~dp0"
title SINDRI - Installation complete

:: ═══ Auto-elevation ══════════════════════════════════════════════════════
net session >nul 2>&1
if %errorLevel% neq 0 (
    echo Elevation requise pour installer Python et .NET...
    powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -Verb RunAs" >nul 2>&1
    exit /b
)

echo.
echo ================================================
echo   SINDRI v3 - Installation TOUT-EN-UN
echo   Energy - Thermal - Systems
echo ================================================
echo.

:: ═══ 1. Verification winget ═══════════════════════════════════════════════
echo [1/5] Verification de winget...
where winget >nul 2>&1
if %errorLevel% neq 0 (
    echo   [X] winget introuvable ^(Windows trop ancien^)
    echo       Installe Python 3.10+ et .NET Desktop Runtime a la main :
    echo       https://www.python.org/downloads/
    echo       https://dotnet.microsoft.com/download/dotnet
    pause
    exit /b 1
)
echo   [OK] winget disponible
echo.

:: ═══ 2. Installation Python (si absent) ═══════════════════════════════════
echo [2/5] Verification / installation de Python...
set PYEXE=
where py >nul 2>&1
if %errorLevel% == 0 (
    for /f "delims=" %%i in ('py -c "import sys; print(sys.executable)" 2^>nul') do set PYEXE=%%i
)
if not defined PYEXE (
    echo   Python absent, installation via winget...
    winget install -e --id Python.Python.3.13 --accept-source-agreements --accept-package-agreements --silent
    :: Recharger le PATH pour cette session
    for /f "tokens=2*" %%A in ('reg query "HKLM\SYSTEM\CurrentControlSet\Control\Session Manager\Environment" /v Path 2^>nul ^| findstr /i "REG_"') do set "SysPath=%%B"
    for /f "tokens=2*" %%A in ('reg query "HKCU\Environment" /v Path 2^>nul ^| findstr /i "REG_"') do set "UserPath=%%B"
    set "PATH=%SysPath%;%UserPath%"
    where py >nul 2>&1
    if !errorLevel! == 0 (
        for /f "delims=" %%i in ('py -c "import sys; print(sys.executable)" 2^>nul') do set PYEXE=%%i
    )
)
if not defined PYEXE (
    echo   [!] Python installe mais introuvable dans cette session.
    echo       Redemarre le PC puis relance Install.bat.
    pause
    exit /b 1
)
echo   [OK] Python : %PYEXE%
echo.

:: ═══ 3. Installation .NET Desktop Runtime (si absent) ════════════════════
echo [3/5] Verification / installation de .NET Desktop Runtime...
set NEED_DOTNET=1
where dotnet >nul 2>&1
if %errorLevel% == 0 (
    dotnet --list-runtimes 2>nul | findstr /r "WindowsDesktop.App 1[0-9]" >nul
    if %errorLevel% == 0 set NEED_DOTNET=0
    dotnet --list-runtimes 2>nul | findstr /r "WindowsDesktop.App 8" >nul
    if %errorLevel% == 0 set NEED_DOTNET=0
)
if %NEED_DOTNET% == 1 (
    echo   .NET Desktop Runtime absent, installation via winget...
    winget install -e --id Microsoft.DotNet.DesktopRuntime.10 --accept-source-agreements --accept-package-agreements --silent
    if !errorLevel! neq 0 (
        echo   Tentative fallback vers .NET 8...
        winget install -e --id Microsoft.DotNet.DesktopRuntime.8 --accept-source-agreements --accept-package-agreements --silent
    )
) else (
    echo   [OK] .NET Desktop Runtime deja installe
)
echo.

:: ═══ 4. Installation paquets pip ═════════════════════════════════════════
echo [4/6] Installation psutil + pythonnet + pystray + Pillow...
"%PYEXE%" -m pip install --upgrade pip >nul 2>&1
"%PYEXE%" -m pip install -r requirements.txt
if %errorLevel% neq 0 (
    echo   [X] Erreur pip. Verifie ta connexion.
    pause
    exit /b 1
)
echo   [OK] Dependances Python installees
echo.

:: ═══ 5. Telechargement LibreHardwareMonitor ══════════════════════════════
echo [5/6] Telechargement LibreHardwareMonitor...
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0Download-LHM.ps1"
if %errorLevel% neq 0 (
    echo   [!] Echec download LHM. Certaines fonctions (temperatures, fans) seront limitees.
)
echo.

:: ═══ 6. Raccourci Bureau ════════════════════════════════════════════════
echo [6/6] Creation d'un raccourci sur le Bureau...
powershell -NoProfile -Command "$s=(New-Object -COM WScript.Shell).CreateShortcut([Environment]::GetFolderPath('Desktop')+'\SINDRI.lnk');$s.TargetPath='%~dp0Lancer SINDRI.bat';$s.WorkingDirectory='%~dp0';$s.IconLocation='%SystemRoot%\System32\imageres.dll,109';$s.Save()"
echo   [OK] Raccourci "SINDRI" cree sur le Bureau
echo.

echo ================================================
echo   Installation terminee !
echo ================================================
echo.
echo   SINDRI va se lancer dans 3 secondes...
echo.
timeout /t 3 /nobreak >nul
start "" "%~dp0Lancer SINDRI.bat"
exit /b 0
