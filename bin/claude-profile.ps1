# Microsoft.PowerShell_profile.ps1
# Managed by claude-q installer (2026-04-24)
#
# This PROFILE loads automatically on every PowerShell startup.
# PowerShell functions take priority over PATH-resolved executables,
# so defining `claude` here cleanly intercepts all `claude ...`
# invocations without any PATH ordering tricks.

function claude {
    $realClaude = "$env:USERPROFILE\.local\bin\claude.exe"
    $wrapperPy  = "$env:USERPROFILE\.claude\scripts\claude-queue\.venv\Scripts\python.exe"
    $wrapperCli = "$env:USERPROFILE\.claude\scripts\claude-queue\cli.py"

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
