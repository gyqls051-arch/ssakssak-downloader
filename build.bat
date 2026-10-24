@echo off
setlocal EnableExtensions DisableDelayedExpansion
REM ============================================================
REM  SsakSsak Kimchi Downloader - signed internal app build
REM  This is a release-only script. The raw app EXE is installer input,
REM  never a public distribution artifact.
REM ============================================================

pushd "%~dp0" >nul || exit /b 1
set "PY_LAUNCHER=py"
set "APP_EXE=build\app\SsakSsakKimchiDownloader.exe"

echo.
echo [1/8] Running fail-closed release preflight...
%PY_LAUNCHER% -3.13 --version >nul 2>&1 || (
    echo [ERROR] Python 3.13 is not installed.
    goto :error
)
%PY_LAUNCHER% -3.13 release_gate.py preflight || goto :error

echo.
echo [2/8] Creating a clean, isolated release environment...
if exist "build\release-venv" rmdir /s /q "build\release-venv" || goto :error
%PY_LAUNCHER% -3.13 -m venv "build\release-venv" || goto :error
set "RELEASE_PY=%CD%\build\release-venv\Scripts\python.exe"

REM requirements-release-win.lock must contain the complete reviewed transitive
REM closure with exact versions and SHA-256 hashes. The preflight rejects a
REM missing, ranged, unhashed, VCS, URL, or alternate-index entry.
echo.
echo [3/8] Installing the reviewed hash-locked dependency set...
"%RELEASE_PY%" -m pip install --require-hashes -r requirements-release-win.lock || goto :error

echo.
echo [4/8] Preparing the pinned PO-token side bundle (potprovider\)...
"%RELEASE_PY%" prepare_bundle.py --force --release-lock bundle-release-lock.json || goto :error

REM Never sign an app that silently bundled a workstation-local ffmpeg.exe.
REM ffmpeg is prepared at runtime through the separately verified installer.
echo.
echo [5/8] Enforcing deterministic binary inputs...
if exist "ffmpeg.exe" (
    echo [ERROR] Unexpected repository-root ffmpeg.exe found.
    echo         Remove it and use the runtime verified ffmpeg installer.
    goto :error
)
if exist "build\app" rmdir /s /q "build\app" || goto :error
if exist "build\pyinstaller" rmdir /s /q "build\pyinstaller" || goto :error

echo.
echo [6/8] Building the internal PyInstaller app...
"%RELEASE_PY%" -m PyInstaller ^
    --noconfirm ^
    --clean ^
    --onefile ^
    --windowed ^
    --name "SsakSsakKimchiDownloader" ^
    --icon "app.ico" ^
    --add-data "app.ico;." ^
    --add-data "APP-ICON-LICENSE.txt;." ^
    --version-file "version_info.txt" ^
    --distpath "build\app" ^
    --workpath "build\pyinstaller" ^
    --specpath "build" ^
    --add-data "web_ui;web_ui" ^
    --collect-all webview ^
    --collect-all pythonnet ^
    --collect-all clr_loader ^
    --hidden-import clr ^
    --collect-submodules mutagen ^
    --exclude-module playwright ^
    main.py || goto :error

if not exist "%APP_EXE%" (
    echo [ERROR] PyInstaller did not create %APP_EXE%.
    goto :error
)

echo.
echo [7/8] Authenticode-signing and verifying the internal app...
%PY_LAUNCHER% -3.13 release_gate.py sign --file "%APP_EXE%" || goto :error
%PY_LAUNCHER% -3.13 release_gate.py verify --kind app --file "%APP_EXE%" || goto :error

echo.
echo [8/8] Recording the exact installed package inventory...
if not exist "build\release-metadata" mkdir "build\release-metadata" || goto :error
"%RELEASE_PY%" -m pip list --format=json > "build\release-metadata\python-packages.json" || goto :error
copy /y "requirements-release-win.lock" "build\release-metadata\requirements-release-win.lock" >nul || goto :error
copy /y "bundle-release-lock.json" "build\release-metadata\bundle-release-lock.json" >nul || goto :error

echo.
echo ============================================================
echo  Signed internal app build complete.
echo  Input for installer: %APP_EXE%
echo  DO NOT publish this raw EXE or potprovider\.
echo  Run build_installer.bat to create the sole release artifact.
echo ============================================================
popd >nul
exit /b 0

:error
set "RC=%ERRORLEVEL%"
if "%RC%"=="0" set "RC=1"
echo.
echo [ERROR] Release app build failed closed. No artifact is approved.
popd >nul
exit /b %RC%
