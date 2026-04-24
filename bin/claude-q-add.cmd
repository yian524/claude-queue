@echo off
rem claude-q-add - enqueue a message into the active claude-q session
rem (useful from a second terminal; same as `claude-q add ...`)

"%USERPROFILE%\.claude\scripts\claude-queue\.venv\Scripts\python.exe" "%USERPROFILE%\.claude\scripts\claude-queue\cli.py" add %*
