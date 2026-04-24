# Contributing to claude-q

Thanks for considering a contribution.

## Before you open a PR

1. **Open an issue first** for anything non-trivial. It's much easier to
   align on scope before code lands.
2. **Keep the scope tight.** `claude-q` is a single-purpose tool
   (type-ahead queue wrapper). Features that drift outside that scope
   are better off as a separate tool that uses `claude-q` as a building
   block.
3. **Run the tests.** `pytest tests/` and every module's `python <file>.py`
   self-test must stay green.
4. **Mind the dependency budget.** Current runtime deps: `pywinpty`,
   `prompt_toolkit`. Adding another means justifying it in the PR.

## Dev setup

```powershell
git clone <your fork>
cd claude-queue
python -m venv .venv
.venv\Scripts\python.exe -m pip install pywinpty prompt_toolkit pytest
```

Every module has a `_self_test()` you can run with
`python <module>.py`. End-to-end tests live in `tests/`.

## Code style

* PEP 8 (4-space indent, 100-col soft limit).
* Type hints on public functions.
* Short docstrings; prefer a module-level header comment for design
  rationale over doc-ing every helper.
* No emoji in source code (they break some Windows code pages).

## Commit message convention

Use this format:

```
YYMMDD-claude-queue-<short description in 繁中 or English>

<optional body>

Co-Authored-By: ...
```

Example:

```
260424-claude-queue-alt-screen-UI重構解決banner堆疊

...
```

## Windows-only scope (for now)

POSIX support is welcome but needs:

1. A POSIX equivalent of `win_console_input.py` (termios-based).
2. Swap `pywinpty` for `ptyprocess` (or use the same interface).
3. Validate alt-screen codes work on Linux + macOS (they do, but please
   double-check on Terminal.app and gnome-terminal).

A separate `input_posix.py` + runtime `sys.platform` dispatch is the
cleanest design.

## Security

If you find a vulnerability, please **don't** file a public issue.
Email the maintainer directly (see `README.md` credits section).
