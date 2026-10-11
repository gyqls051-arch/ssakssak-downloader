@echo off
setlocal EnableExtensions DisableDelayedExpansion
REM ============================================================
REM  SsakSsak Kimchi Downloader - canonical signed release build
REM  Output: dist\SsakSsakKimchiDownloader-Setup-v{version}.exe
REM  Requires Windows, Python 3.13, Inno Setup 6, SignTool, a trusted
REM  code-signing certificate, reviewed legal docs, and a hashed lock.
REM ============================================================

pushd "%~dp0" >nul || exit /b 1
set "PY_LAUNCHER=py"
set "ISCC=%LOCALAPPDATA%\Programs\Inno Setup 6\ISCC.exe"
if not exist "%ISCC%" set "ISCC=%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe"
if not exist "%ISCC%" set "ISCC=%ProgramFiles%\Inno Setup 6\ISCC.exe"

echo.
echo [1/7] Running release preflight before any build work...
%PY_LAUNCHER% -3.13 --version >nul 2>&1 || (
    echo [ERROR] Python 3.13 is not installed.
    goto :error
)
%PY_LAUNCHER% -3.13 release_gate.py preflight || goto :error
if not exist "%ISCC%" (
    echo [ERROR] Inno Setup 6 is not installed.
    echo         Download: https://jrsoftware.org/isdl.php
    goto :error
)

for /f "tokens=1,* delims==" %%A in ('%PY_LAUNCHER% -3.13 release_gate.py metadata') do set "%%A=%%B"
for /f "delims=" %%P in ('%PY_LAUNCHER% -3.13 -c "import sys; print(sys.executable)"') do set "RELEASE_PYTHON=%%P"
if not defined APP_VERSION goto :metadata_error
if not defined FILE_VERSION goto :metadata_error
if not defined CANONICAL_SETUP_NAME goto :metadata_error
if not defined RELEASE_PYTHON goto :metadata_error

echo.
echo [2/7] Building, signing, and verifying the internal app...
call build.bat || goto :error

set "APP_EXE=build\app\%APP_EXE_NAME%"
echo.
echo [3/7] Re-verifying installer input at the trust boundary...
%PY_LAUNCHER% -3.13 release_gate.py verify --kind app --file "%APP_EXE%" || goto :error

echo.
echo [4/7] Removing stale executable outputs...
if not exist "dist" mkdir "dist" || goto :error
if exist "dist\*.exe" del /q "dist\*.exe" || goto :error
if exist "build\signed-uninstaller" rmdir /s /q "build\signed-uninstaller" || goto :error

echo.
echo [5/7] Building signed Setup and signed uninstaller with Inno Setup...
REM Inno replaces $q with a quote and $f with the quoted target filename.
REM release_gate.py invokes SignTool with a fixed argv list, then verifies the
REM Authenticode status, expected thumbprint, and RFC 3161 timestamp.
"%ISCC%" ^
    "--define=MyAppVersion=%APP_VERSION%" ^
    "--define=MyFileVersion=%FILE_VERSION%" ^
    "--signtool=release=$q%RELEASE_PYTHON%$q $q%CD%\release_gate.py$q sign --file $f" ^
    --no-ide-signtools ^
    "installer.iss" || goto :error

set "SETUP=dist\%CANONICAL_SETUP_NAME%"
echo.
echo [6/7] Verifying canonical filename, signer, timestamp, and ProductVersion...
%PY_LAUNCHER% -3.13 release_gate.py finalize --file "%SETUP%" || goto :error

echo.
echo [7/7] Release artifact policy check complete.
echo ============================================================
echo  APPROVED LOCAL ARTIFACT: %SETUP%
echo  Upload this Setup as the only executable asset for tag v%APP_VERSION%.
echo  Never upload the raw app EXE or potprovider\.
echo  Publish version.json only after the uploaded asset is re-verified.
echo ============================================================
popd >nul
exit /b 0

:metadata_error
echo [ERROR] release_gate.py did not emit complete version metadata.
goto :error

:error
set "RC=%ERRORLEVEL%"
if "%RC%"=="0" set "RC=1"
echo.
echo [ERROR] Canonical installer build failed closed. Do not publish dist output.
popd >nul
exit /b %RC%
