from __future__ import annotations

import json
import re
from typing import Any, Dict, Iterable, Optional

_HINT_LINE_RE = re.compile(r"^\[Hint: .*?\]$", re.MULTILINE)
_WHITESPACE_RE = re.compile(r"\s+")


def _normalize_text(value: Any) -> str:
    text = str(value or "")
    text = _HINT_LINE_RE.sub("", text)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    return text.strip()


def _single_line(text: str) -> str:
    return _WHITESPACE_RE.sub(" ", (text or "").strip())


def _truncate(text: str, max_chars: int) -> str:
    text = (text or "").strip()
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 12].rstrip() + " …[omitted]"


def _try_json(text: str) -> Optional[Any]:
    text = (text or "").strip()
    if not text or text[0] not in "[{":
        return None
    try:
        return json.loads(text)
    except Exception:
        return None


def summarize_tool_content(tool_name: Optional[str], content: Any, *, max_chars: int = 220) -> str:
    tool = str(tool_name or "tool").strip() or "tool"
    text = _normalize_text(content)
    if not text:
        return f"{tool} returned no visible output"

    payload = _try_json(text)
    if isinstance(payload, dict):
        if "todos" in payload and isinstance(payload.get("summary"), dict):
            summary = payload["summary"]
            total = int(summary.get("total") or len(payload.get("todos") or []))
            completed = int(summary.get("completed") or 0)
            pending = int(summary.get("pending") or 0)
            in_progress = int(summary.get("in_progress") or 0)
            parts = [f"todo list updated ({total} items"]
            if completed:
                parts.append(f"{completed} completed")
            if in_progress:
                parts.append(f"{in_progress} in progress")
            if pending:
                parts.append(f"{pending} pending")
            return "; ".join(parts) + ")"

        if "total_count" in payload and "matches" in payload:
            total = payload.get("total_count")
            truncated = bool(payload.get("truncated"))
            suffix = " (truncated)" if truncated else ""
            return f"{tool} returned {total} matches{suffix}"

        if "total_count" in payload and "files" in payload:
            total = payload.get("total_count")
            truncated = bool(payload.get("truncated"))
            suffix = " (truncated)" if truncated else ""
            return f"{tool} returned {total} files{suffix}"

        if {"content", "total_lines"}.issubset(payload.keys()):
            total_lines = payload.get("total_lines")
            truncated = bool(payload.get("truncated"))
            suffix = " (truncated)" if truncated else ""
            return f"{tool} returned file content ({total_lines} lines){suffix}"

        if "output" in payload and "exit_code" in payload:
            exit_code = payload.get("exit_code")
            error = _single_line(str(payload.get("error") or ""))
            output = _single_line(str(payload.get("output") or ""))
            if exit_code not in (0, "0", None):
                detail = error or output or "no further details"
                return f"{tool} failed with exit code {exit_code}: {_truncate(detail, max_chars)}"
            if output:
                return f"{tool} succeeded: {_truncate(output, max_chars)}"
            return f"{tool} succeeded with exit code 0"

        if payload.get("success") is False and payload.get("error"):
            return f"{tool} error: {_truncate(_single_line(str(payload['error'])), max_chars)}"

        if payload.get("success") is True:
            remaining = [k for k in payload.keys() if k != "success"]
            if not remaining:
                return f"{tool} succeeded"

    line = _single_line(text)
    if not line:
        return f"{tool} returned no visible output"
    if len(line) > max_chars:
        return f"{tool} output omitted ({len(line)} chars)"
    return line


def summarize_assistant_tool_calls(tool_calls: Any) -> Optional[str]:
    if not isinstance(tool_calls, Iterable) or isinstance(tool_calls, (str, bytes, dict)):
        return None
    names = []
    for tc in tool_calls:
        if not isinstance(tc, dict):
            continue
        name = tc.get("name") or tc.get("function", {}).get("name")
        if name:
            names.append(str(name))
    if not names:
        return None
    return f"Called: {', '.join(names)}"
