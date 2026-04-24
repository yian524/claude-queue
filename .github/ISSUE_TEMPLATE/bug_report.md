---
name: Bug report
about: Something isn't behaving as expected
title: "[bug] "
labels: bug
---

## Environment

Run `claude-q doctor` and paste the output here:

```
(paste doctor output)
```

- Windows build: (run `winver` or `[System.Environment]::OSVersion`)
- Terminal:      (Windows Terminal / cmd.exe / Git Bash / PowerShell / other)
- Claude Code version: (run `claude --version`)

## What happened?

Describe the observed behaviour.

## What did you expect to happen?

Describe the expected behaviour.

## Reproduction steps

1. Start `claude-q ...`
2. Press ...
3. Observe ...

## Debug log (optional but very helpful)

Set `CLAUDE_Q_DEBUG=1` before starting, reproduce the bug, then paste
the last ~40 lines of the log:

```powershell
$env:CLAUDE_Q_DEBUG = "1"
claude-q
# (reproduce bug, then Ctrl+C)
Get-Content "$env:USERPROFILE\.claude\run\claude-q\relay_debug.log" -Tail 40
```

```
(paste log here)
```

## Screenshot

If the issue is visual, a screenshot helps a lot.
