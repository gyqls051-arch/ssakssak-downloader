@echo off
REM ============================================================
REM  Offcut YouTube Downloader - exe build script
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
REM  --collect-all curl_cffi  : bundles its compiled libcurl DLLs + cacert
REM     (PyInstaller cannot auto-detect these; onefile crashes without it)
REM  --collect-submodules mutagen : yt-dlp imports mutagen dynamically
%PY% -m PyInstaller ^
    --noconfirm ^
    --onefile ^
    --windowed ^
    --name "OffcutYoutubeDownloader" ^
    --collect-data sv_ttk ^
    --collect-all curl_cffi ^
    --collect-submodules mutagen ^
    %FFMPEG_BUNDLE% ^
    main.py || goto :error

echo.
echo ============================================================
echo  Build complete!
echo  Output: dist\OffcutYoutubeDownloader.exe
echo ============================================================
echo.
pause
exit /b 0

:error
echo.
echo [ERROR] Build failed. See messages above.
pause
exit /b 1
