@echo off
REM ============================================================
REM  SsakSsak Kimchi Downloader - exe build script
REM  Creates a single executable for distribution.
REM  (English-only to avoid Windows codepage issues with .bat files)
REM ============================================================

set "PY=py -3.13"

echo.
echo [1/4] Checking Python 3.13...
%PY% --version >nul 2>&1 || (
    echo   [ERROR] Python 3.13 is not installed.
    echo   Install from https://www.python.org/downloads/ then retry.
    pause
    exit /b 1
)

echo.
echo [2/4] Installing dependencies (pyinstaller + requirements.txt)...
%PY% -m pip install --upgrade pyinstaller || goto :error
%PY% -m pip install --upgrade -r requirements.txt || goto :error

echo.
echo [3/4] Checking ffmpeg.exe bundling...
set "FFMPEG_BUNDLE="
if exist "ffmpeg.exe" (
    set "FFMPEG_BUNDLE=--add-binary ffmpeg.exe;."
    echo   - ffmpeg.exe found. Will be bundled.
) else (
    echo   - ffmpeg.exe not found. App will auto-download it on first run.
)

echo.
echo [4/4] Building exe... (this takes 1-2 minutes)
REM  --add-data web_ui        : 디자인 HTML/CSS 번들 (UI 본체)
REM  --collect-all webview    : pywebview + bottle 등
REM  --collect-all pythonnet / clr_loader : WebView2(.NET) 백엔드 로더
REM  --collect-all curl_cffi  : 컴파일된 libcurl DLL + cacert
REM  --collect-submodules mutagen : yt-dlp 가 동적 import
%PY% -m PyInstaller ^
    --noconfirm ^
    --onefile ^
    --windowed ^
    --name "SsakSsakKimchiDownloader" ^
    --add-data "web_ui;web_ui" ^
    --collect-all webview ^
    --collect-all pythonnet ^
    --collect-all clr_loader ^
    --hidden-import clr ^
    --collect-all curl_cffi ^
    --collect-submodules mutagen ^
    %FFMPEG_BUNDLE% ^
    main.py || goto :error

echo.
echo ============================================================
echo  Build complete!
echo  Output: dist\SsakSsakKimchiDownloader.exe
echo ============================================================
echo.
pause
exit /b 0

:error
echo.
echo [ERROR] Build failed. See messages above.
pause
exit /b 1
