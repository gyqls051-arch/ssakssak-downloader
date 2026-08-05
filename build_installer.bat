@echo off
REM ============================================================
REM  SsakSsak Kimchi Downloader - installer build script
REM  Requires: Inno Setup 6 (https://jrsoftware.org/isdl.php)
REM  Run build.bat first to create dist\SsakSsakKimchiDownloader.exe
REM  (English-only to avoid Windows codepage issues with .bat files)
REM ============================================================

set "ISCC=%LOCALAPPDATA%\Programs\Inno Setup 6\ISCC.exe"
if not exist "%ISCC%" set "ISCC=%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe"
if not exist "%ISCC%" set "ISCC=%ProgramFiles%\Inno Setup 6\ISCC.exe"

if not exist "%ISCC%" (
    echo [ERROR] Inno Setup 6 is not installed.
    echo   Download: https://jrsoftware.org/isdl.php
    pause
    exit /b 1
)

if not exist "dist\SsakSsakKimchiDownloader.exe" (
    echo [ERROR] dist\SsakSsakKimchiDownloader.exe not found.
    echo   Run build.bat first.
    pause
    exit /b 1
)

echo Building installer...
"%ISCC%" installer.iss || (
    echo [ERROR] Installer build failed. See messages above.
    pause
    exit /b 1
)

echo.
echo ============================================================
echo  Installer build complete!
echo  Output: dist\ folder (Korean-named setup exe)
echo ============================================================
echo.
pause
exit /b 0
