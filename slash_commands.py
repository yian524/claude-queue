"""slash_commands.py - parse and execute /slash commands in queue input.

Commands
--------

    /wait <duration> <message>
        Queue <message> and dispatch it only after <duration> has elapsed
        AND Claude is idle. Example: /wait 5m please run the tests

    /at <time> <message>
        Queue <message> for dispatch at absolute <time>. Time accepts
        HH:MM, HH:MM:SS, or YYYY-MM-DD HH:MM. Example: /at 14:30 deploy

    /priority <message>
        Queue <message> with priority=100 so it's dispatched before any
        normal-priority entry regardless of order.

    /now <message>
        (DANGEROUS) Dispatch <message> immediately, bypassing the idle
        wait. Will interrupt Claude's current output.

    /cancel
        Exit queue mode without queuing anything.

    /help
        Show the command list (handled by caller via `HelpRequest`).

The parse() function returns one of several Result dataclasses so the
caller can dispatch to the right side-effect.
"""
from __future__ import annotations

import json
import shlex
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Union

import scheduler


# ============================= result types =============================

@dataclass
class QueueRequest:
    """Plain queue: dispatch when Claude is idle."""
    text: str
    dispatch_at: Optional[str] = None   # ISO string, None = ASAP
    priority: int = 0


@dataclass
class ForceSendRequest:
    """/now - skip idle check, write directly to PTY."""
    text: str


@dataclass
class CancelRequest:
    """/cancel - exit queue mode without pushing anything."""


@dataclass
class HelpRequest:
    """/help - show command reference."""


@dataclass
class DropRequest:
    """/drop <N> - drop pending entry at 1-based index N."""
    index: int


@dataclass
class ClearRequest:
    """/qclear - drop all pending queue entries."""


@dataclass
class ClaudeEnterRequest:
    """/claudeenter - prompt the relay to show the native Claude
    command list as the dropdown contents (no other side effect)."""


@dataclass
class ParseError:
    """Malformed command; caller should show the message and stay in queue."""
    message: str


ParseResult = Union[
    QueueRequest, ForceSendRequest, CancelRequest, HelpRequest,
    DropRequest, ClearRequest, ClaudeEnterRequest, ParseError,
]


# ============================= command metadata =============================

# Queue-internal commands. Order matters for display.
# /cancel is intentionally omitted: Esc and Ctrl+Q already cancel.
COMMANDS: list[dict] = [
    {
        "name": "/wait",
        "template": "/wait <duration> <message>",
        "summary": "Dispatch after duration (30s, 5m, 1h30m)",
        "kind": "queue",
    },
    {
        "name": "/at",
        "template": "/at <time> <message>",
        "summary": "Dispatch at absolute time (HH:MM or YYYY-MM-DD HH:MM)",
        "kind": "queue",
    },
    {
        "name": "/priority",
        "template": "/priority <message>",
        "summary": "Jump ahead of normal queue entries",
        "kind": "queue",
    },
    {
        "name": "/now",
        "template": "/now <message>",
        "summary": "WARNING: send immediately, interrupts Claude",
        "kind": "queue",
    },
    {
        "name": "/drop",
        "template": "/drop <N>",
        "summary": "Drop pending entry #N (see the Pending list)",
        "kind": "queue",
    },
    {
        "name": "/qclear",
        "template": "/qclear",
        "summary": "Drop ALL pending queue entries",
        "kind": "queue",
    },
    {
        "name": "/qhelp",
        "template": "/qhelp",
        "summary": "Show this command list",
        "kind": "queue",
    },
]


# ============================= native Claude commands =============================
# These are Claude Code's own slash commands. When the user picks one of
# these from the dropdown, the literal string is queued and dispatched
# to Claude as-is — Claude's TUI then handles the command.
#
# Source: Claude Code 2.1.x release notes + /help output. Maintained by
# hand because there's no programmatic way to enumerate them from the
# binary. Add new ones here as Claude Code ships them.
NATIVE_CLAUDE_COMMANDS: list[dict] = [
    {"name": "/help",          "summary": "Claude's own help"},
    {"name": "/clear",         "summary": "Clear conversation context"},
    {"name": "/compact",       "summary": "Compact context (summarize older turns)"},
    {"name": "/init",          "summary": "Generate CLAUDE.md for this codebase"},
    {"name": "/resume",        "summary": "Resume a previous session"},
    {"name": "/memory",        "summary": "Edit ~/.claude/CLAUDE.md memory"},
    {"name": "/model",         "summary": "Switch model (Opus / Sonnet / Haiku)"},
    {"name": "/config",        "summary": "Open settings"},
    {"name": "/cost",          "summary": "Show token usage / cost"},
    {"name": "/usage",         "summary": "Show 5h / 7d quota"},
    {"name": "/status",        "summary": "Show session status"},
    {"name": "/permissions",   "summary": "Manage tool permissions"},
    {"name": "/agents",        "summary": "List subagents"},
    {"name": "/skills",        "summary": "List available skills"},
    {"name": "/mcp",           "summary": "Manage MCP servers"},
    {"name": "/plan",          "summary": "Toggle plan mode"},
    {"name": "/login",         "summary": "Re-authenticate"},
    {"name": "/logout",        "summary": "Sign out"},
    {"name": "/upgrade",       "summary": "Upgrade Claude Code"},
    {"name": "/release-notes", "summary": "View release notes"},
    {"name": "/export",        "summary": "Export conversation"},
    {"name": "/vim",           "summary": "Toggle vim editing keys"},
]
# Tag every entry with kind="claude" + source="builtin" so the UI/parser
# can distinguish queue-internal vs native, and group native by sub-source.
for _e in NATIVE_CLAUDE_COMMANDS:
    _e["kind"] = "claude"
    _e["source"] = "builtin"
    _e.setdefault("template", _e["name"])

_NATIVE_NAMES = {c["name"] for c in NATIVE_CLAUDE_COMMANDS}


# Special command that exposes the native-claude command list as the
# entire dropdown contents. Useful when the user wants to browse the
# native command set without typing the prefix character.
COMMANDS.append({
    "name": "/claudeenter",
    "template": "/claudeenter",
    "summary": "Browse native Claude /commands; pick one to queue-and-fire",
    "kind": "queue",
})


# Module-level cache of the discovered native commands. Populated lazily
# on first call to discover_native_commands(); cleared via
# _reset_native_cache() in tests.
_native_cache: Optional[list[dict]] = None


def _read_skill_metadata(skill_md: Path) -> Optional[dict]:
    """Parse a SKILL.md file's YAML frontmatter for `name` and
    `description`. Returns None on any parse error so a single bad
    skill file can't poison the dropdown."""
    try:
        text = skill_md.read_text(encoding="utf-8")
    except Exception:
        return None
    if not text.startswith("---"):
        return None
    end = text.find("\n---", 3)
    if end == -1:
        return None
    fm = text[3:end]
    name = None
    desc_lines: list[str] = []
    in_desc = False
    for line in fm.splitlines():
        stripped = line.lstrip()
        if not in_desc and line.startswith("name:"):
            name = line.split(":", 1)[1].strip().strip("'\"")
        elif not in_desc and line.startswith("description:"):
            d = line.split(":", 1)[1].strip()
            if d == "|" or d == ">":
                in_desc = True
            else:
                desc_lines.append(d.strip("'\""))
        elif in_desc:
            if line and not line.startswith(" ") and not line.startswith("\t"):
                in_desc = False
            elif stripped:
                desc_lines.append(stripped)
    if not name:
        return None
    summary = " ".join(desc_lines).strip() or "(no description)"
    if len(summary) > 80:
        summary = summary[:77] + "..."
    return {"name": name, "summary": summary}


def discover_native_commands(cwd: Optional[Path] = None) -> list[dict]:
    """Build the live list of native Claude commands the user can pick
    in queue mode's `//` dropdown. Mirrors what Claude Code's own `/`
    picker shows. Combines:
      1. 22 hardcoded built-in commands (NATIVE_CLAUDE_COMMANDS)
      2. User skills at ~/.claude/skills/<name>/SKILL.md
      3. Plugin commands at ~/.claude/plugins/cache/.../commands/*.md
      4. User-level commands at ~/.claude/commands/*.md
      5. Project-level commands at <cwd>/.claude/commands/*.md

    Each entry has `kind="claude"` plus `source` ∈
    {builtin, skill, plugin, user-cmd, project-cmd} so the UI can
    group/badge them. Results are cached at module level — call
    _reset_native_cache() in tests to refresh.

    `cwd` defaults to `Path.cwd()`. Pass an explicit path in tests
    to control where project-level commands are scanned from.
    """
    global _native_cache
    if _native_cache is not None and cwd is None:
        return list(_native_cache)

    out: list[dict] = []

    # 1) built-in (already tagged kind="claude" source="builtin")
    out.extend({**c} for c in NATIVE_CLAUDE_COMMANDS)

    home = Path.home()

    # 2) user skills
    skills_dir = home / ".claude" / "skills"
    if skills_dir.exists():
        for skill_md in skills_dir.glob("*/SKILL.md"):
            # Defensive: refuse to follow symlinks. Reviewer of
            # commit 3560bd1 noted that a malicious skill dir could
            # symlink SKILL.md to e.g. C:\Windows\System32\config\SAM
            # — even though contents only hit display strings, reading
            # arbitrary files is worth avoiding.
            try:
                if not skill_md.is_file() or skill_md.is_symlink():
                    continue
            except OSError:
                continue
            meta = _read_skill_metadata(skill_md)
            if not meta:
                continue
            out.append({
                "name": f"/{meta['name']}",
                "template": f"/{meta['name']}",
                "summary": meta["summary"],
                "kind": "claude",
                "source": "skill",
            })

    # 3) plugin commands — scan installed_plugins.json then look up
    #    each plugin's commands directory inside the cache.
    installed_path = home / ".claude" / "plugins" / "installed_plugins.json"
    cache_root = home / ".claude" / "plugins" / "cache"
    if installed_path.exists() and cache_root.exists():
        try:
            data = json.loads(installed_path.read_text(encoding="utf-8"))
        except Exception:
            data = None
        if data and "plugins" in data:
            seen_plugin_cmds: set[str] = set()  # dedupe across version dirs
            for plugin_key in data["plugins"].keys():
                # plugin_key looks like "code-review@claude-plugins-official"
                if "@" not in plugin_key:
                    continue
                plugin_name, repo = plugin_key.split("@", 1)
                # commands live in cache/<repo>/<plugin>/<version>/commands/
                # multiple version dirs may coexist (`unknown`, git-sha,
                # semver); the same command file appears in each. Dedupe
                # by qualified name so the dropdown stays clean.
                pdir = cache_root / repo / plugin_name
                if not pdir.exists():
                    continue
                for cmd_md in pdir.glob("*/commands/*.md"):
                    try:
                        if not cmd_md.is_file() or cmd_md.is_symlink():
                            continue
                    except OSError:
                        continue
                    cmd_name = cmd_md.stem
                    qualified = f"/{plugin_name}:{cmd_name}"
                    if qualified in seen_plugin_cmds:
                        continue
                    seen_plugin_cmds.add(qualified)
                    summary = _read_command_summary(cmd_md)
                    out.append({
                        "name": qualified,
                        "template": qualified,
                        "summary": summary,
                        "kind": "claude",
                        "source": "plugin",
                    })

    # 4) user-level commands at ~/.claude/commands/*.md — these are
    #    custom slash commands the user has authored at the user scope
    #    (visible in every Claude Code session globally). Claude's
    #    native picker surfaces them as `/<filename-without-extension>`.
    user_cmd_dir = home / ".claude" / "commands"
    if user_cmd_dir.exists():
        for cmd_md in user_cmd_dir.glob("*.md"):
            try:
                if not cmd_md.is_file() or cmd_md.is_symlink():
                    continue
            except OSError:
                continue
            cmd_name = cmd_md.stem
            summary = _read_command_summary(cmd_md)
            out.append({
                "name": f"/{cmd_name}",
                "template": f"/{cmd_name}",
                "summary": summary,
                "kind": "claude",
                "source": "user-cmd",
            })

    # 5) project-level commands at <cwd>/.claude/commands/*.md —
    #    same as #4 but only visible in this project. Mirrors
    #    Claude's behaviour where a `/codex` defined under
    #    `~/Desktop/碩論/.claude/commands/codex.md` only shows up
    #    when running Claude inside that project.
    project_root = cwd if cwd is not None else Path.cwd()
    project_cmd_dir = project_root / ".claude" / "commands"
    if project_cmd_dir.exists():
        for cmd_md in project_cmd_dir.glob("*.md"):
            try:
                if not cmd_md.is_file() or cmd_md.is_symlink():
                    continue
            except OSError:
                continue
            cmd_name = cmd_md.stem
            summary = _read_command_summary(cmd_md)
            out.append({
                "name": f"/{cmd_name}",
                "template": f"/{cmd_name}",
                "summary": summary,
                "kind": "claude",
                "source": "project-cmd",
            })

    if cwd is None:
        # only cache the "real" production discovery, not test-injected ones
        _native_cache = out
    return list(out)


def _read_command_summary(cmd_md: Path) -> str:
    """Pull a 1-line summary out of a plugin command's frontmatter
    (description) or the first non-frontmatter line."""
    try:
        text = cmd_md.read_text(encoding="utf-8")
    except Exception:
        return "(plugin command)"
    if text.startswith("---"):
        end = text.find("\n---", 3)
        if end != -1:
            for line in text[3:end].splitlines():
                if line.startswith("description:"):
                    s = line.split(":", 1)[1].strip().strip("'\"")
                    if s and s not in ("|", ">"):
                        return s if len(s) <= 80 else s[:77] + "..."
            text = text[end + 4:]
    for line in text.splitlines():
        s = line.strip()
        if s and not s.startswith("#"):
            return s if len(s) <= 80 else s[:77] + "..."
    return "(plugin command)"


def _reset_native_cache() -> None:
    """Test helper: drop the module-level cache so a fresh
    discover_native_commands() call re-scans the filesystem."""
    global _native_cache
    _native_cache = None


def filter_commands(prefix: str, claude_picker: bool = False) -> list[dict]:
    """Return commands whose name starts with `prefix` (case-insensitive).

    `claude_picker=False` (default — single-`/` dropdown):
        Returns ONLY queue-internal commands (kind="queue"). User typed
        `/wait` etc.

    `claude_picker=True` (double-`//` dropdown):
        Returns native Claude commands — built-in + skills + plugins
        (kind="claude"). Picking one queues the literal string for
        Claude's TUI to execute on dispatch.

    The two pools are kept separate so the dropdown stays readable.
    For prior-version code that called this with no flag, behaviour
    is preserved: only queue-internal commands surface on `/`.
    """
    if claude_picker:
        pool = discover_native_commands()
    else:
        pool = COMMANDS
    if not prefix:
        return list(pool)
    p = prefix.lower()
    return [c for c in pool if c["name"].lower().startswith(p)]


def native_commands() -> list[dict]:
    """Return the full list of native Claude commands (used when the
    user invokes /claudeenter or `//` to expose them all at once)."""
    return discover_native_commands()


def is_native_claude_command(line: str) -> bool:
    """True if `line` starts with one of Claude Code's own /commands."""
    if not line or not line.startswith("/"):
        return False
    cmd = line.split(None, 1)[0].lower()
    return cmd in _NATIVE_NAMES


# ============================= parser =============================

def parse(raw: str) -> ParseResult:
    """Parse the user's raw input line (after Enter).

    If the first token is not a /command, returns a plain QueueRequest
    with the original text. Otherwise dispatches to the right result.
    """
    if not raw or not raw.strip():
        return ParseError("empty input; type something or Esc to cancel")

    stripped = raw.strip()
    if not stripped.startswith("/"):
        return QueueRequest(text=stripped)

    # `//xxx` is the "Claude native command" namespace in queue mode —
    # the user typed two slashes to open the native picker. Normalize
    # to a single slash here so it dispatches as `/xxx` to Claude's
    # TUI on idle. Also covers the case where the user hand-types
    # `//foo` without using the dropdown.
    if stripped.startswith("//"):
        return QueueRequest(text=stripped[1:])

    # split into /cmd + rest
    try:
        parts = stripped.split(None, 1)
    except ValueError:
        return ParseError("malformed command")
    cmd = parts[0].lower()
    rest = parts[1] if len(parts) > 1 else ""

    if cmd == "/qhelp":
        return HelpRequest()
    if cmd == "/claudeenter":
        return ClaudeEnterRequest()
    if cmd == "/priority":
        if not rest.strip():
            return ParseError("/priority needs a message")
        return QueueRequest(text=rest.strip(), priority=100)
    if cmd == "/now":
        if not rest.strip():
            return ParseError("/now needs a message")
        return ForceSendRequest(text=rest.strip())
    if cmd == "/wait":
        return _parse_wait(rest)
    if cmd == "/at":
        return _parse_at(rest)
    if cmd == "/drop":
        return _parse_drop(rest)
    if cmd == "/qclear":
        return ClearRequest()

    # Native Claude commands AND any unknown /commands fall through to
    # plain queue: the literal string is queued and dispatched as-is.
    # Claude's own TUI handles /clear, /help, /init, /compact, etc.,
    # plus any new commands Claude Code ships in the future without us
    # needing to update this list.
    return QueueRequest(text=stripped)


def _parse_drop(rest: str) -> ParseResult:
    if not rest.strip():
        return ParseError("/drop needs a pending index (e.g. /drop 1)")
    try:
        idx = int(rest.strip())
    except ValueError:
        return ParseError(f"/drop: {rest.strip()!r} is not a valid index")
    if idx < 1:
        return ParseError("/drop: index must be >= 1")
    return DropRequest(index=idx)


def _parse_wait(rest: str) -> ParseResult:
    """Parse '<duration> <message>' for /wait."""
    if not rest.strip():
        return ParseError("/wait needs a duration and a message")
    try:
        dur, msg = rest.split(None, 1)
    except ValueError:
        return ParseError("/wait needs a message after the duration")
    if not msg.strip():
        return ParseError("/wait needs a message after the duration")
    try:
        dispatch_at = scheduler.dispatch_at_from_wait(dur)
    except scheduler.ScheduleParseError as e:
        return ParseError(str(e))
    return QueueRequest(text=msg.strip(), dispatch_at=dispatch_at)


def _parse_at(rest: str) -> ParseResult:
    """Parse '<time> <message>' for /at.

    Time may contain a space (YYYY-MM-DD HH:MM) so we can't naively split.
    Strategy: try increasing split points; first one that parses as time wins.
    """
    if not rest.strip():
        return ParseError("/at needs a time and a message")
    tokens = rest.split()
    # try absorbing 1..n tokens as the time, rest as message
    for cut in range(1, len(tokens)):
        time_str = " ".join(tokens[:cut])
        msg = " ".join(tokens[cut:]).strip()
        if not msg:
            continue
        try:
            dispatch_at = scheduler.dispatch_at_from_at(time_str)
            return QueueRequest(text=msg, dispatch_at=dispatch_at)
        except scheduler.ScheduleParseError:
            continue
    return ParseError("/at: could not parse time. "
                      "Try /at 14:30 <msg> or /at 2026-04-25 14:30 <msg>")


# ============================= self-test =============================

def _self_test() -> int:
    # plain text
    r = parse("hello world")
    assert isinstance(r, QueueRequest) and r.text == "hello world"
    assert r.dispatch_at is None and r.priority == 0

    # /qhelp = queue help (was /help, renamed to avoid shadowing Claude's /help)
    assert isinstance(parse("/qhelp"), HelpRequest)
    # /help is now passed through to Claude as a plain queue entry
    r = parse("/help")
    assert isinstance(r, QueueRequest) and r.text == "/help"
    # /cancel was removed in v0.3.1 — Esc / Ctrl+Q handle cancellation
    # (now passes through as plain queue entry too)
    r = parse("/cancel")
    assert isinstance(r, QueueRequest) and r.text == "/cancel"

    # /priority
    r = parse("/priority pick me first")
    assert isinstance(r, QueueRequest) and r.text == "pick me first" and r.priority == 100
    assert isinstance(parse("/priority"), ParseError)

    # /now
    r = parse("/now urgent")
    assert isinstance(r, ForceSendRequest) and r.text == "urgent"
    assert isinstance(parse("/now"), ParseError)

    # /wait
    r = parse("/wait 5m do the thing")
    assert isinstance(r, QueueRequest) and r.text == "do the thing"
    assert r.dispatch_at is not None and len(r.dispatch_at) >= 19
    assert isinstance(parse("/wait"), ParseError)
    assert isinstance(parse("/wait garbage hello"), ParseError)
    assert isinstance(parse("/wait 5m"), ParseError)  # no message

    # /at HH:MM
    r = parse("/at 23:59 end of day")
    assert isinstance(r, QueueRequest) and r.text == "end of day"
    assert r.dispatch_at is not None

    # /at YYYY-MM-DD HH:MM (space inside time)
    r = parse("/at 2099-01-01 00:00 new year")
    assert isinstance(r, QueueRequest) and r.text == "new year", r

    # /at bad
    assert isinstance(parse("/at"), ParseError)
    assert isinstance(parse("/at 25:99 msg"), ParseError)  # invalid time
    assert isinstance(parse("/at 14:30"), ParseError)  # no message

    # unknown /command — now queued as plain text (Claude will handle it
    # if it's a valid Claude command, or reject otherwise). This means
    # our list doesn't need to keep up with every new Claude command.
    r = parse("/foobar")
    assert isinstance(r, QueueRequest) and r.text == "/foobar"

    # native Claude commands queue cleanly with their literal string
    for native in ("/clear", "/compact", "/init", "/model", "/cost"):
        r = parse(native)
        assert isinstance(r, QueueRequest), f"{native} should QueueRequest, got {r}"
        assert r.text == native

    # /drop
    r = parse("/drop 2")
    assert isinstance(r, DropRequest) and r.index == 2
    assert isinstance(parse("/drop"), ParseError)
    assert isinstance(parse("/drop abc"), ParseError)
    assert isinstance(parse("/drop 0"), ParseError)

    # /qclear (was /clear; renamed so Claude's /clear passes through)
    assert isinstance(parse("/qclear"), ClearRequest)
    # bare /clear is now a Claude command that gets queued
    r = parse("/clear")
    assert isinstance(r, QueueRequest) and r.text == "/clear"

    # /claudeenter triggers the dropdown-of-native-commands UI
    assert isinstance(parse("/claudeenter"), ClaudeEnterRequest)

    # empty
    assert isinstance(parse(""), ParseError)
    assert isinstance(parse("   "), ParseError)

    # filter_commands — v0.4.17 single `/` only returns queue commands;
    # `//` (claude_picker=True) returns native commands.
    queue_only = filter_commands("")
    assert len(queue_only) == len(COMMANDS), (
        f"single-slash should only return queue commands; "
        f"got {len(queue_only)} vs expected {len(COMMANDS)}"
    )
    assert all(c.get("kind") == "queue" for c in queue_only)

    # /w prefix only matches queue /wait
    w = filter_commands("/w")
    assert any(c["name"] == "/wait" for c in w)
    assert all(c.get("kind") == "queue" for c in w)

    # /c prefix in queue mode only matches /claudeenter (no native pollution)
    c = filter_commands("/c")
    assert {x["name"] for x in c} == {"/claudeenter"}, (
        f"single-slash /c should only show /claudeenter; got {[x['name'] for x in c]}"
    )

    # native picker (claude_picker=True) returns native commands
    native = filter_commands("", claude_picker=True)
    builtin_only = [n for n in native if n.get("source") == "builtin"]
    assert len(builtin_only) == len(NATIVE_CLAUDE_COMMANDS)

    # /c prefix in claude_picker mode matches /clear /compact /config /cost
    nc = filter_commands("/c", claude_picker=True)
    nc_names = {x["name"] for x in nc}
    assert {"/clear", "/compact", "/config", "/cost"}.issubset(nc_names)

    assert len(filter_commands("/xyz")) == 0
    assert len(filter_commands("/xyz", claude_picker=True)) == 0

    # `//` namespace — parse() normalizes to single slash so dispatch
    # delivers `/clear` (Claude's command) not `//clear` (literal text).
    r = parse("//clear")
    assert isinstance(r, QueueRequest) and r.text == "/clear"
    r = parse("//expert-roundtable some question")
    assert isinstance(r, QueueRequest) and r.text == "/expert-roundtable some question"

    # is_native_claude_command()
    assert is_native_claude_command("/clear")
    assert is_native_claude_command("/init now please")
    assert not is_native_claude_command("/wait 5m do thing")
    assert not is_native_claude_command("plain text")

    # discover_native_commands cache helper
    _reset_native_cache()
    discovered = discover_native_commands()
    assert len(discovered) >= len(NATIVE_CLAUDE_COMMANDS), (
        f"discovery should yield at least {len(NATIVE_CLAUDE_COMMANDS)} "
        f"built-in commands; got {len(discovered)}"
    )
    builtin_count = sum(1 for c in discovered if c.get("source") == "builtin")
    assert builtin_count == len(NATIVE_CLAUDE_COMMANDS)

    print("slash_commands.py self-test: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(_self_test())
