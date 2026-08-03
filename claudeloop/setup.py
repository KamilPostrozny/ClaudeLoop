"""Setup mode: a loopback-only server that writes config.toml, and the TOML
emitter behind it.

It runs only when the loop does not. There is no shared state with the loop
at all, which is why this can write a file the dashboard's own rules would
forbid.
"""

from __future__ import annotations

import json

from .config import SCHEMA


def _scalar(value: object) -> str:
    """One TOML value.

    Strings go through json.dumps: TOML's basic string accepts JSON's escape
    set, so quotes, backslashes, tabs and control characters are all handled
    by the stdlib rather than by hand.
    """
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return repr(value)
    return json.dumps(str(value))


def _blank(value: object) -> bool:
    """Whether to leave the key out entirely.

    An emitted `settings_file = ""` reads back as a path that does not exist,
    and load_config would then refuse the file this module just wrote. False
    and 0 are real values, not blanks.
    """
    return value is None or (isinstance(value, str) and not value.strip())


def _wrap(text: str, width: int = 74) -> list[str]:
    lines, current = [], ""
    for word in text.split():
        if current and len(current) + len(word) + 1 > width:
            lines.append(current)
            current = word
        else:
            current = f"{current} {word}".strip()
    if current:
        lines.append(current)
    return lines


def dump_toml(data: dict) -> str:
    """`data` in the config.toml shape, as the text of that file.

    Key order and the comments both come from SCHEMA, so a key added to the
    table is documented in every file written afterwards for free.
    """
    out: list[str] = ["# Written by ClaudeLoop's setup wizard.",
                      "# Re-run it with: python -m claudeloop --setup", ""]
    for section in ("", "jira"):
        fields = [f for f in SCHEMA if f.section == section]
        table = data.get(section) if section else data
        if not isinstance(table, dict):
            continue
        emitted = [f for f in fields if not _blank(table.get(f.name))]
        if not emitted:
            continue
        if section:
            out.append(f"[{section}]")
        for field in emitted:
            for line in _wrap(field.help):
                out.append(f"# {line}")
            out.append(f"{field.name} = {_scalar(table[field.name])}")
            out.append("")
    env = data.get("session_env")
    if isinstance(env, dict) and env:
        out.append("# Extra environment variables for every session.")
        out.append("[session_env]")
        for name, value in env.items():
            out.append(f"{name} = {_scalar(value)}")
        out.append("")
    return "\n".join(out).rstrip() + "\n"
