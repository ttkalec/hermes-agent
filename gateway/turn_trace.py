from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, Iterable
import uuid


_TRACE_FIELDS: tuple[str, ...] = (
    "received_at",
    "batch_flushed_at",
    "first_typing_sent_at",
    "first_tool_started_at",
    "first_model_call_at",
    "final_response_sent_at",
)


def _now_like(event: Any) -> datetime:
    timestamp = getattr(event, "timestamp", None)
    tzinfo = getattr(timestamp, "tzinfo", None)
    if tzinfo is not None:
        return datetime.now(tz=tzinfo)
    return datetime.now()


def ensure_turn_metadata(event: Any) -> Any:
    if not getattr(event, "correlation_id", None):
        event.correlation_id = uuid.uuid4().hex[:8]
    if getattr(event, "turn_phase", None) is None:
        event.turn_phase = "received"
    trace = getattr(event, "trace", None)
    if trace is None:
        trace = {}
        event.trace = trace
    if "received_at" not in trace:
        trace["received_at"] = getattr(event, "timestamp", None) or _now_like(event)
    return event


def record_timestamp(event: Any, field_name: str, when: datetime | None = None) -> datetime:
    ensure_turn_metadata(event)
    if field_name in event.trace:
        return event.trace[field_name]
    value = when or _now_like(event)
    event.trace[field_name] = value
    return value


def set_phase(event: Any, phase: str) -> str:
    ensure_turn_metadata(event)
    event.turn_phase = phase
    return phase


def _format_value(value: Any) -> str:
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def iter_trace_fields(event: Any) -> Iterable[tuple[str, Any]]:
    ensure_turn_metadata(event)
    for name in _TRACE_FIELDS:
        value = event.trace.get(name)
        if value is not None:
            yield name, value


def format_turn_snapshot(event: Any) -> str:
    ensure_turn_metadata(event)
    parts = [f"phase={getattr(event, 'turn_phase', 'received')}"]
    parts.extend(f"{name}={_format_value(value)}" for name, value in iter_trace_fields(event))
    return " ".join(parts)


def slow_turn_warning_message(event: Any, elapsed_seconds: float) -> str:
    ensure_turn_metadata(event)
    return f"slow_turn elapsed={elapsed_seconds:.1f}s {format_turn_snapshot(event)}"


def turn_log_fields(event: Any) -> Dict[str, str]:
    ensure_turn_metadata(event)
    return {name: _format_value(value) for name, value in iter_trace_fields(event)}
