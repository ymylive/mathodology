@echo off
REM Harvest large file trees into auto-*.wxs ComponentGroups.
REM Run from installer\windows\ (cwd) before build-msi.cmd.
REM Requires WiX 3.x on PATH (heat.exe).

setlocal
set ROOT=%~dp0..\..
set HERE=%~dp0

REM Always uses heat.exe explicitly — bare `heat` would resolve to a sibling
REM .cmd (PATHEXT puts CWD ahead of PATH) and recurse infinitely.

REM Web SPA bundle — variable WebDistSrc resolved at candle time via -d.
heat.exe dir "%ROOT%\apps\web\dist" ^
    -cg WebDistGroup -gg -scom -sreg -srd -sfrag ^
    -dr WEBDIR -var var.WebDistSrc ^
    -out "%HERE%auto-web.wxs" || exit /b 1

REM Worker source tree (Python). Excludes via heat -t XSL transform if needed;
REM here we rely on CI staging a clean tree (no .venv, no __pycache__).
heat.exe dir "%ROOT%\apps\agent-worker" ^
    -cg WorkerGroup -gg -scom -sreg -srd -sfrag ^
    -dr WORKERDIR -var var.WorkerSrc ^
    -out "%HERE%auto-worker.wxs" || exit /b 1

REM Python contracts package.
heat.exe dir "%ROOT%\packages\py-contracts" ^
    -cg PyContractsGroup -gg -scom -sreg -srd -sfrag ^
    -dr PYCDIR -var var.PyContractsSrc ^
    -out "%HERE%auto-pycontracts.wxs" || exit /b 1

echo ==^> harvested: auto-web.wxs auto-worker.wxs auto-pycontracts.wxs
endlocal
