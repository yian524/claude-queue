@echo off
rem claude shim - dispatches `claude -q ...` to the claude-q wrapper;
rem anything else transparently forwards to the real claude.exe.
rem
rem Install: copy this file to a directory that appears EARLIER in your
rem PATH than the directory containing the real claude.exe. The
rem project convention is %USERPROFILE%\.claude\bin\.
rem
rem Note: `shift` inside a parenthesized (if ...) block in Windows batch
rem files does NOT actually shift the arguments until the block exits.
rem We use goto/label flow instead so shift takes effect.

setlocal enabledelayedexpansion

rem Real claude path - adjust if you moved it.
rem (run `where claude` in a shell WITHOUT this shim in PATH to find it)
set "REAL_CLAUDE=%USERPROFILE%\.local\bin\claude.exe"

if /i "%~1"=="-q" goto :queue_route

"%REAL_CLAUDE%" %*
exit /b %errorlevel%


:queue_route
shift
set "ARGS="
:collect_loop
if "%~1"=="" goto :run_wrapper
set "ARGS=!ARGS! %1"
shift
goto :collect_loop

:run_wrapper
"%USERPROFILE%\.claude\scripts\claude-queue\.venv\Scripts\python.exe" "%USERPROFILE%\.claude\scripts\claude-queue\cli.py"!ARGS!
exit /b %errorlevel%
