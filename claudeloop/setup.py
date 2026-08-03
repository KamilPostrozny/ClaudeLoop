"""Setup mode: a loopback-only server that writes config.toml, and the TOML
emitter behind it.

It runs only when the loop does not. There is no shared state with the loop
at all, which is why this can write a file the dashboard's own rules would
forbid.
"""

from __future__ import annotations

import json
import re

from .config import SCHEMA

_BARE_KEY = re.compile(r"^[A-Za-z0-9_-]+$")


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
    # JSON's escape set covers TOML's basic string for ", \ and U+0000..U+001F,
    # but the two disagree at both ends of the range. ensure_ascii=True encodes
    # a non-BMP character as a surrogate pair, which TOML rejects outright as
    # not a Unicode scalar value -- and one emoji in a [session_env] value then
    # makes tomllib fail on the whole file, so the config the wizard just wrote
    # cannot be read back. ensure_ascii=False fixes that and opens the other
    # end: it emits U+007F raw, which TOML forbids in a basic string. That one
    # character is the entire remaining gap.
    return json.dumps(str(value), ensure_ascii=False).replace("\x7f", "\\u007f")


def _key(name: str) -> str:
    """A bare key where TOML allows one, a quoted key otherwise.

    [session_env] names are operator input, not schema data: a space or a
    quote in one would break the whole file, and a dot would silently parse
    as a nested table instead of the name the operator typed.
    """
    return name if _BARE_KEY.match(name) else _scalar(name)


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
    table is documented in every file written afterwards for free. Any key
    in `data` not named in SCHEMA, and any section other than the top level,
    "jira" and "session_env", is silently dropped -- SCHEMA is the single
    source of truth for what gets written.
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
            out.append(f"{_key(name)} = {_scalar(value)}")
        out.append("")
    return "\n".join(out).rstrip() + "\n"
