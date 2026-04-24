# claude wrapper function - append this to your PowerShell $PROFILE
# Managed by claude-q installer (2026-04-24)
#
# Why in $PROFILE: PowerShell functions take priority over anything
# resolved via $PATH, so this cleanly intercepts `claude ...`
# invocations without requiring fragile PATH ordering.

function claude {
    # Path to the claude-q wrapper (adjust if you cloned elsewhere)
    $wrapperPy  = "$env:USERPROFILE\.claude\scripts\claude-queue\.venv\Scripts\python.exe"
    $wrapperCli = "$env:USERPROFILE\.claude\scripts\claude-queue\cli.py"

    # Resolve the real claude.exe dynamically so this profile works
    # regardless of where the user installed Claude Code (pipx, manual,
    # winget, etc.). Skip any hit that would re-invoke this function
    # via a claude.cmd shim inside the claude-queue project.
    $realClaude = Get-Command -Name claude.exe -CommandType Application -ErrorAction SilentlyContinue |
                  Where-Object { $_.Source -notlike '*claude-queue*' -and $_.Source -notlike '*claude-q\bin*' } |
                  Select-Object -First 1 -ExpandProperty Source

    if (-not $realClaude) {
        Write-Host "[claude-q] ERROR: could not find claude.exe on PATH." -ForegroundColor Red
        Write-Host "           Install Claude Code CLI first:" -ForegroundColor Red
        Write-Host "           https://github.com/anthropics/claude-code" -ForegroundColor Red
        return
    }

    if ($args.Count -gt 0 -and $args[0] -eq '-q') {
        if ($args.Count -eq 1) {
            & $wrapperPy $wrapperCli
        } else {
            $rest = @($args[1..($args.Count - 1)])
            & $wrapperPy $wrapperCli @rest
        }
    }
    else {
        if ($args.Count -eq 0) {
            & $realClaude
        } else {
            & $realClaude @args
        }
    }
}
