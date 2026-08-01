"""Turn raw stream-json events into the compact entries the dashboard shows.

Pure, so it is testable against captured event shapes exactly the way
loop.decide() is. Raw output is unreadable on a phone: a single file read can
be tens of thousands of characters and would bury the prose entirely.
"""

from __future__ import annotations

SUMMARY_KEYS = ("file_path", "command", "pattern", "path", "url", "query", "prompt")
"""Tool inputs, in the order worth showing. The first one present becomes the
tool call's one-line summary."""

PREVIEW_CHARS = 400
SUMMARY_CHARS = 120


def _clip(text: str, limit: int) -> str:
    text = str(text)
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _to_float(value) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _to_int(value) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _tool_summary(tool_input) -> str:
    if not isinstance(tool_input, dict):
        return ""
    for key in SUMMARY_KEYS:
        value = tool_input.get(key)
        if isinstance(value, str) and value.strip():
            return _clip(" ".join(value.split()), SUMMARY_CHARS)
    return ""


def _flatten(content) -> str:
    """tool_result content is either a string or a list of typed blocks."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            str(block.get("text") or "")
            for block in content
            if isinstance(block, dict) and block.get("type") == "text"
        )
    return ""


def _assistant_block(block: dict) -> dict | None:
    kind = block.get("type")
    if kind == "text":
        text = str(block.get("text") or "").strip()
        return {"kind": "text", "text": text} if text else None
    if kind == "tool_use":
        return {
            "kind": "tool",
            "id": str(block.get("id") or ""),
            "name": str(block.get("name") or "tool"),
            "summary": _tool_summary(block.get("input")),
        }
    return None


def _user_block(block: dict) -> dict | None:
    if block.get("type") != "tool_result":
        return None
    return {
        "kind": "result",
        "id": str(block.get("tool_use_id") or ""),
        "preview": _clip(_flatten(block.get("content")), PREVIEW_CHARS),
        "is_error": bool(block.get("is_error")),
    }


def _blocks(event: dict, render_block) -> list[dict]:
    msg = event.get("message")
    content = msg.get("content") if isinstance(msg, dict) else None
    if not isinstance(content, list):
        return []
    entries = []
    for block in content:
        if isinstance(block, dict):
            entry = render_block(block)
            if entry:
                entries.append(entry)
    return entries


def render_event(event: dict) -> list[dict]:
    """Zero or more display entries for one raw event.

    A list rather than a single entry because one assistant message routinely
    carries prose and several tool calls in the same content array.
    """
    kind = event.get("type")
    if kind == "assistant":
        return _blocks(event, _assistant_block)
    if kind == "user":
        return _blocks(event, _user_block)
    if kind == "result":
        return [
            {
                "kind": "done",
                "cost": _to_float(event.get("total_cost_usd") or 0.0),
                "duration_ms": _to_int(event.get("duration_ms") or 0),
                "subtype": str(event.get("subtype") or ""),
            }
        ]
    return []
