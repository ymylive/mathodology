@echo off
REM Build mathodology-%VERSION%.msi. Requires WiX 3.x (candle, light, heat) on PATH.
REM Inputs (must exist):
REM   ..\..\target\release\gateway.exe
REM   ..\..\apps\web\dist\
REM   ..\..\apps\agent-worker\
REM   ..\..\packages\py-contracts\
REM   ..\..\config\providers.toml
REM   ..\..\.env.example
REM   nssm.exe in this directory (CI downloads it before invoking this)
REM
REM Env:
REM   VERSION   required (e.g. 0.3.0 -> 0.3.0.0; WiX wants 4-part).

setlocal
if "%VERSION%"=="" (
    echo !! VERSION env var required ^(e.g. set VERSION=0.3.0^)
    exit /b 64
)
set HERE=%~dp0
set ROOT=%HERE%..\..
cd /d "%HERE%"

REM 4-part version for WiX. Most release tags are 3-part; pad with .0.
set WIX_VERSION=%VERSION%.0
echo %VERSION% | findstr /r "^[0-9][0-9]*\.[0-9][0-9]*\.[0-9][0-9]*\.[0-9][0-9]*$" >nul
if %errorlevel%==0 set WIX_VERSION=%VERSION%

if not exist "%ROOT%\target\release\gateway.exe" (
    echo !! %ROOT%\target\release\gateway.exe missing
    exit /b 1
)
if not exist "%HERE%nssm.exe" (
    echo !! %HERE%nssm.exe missing — download from https://nssm.cc/download
    exit /b 1
)

REM 1. harvest dynamic file trees.
call "%HERE%heat.cmd" || exit /b 1

REM 2. compile.
candle -nologo -arch x64 ^
       -dVersion=%WIX_VERSION% ^
       -dWebDistSrc=%ROOT%\apps\web\dist ^
       -dWorkerSrc=%ROOT%\apps\agent-worker ^
       -dPyContractsSrc=%ROOT%\packages\py-contracts ^
       Mathodology.wxs auto-web.wxs auto-worker.wxs auto-pycontracts.wxs ^
       || exit /b 1

REM 3. link. WixUIExtension -> InstallDir UI; WixUtilExtension -> EnvironmentVariable.
light  -nologo ^
       -ext WixUIExtension -ext WixUtilExtension ^
       Mathodology.wixobj auto-web.wixobj auto-worker.wixobj auto-pycontracts.wixobj ^
       -o mathodology-%VERSION%.msi ^
       || exit /b 1

echo ==^> built mathodology-%VERSION%.msi  (UNSIGNED — SmartScreen will warn)
endlocal
