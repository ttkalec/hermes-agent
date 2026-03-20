"""
Comprehensive activity logging for Hermes Agent.

Provides a unified "log everything" system that captures every significant event
(tool calls, model calls, errors, cron jobs, gateway events) to two destinations:

1. SQLite `activity_log` table in state.db — queryable, structured, great for
   "what happened last night?" questions.
2. JSONL file at ~/.hermes/logs/activity.jsonl — append-only raw log, cheap backup,
   easy for AI to parse line-by-line.

Both outputs use the same event schema. The SQLite table is indexed for fast
time-range and level-based queries. The JSONL file is rotated at 50MB.

Usage:
    from agent.activity_logger import activity_logger

    activity_logger.log_tool_call("web_search", {"query": "test"}, "results...", 1.23, session_id="abc")
    activity_logger.log_model_call("claude-opus-4.6", 1500, 300, 2.1, cost_usd=0.05, session_id="abc")
    activity_logger.log_error("cron", "Calendar fetch failed", traceback_str, session_id="abc")
    activity_logger.log_event("gateway", "Platform connected", {"platform": "telegram"})
"""

import json
import logging
import os
import sqlite3
import threading
import time
import traceback as tb_module
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

from agent.redact import redact_sensitive_text

logger = logging.getLogger(__name__)

_hermes_home = Path(os.getenv("HERMES_HOME", Path.home() / ".hermes"))

# Maximum length for detail fields to prevent bloat
_MAX_DETAIL_LENGTH = 10_000

# JSONL rotation threshold (50MB)
_JSONL_MAX_BYTES = 50 * 1024 * 1024


def _truncate(text: str, max_len: int = _MAX_DETAIL_LENGTH) -> str:
    """Truncate text to max_len, appending a note if truncated."""
    if not text or len(text) <= max_len:
        return text
    return text[:max_len] + f"\n[truncated, {len(text)} chars total]"


def _now_iso() -> str:
    """Current UTC time as ISO 8601 string."""
    return datetime.now(timezone.utc).isoformat()


def _now_ts() -> float:
    """Current time as Unix timestamp."""
    return time.time()


# ---------------------------------------------------------------------------
# SQLite schema for activity_log
# ---------------------------------------------------------------------------

ACTIVITY_LOG_SQL = """
CREATE TABLE IF NOT EXISTS activity_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp REAL NOT NULL,
    timestamp_iso TEXT NOT NULL,
    event_type TEXT NOT NULL,
    level TEXT NOT NULL DEFAULT 'INFO',
    source TEXT,
    session_id TEXT,
    summary TEXT NOT NULL,
    detail TEXT,
    duration_ms REAL,
    metadata TEXT
);

CREATE INDEX IF NOT EXISTS idx_activity_timestamp ON activity_log(timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_activity_level ON activity_log(level, timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_activity_event_type ON activity_log(event_type, timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_activity_session ON activity_log(session_id, timestamp DESC);
"""


class ActivityLogger:
    """Singleton activity logger that writes to SQLite + JSONL."""

    def __init__(self):
        self._lock = threading.Lock()
        self._db_conn: Optional[sqlite3.Connection] = None
        self._jsonl_path: Optional[Path] = None
        self._jsonl_fd = None
        self._initialized = False

    def _ensure_init(self):
        """Lazy initialization — only sets up DB/file on first log call."""
        if self._initialized:
            return
        with self._lock:
            if self._initialized:
                return
            try:
                self._init_sqlite()
            except Exception as e:
                logger.debug("Activity logger: SQLite init failed: %s", e)
            try:
                self._init_jsonl()
            except Exception as e:
                logger.debug("Activity logger: JSONL init failed: %s", e)
            self._initialized = True

    def _init_sqlite(self):
        """Initialize the activity_log table in state.db."""
        db_path = _hermes_home / "state.db"
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._db_conn = sqlite3.connect(
            str(db_path),
            check_same_thread=False,
            timeout=10.0,
        )
        self._db_conn.execute("PRAGMA journal_mode=WAL")
        self._db_conn.executescript(ACTIVITY_LOG_SQL)
        self._db_conn.commit()

    def _init_jsonl(self):
        """Initialize the JSONL log file."""
        log_dir = _hermes_home / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        self._jsonl_path = log_dir / "activity.jsonl"

    def _rotate_jsonl_if_needed(self):
        """Rotate the JSONL file if it exceeds the size threshold."""
        if not self._jsonl_path or not self._jsonl_path.exists():
            return
        try:
            size = self._jsonl_path.stat().st_size
            if size >= _JSONL_MAX_BYTES:
                rotated = self._jsonl_path.with_suffix(".jsonl.1")
                # Keep only one rotated backup
                if rotated.exists():
                    rotated.unlink()
                self._jsonl_path.rename(rotated)
        except OSError:
            pass

    def _write_event(self, event: Dict[str, Any]):
        """Write an event to both SQLite and JSONL."""
        self._ensure_init()

        # Redact sensitive data from summary and detail
        if event.get("summary"):
            event["summary"] = redact_sensitive_text(str(event["summary"]))
        if event.get("detail"):
            event["detail"] = redact_sensitive_text(str(event["detail"]))

        # Write to SQLite
        if self._db_conn:
            try:
                with self._lock:
                    self._db_conn.execute(
                        """INSERT INTO activity_log
                           (timestamp, timestamp_iso, event_type, level, source,
                            session_id, summary, detail, duration_ms, metadata)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        (
                            event["timestamp"],
                            event["timestamp_iso"],
                            event["event_type"],
                            event["level"],
                            event.get("source"),
                            event.get("session_id"),
                            event["summary"],
                            _truncate(event.get("detail") or ""),
                            event.get("duration_ms"),
                            json.dumps(event.get("metadata")) if event.get("metadata") else None,
                        ),
                    )
                    self._db_conn.commit()
            except Exception as e:
                logger.debug("Activity logger: SQLite write failed: %s", e)

        # Write to JSONL
        if self._jsonl_path:
            try:
                self._rotate_jsonl_if_needed()
                line = json.dumps(event, default=str, ensure_ascii=False) + "\n"
                with open(self._jsonl_path, "a", encoding="utf-8") as f:
                    f.write(line)
            except Exception as e:
                logger.debug("Activity logger: JSONL write failed: %s", e)

    # ------------------------------------------------------------------
    # Public logging methods
    # ------------------------------------------------------------------

    def log_tool_call(
        self,
        tool_name: str,
        args: Dict[str, Any],
        result: str,
        duration_secs: float,
        session_id: str = None,
        success: bool = True,
        error_message: str = None,
    ):
        """Log a tool call with its arguments, result, and duration."""
        # Sanitize args — keep keys but truncate values
        safe_args = {}
        for k, v in (args or {}).items():
            sv = str(v)
            safe_args[k] = sv[:500] if len(sv) > 500 else sv

        result_preview = _truncate(result or "", 2000)
        level = "INFO" if success else "ERROR"
        summary = f"tool:{tool_name} {'OK' if success else 'FAILED'} ({duration_secs:.2f}s)"
        if error_message:
            summary += f" — {error_message[:200]}"

        self._write_event({
            "timestamp": _now_ts(),
            "timestamp_iso": _now_iso(),
            "event_type": "tool_call",
            "level": level,
            "source": "agent",
            "session_id": session_id,
            "summary": summary,
            "detail": result_preview,
            "duration_ms": round(duration_secs * 1000, 1),
            "metadata": {
                "tool_name": tool_name,
                "args": safe_args,
                "success": success,
            },
        })

    def log_model_call(
        self,
        model: str,
        input_tokens: int,
        output_tokens: int,
        duration_secs: float,
        session_id: str = None,
        cost_usd: float = None,
        finish_reason: str = None,
        cache_read_tokens: int = 0,
        cache_write_tokens: int = 0,
        provider: str = None,
    ):
        """Log an LLM API call with token usage, cost, and latency."""
        summary = (
            f"model:{model} in={input_tokens} out={output_tokens} "
            f"({duration_secs:.2f}s)"
        )
        if cost_usd is not None:
            summary += f" ${cost_usd:.4f}"

        self._write_event({
            "timestamp": _now_ts(),
            "timestamp_iso": _now_iso(),
            "event_type": "model_call",
            "level": "INFO",
            "source": "agent",
            "session_id": session_id,
            "summary": summary,
            "detail": None,
            "duration_ms": round(duration_secs * 1000, 1),
            "metadata": {
                "model": model,
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "cache_read_tokens": cache_read_tokens,
                "cache_write_tokens": cache_write_tokens,
                "cost_usd": cost_usd,
                "finish_reason": finish_reason,
                "provider": provider,
            },
        })

    def log_error(
        self,
        source: str,
        message: str,
        traceback_str: str = None,
        session_id: str = None,
        metadata: Dict[str, Any] = None,
    ):
        """Log an error with optional traceback."""
        self._write_event({
            "timestamp": _now_ts(),
            "timestamp_iso": _now_iso(),
            "event_type": "error",
            "level": "ERROR",
            "source": source,
            "session_id": session_id,
            "summary": message[:500],
            "detail": _truncate(traceback_str or ""),
            "duration_ms": None,
            "metadata": metadata,
        })

    def log_warning(
        self,
        source: str,
        message: str,
        session_id: str = None,
        metadata: Dict[str, Any] = None,
    ):
        """Log a warning event."""
        self._write_event({
            "timestamp": _now_ts(),
            "timestamp_iso": _now_iso(),
            "event_type": "warning",
            "level": "WARNING",
            "source": source,
            "session_id": session_id,
            "summary": message[:500],
            "detail": None,
            "duration_ms": None,
            "metadata": metadata,
        })

    def log_event(
        self,
        source: str,
        message: str,
        metadata: Dict[str, Any] = None,
        session_id: str = None,
        level: str = "INFO",
    ):
        """Log a general event (gateway connect, session start, etc.)."""
        self._write_event({
            "timestamp": _now_ts(),
            "timestamp_iso": _now_iso(),
            "event_type": "event",
            "level": level,
            "source": source,
            "session_id": session_id,
            "summary": message[:500],
            "detail": None,
            "duration_ms": None,
            "metadata": metadata,
        })

    def log_cron_job(
        self,
        job_id: str,
        job_name: str,
        success: bool,
        duration_secs: float,
        error_message: str = None,
        delivered_to: str = None,
        output_preview: str = None,
    ):
        """Log a cron job execution."""
        level = "INFO" if success else "ERROR"
        summary = f"cron:{job_name} {'OK' if success else 'FAILED'} ({duration_secs:.2f}s)"
        if delivered_to:
            summary += f" → {delivered_to}"
        if error_message:
            summary += f" — {error_message[:200]}"

        self._write_event({
            "timestamp": _now_ts(),
            "timestamp_iso": _now_iso(),
            "event_type": "cron_job",
            "level": level,
            "source": "cron",
            "session_id": None,
            "summary": summary,
            "detail": _truncate(output_preview or ""),
            "duration_ms": round(duration_secs * 1000, 1),
            "metadata": {
                "job_id": job_id,
                "job_name": job_name,
                "success": success,
                "delivered_to": delivered_to,
                "error": error_message,
            },
        })

    def log_session_event(
        self,
        event_name: str,
        session_id: str,
        platform: str = None,
        user_id: str = None,
        metadata: Dict[str, Any] = None,
    ):
        """Log session lifecycle events (start, end, reset)."""
        summary = f"session:{event_name}"
        if platform:
            summary += f" platform={platform}"
        if user_id:
            summary += f" user={user_id}"

        self._write_event({
            "timestamp": _now_ts(),
            "timestamp_iso": _now_iso(),
            "event_type": "session",
            "level": "INFO",
            "source": "gateway",
            "session_id": session_id,
            "summary": summary,
            "detail": None,
            "duration_ms": None,
            "metadata": {
                **(metadata or {}),
                "event_name": event_name,
                "platform": platform,
                "user_id": user_id,
            },
        })

    # ------------------------------------------------------------------
    # Query methods (for AI-driven log review)
    # ------------------------------------------------------------------

    def query_recent(
        self,
        hours: float = 12,
        level: str = None,
        event_type: str = None,
        limit: int = 100,
    ) -> list:
        """Query recent activity log entries.

        Args:
            hours: How far back to look (default 12 hours)
            level: Filter by level (ERROR, WARNING, INFO)
            event_type: Filter by event type (tool_call, model_call, error, etc.)
            limit: Max results to return

        Returns:
            List of dicts with log entries, most recent first.
        """
        self._ensure_init()
        if not self._db_conn:
            return []

        cutoff = time.time() - (hours * 3600)
        where_clauses = ["timestamp > ?"]
        params: list = [cutoff]

        if level:
            where_clauses.append("level = ?")
            params.append(level)
        if event_type:
            where_clauses.append("event_type = ?")
            params.append(event_type)

        params.append(limit)
        where_sql = " AND ".join(where_clauses)

        try:
            with self._lock:
                cursor = self._db_conn.execute(
                    f"""SELECT id, timestamp_iso, event_type, level, source,
                               session_id, summary, detail, duration_ms, metadata
                        FROM activity_log
                        WHERE {where_sql}
                        ORDER BY timestamp DESC
                        LIMIT ?""",
                    params,
                )
                rows = cursor.fetchall()
            columns = [
                "id", "timestamp_iso", "event_type", "level", "source",
                "session_id", "summary", "detail", "duration_ms", "metadata",
            ]
            results = []
            for row in rows:
                entry = dict(zip(columns, row))
                if entry.get("metadata"):
                    try:
                        entry["metadata"] = json.loads(entry["metadata"])
                    except (json.JSONDecodeError, TypeError):
                        pass
                results.append(entry)
            return results
        except Exception as e:
            logger.debug("Activity logger: query failed: %s", e)
            return []

    def get_summary(self, hours: float = 12) -> Dict[str, Any]:
        """Get a summary of activity over the given time period.

        Returns counts by event_type and level, plus lists of errors/warnings.
        """
        self._ensure_init()
        if not self._db_conn:
            return {"error": "Database not available"}

        cutoff = time.time() - (hours * 3600)
        try:
            with self._lock:
                # Counts by event type
                cursor = self._db_conn.execute(
                    """SELECT event_type, COUNT(*) as cnt
                       FROM activity_log WHERE timestamp > ?
                       GROUP BY event_type""",
                    (cutoff,),
                )
                type_counts = {row[0]: row[1] for row in cursor.fetchall()}

                # Counts by level
                cursor = self._db_conn.execute(
                    """SELECT level, COUNT(*) as cnt
                       FROM activity_log WHERE timestamp > ?
                       GROUP BY level""",
                    (cutoff,),
                )
                level_counts = {row[0]: row[1] for row in cursor.fetchall()}

                # Recent errors
                cursor = self._db_conn.execute(
                    """SELECT timestamp_iso, source, summary, detail
                       FROM activity_log
                       WHERE timestamp > ? AND level = 'ERROR'
                       ORDER BY timestamp DESC LIMIT 20""",
                    (cutoff,),
                )
                errors = [
                    {"time": r[0], "source": r[1], "summary": r[2], "detail": r[3]}
                    for r in cursor.fetchall()
                ]

                # Recent warnings
                cursor = self._db_conn.execute(
                    """SELECT timestamp_iso, source, summary
                       FROM activity_log
                       WHERE timestamp > ? AND level = 'WARNING'
                       ORDER BY timestamp DESC LIMIT 20""",
                    (cutoff,),
                )
                warnings = [
                    {"time": r[0], "source": r[1], "summary": r[2]}
                    for r in cursor.fetchall()
                ]

            return {
                "period_hours": hours,
                "event_counts": type_counts,
                "level_counts": level_counts,
                "total_events": sum(type_counts.values()),
                "errors": errors,
                "warnings": warnings,
            }
        except Exception as e:
            logger.debug("Activity logger: summary query failed: %s", e)
            return {"error": str(e)}

    def close(self):
        """Close database connection."""
        with self._lock:
            if self._db_conn:
                try:
                    self._db_conn.close()
                except Exception:
                    pass
                self._db_conn = None


# Module-level singleton
activity_logger = ActivityLogger()
