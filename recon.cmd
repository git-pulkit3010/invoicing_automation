@echo off
REM Convenience launcher so you do not have to type the venv path.
REM Usage:  recon.cmd reconcile
REM         recon.cmd reconcile --format json
REM         recon.cmd timesheet --name Harish
REM         recon.cmd workorders
REM         recon.cmd parse invoices\Some_Invoice.pdf

setlocal
set "HERE=%~dp0"
if not exist "%HERE%.venv\Scripts\python.exe" (
    echo Virtual environment not found at "%HERE%.venv"
    echo Create it with:  python -m venv .venv
    echo Then install with:  .venv\Scripts\python -m pip install -r requirements.txt
    exit /b 2
)
"%HERE%.venv\Scripts\python.exe" -m recon %*
endlocal
