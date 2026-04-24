@echo off
rem claude-q - type-ahead FIFO queue wrapper for Claude Code CLI
rem
rem Copy this file to a directory on your PATH (e.g. ~/.claude/bin/)
rem or add this directory to PATH so you can run `claude-q` globally.
rem
rem Assumes the package lives at %USERPROFILE%\.claude\scripts\claude-queue\
rem with a dedicated venv at .venv\. Adjust the two paths below if your
rem layout differs.

"%USERPROFILE%\.claude\scripts\claude-queue\.venv\Scripts\python.exe" "%USERPROFILE%\.claude\scripts\claude-queue\cli.py" %*
