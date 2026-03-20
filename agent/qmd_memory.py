from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import threading
import time
from datetime import datetime
from functools import cached_property
from pathlib import Path
from typing import Any, Dict, List, Optional

from hermes_state import SessionDB
from agent.session_transcript import summarize_assistant_tool_calls, summarize_tool_content

logger = logging.getLogger(__name__)

_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
_QMD_STRUCTURED_QUERY_RE = re.compile(r"^(?:lex|vec|hyde|intent|expand):\s*", re.IGNORECASE)


def _strip_ansi(text: str) -> str:
    return _ANSI_RE.sub("", text or "")


def _extract_json_payload(text: str) -> Optional[Any]:
    cleaned = _strip_ansi(text).strip()
    if not cleaned:
        return None
    for start_char, end_char in (("[", "]"), ("{", "}")):
        start = cleaned.find(start_char)
        end = cleaned.rfind(end_char)
        if start != -1 and end != -1 and end > start:
            candidate = cleaned[start : end + 1]
            try:
                return json.loads(candidate)
            except Exception:
                pass
    return None


def _safe_slug(text: str) -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9._-]+", "-", (text or "").strip())
    cleaned = cleaned.strip("-._")
    return cleaned or "untitled"


class QmdMemoryManager:
    """QMD-backed recall for Hermes memory files and exported session transcripts."""

    _init_lock = threading.Lock()
    _initialized_keys: set[str] = set()
    _sync_lock = threading.Lock()
    _syncing_keys: set[str] = set()

    def __init__(self, hermes_home: Path, config: Dict[str, Any]):
        self.hermes_home = Path(hermes_home)
        self.config = config or {}
        qcfg = self.config.get("qmd") or {}

        self.command = str(qcfg.get("command") or "qmd").strip() or "qmd"
        self.search_mode = str(qcfg.get("search_mode") or "query").strip().lower()
        if self.search_mode not in {"query", "search", "vsearch"}:
            self.search_mode = "query"
        self.max_results = max(1, int(qcfg.get("max_results") or 6))
        self.max_injected_chars = max(200, int(qcfg.get("max_injected_chars") or 2400))
        self.update_interval_seconds = max(0, int(qcfg.get("update_interval_seconds") or 300))
        self.embed_on_sync = bool(qcfg.get("embed_on_sync", True))

        memory_path = str(qcfg.get("memory_path") or "").strip()
        self.memory_path = Path(memory_path).expanduser() if memory_path else self.hermes_home / "memories"

        self.state_dir = self.hermes_home / "qmd"
        self.xdg_config_home = self.state_dir / "xdg-config"
        cache_home = str(qcfg.get("cache_home") or "").strip()
        self.xdg_cache_home = Path(cache_home).expanduser() if cache_home else (Path.home() / ".cache")
        self.meta_path = self.state_dir / "state.json"

        sessions_cfg = qcfg.get("sessions") or {}
        self.sessions_enabled = bool(sessions_cfg.get("enabled", True))
        self.sessions_retention_days = max(1, int(sessions_cfg.get("retention_days") or 120))
        sessions_export_dir = str(sessions_cfg.get("export_dir") or "").strip()
        self.sessions_export_dir = Path(sessions_export_dir).expanduser() if sessions_export_dir else (self.state_dir / "sessions")

        self.last_error: Optional[str] = None
        self.memory_collection = self.memory_path.name or "memories"
        self.sessions_collection = self.sessions_export_dir.name or "sessions"

    def _cache_key(self) -> str:
        return "|".join(
            [
                str(Path(self.command).expanduser()),
                str(self.memory_path),
                str(self.sessions_export_dir),
                str(self.sessions_enabled),
            ]
        )

    @property
    def enabled(self) -> bool:
        return True

    @cached_property
    def _resolved_command(self) -> List[str]:
        cmd_path = Path(self.command).expanduser()
        if not cmd_path.is_absolute() or not cmd_path.exists():
            return [self.command]

        try:
            source = cmd_path.resolve()
        except Exception:
            return [str(cmd_path)]

        package_dir = source.parent.parent
        script_path = package_dir / "dist" / "cli" / "qmd.js"
        if source.name != "qmd" or source.parent.name != "bin" or not script_path.exists():
            return [str(cmd_path)]

        uses_bun = any((package_dir / lock_name).exists() for lock_name in ("bun.lock", "bun.lockb")) or bool(os.environ.get("BUN_INSTALL"))
        if uses_bun:
            bun_path = shutil.which("bun")
            if bun_path:
                return [bun_path, str(script_path)]
            return [str(cmd_path)]

        node_path = cmd_path.parent / "node"
        if node_path.exists():
            return [str(node_path), str(script_path)]
        return [str(cmd_path)]

    def _env(self) -> Dict[str, str]:
        env = os.environ.copy()
        env["XDG_CONFIG_HOME"] = str(self.xdg_config_home)
        env["XDG_CACHE_HOME"] = str(self.xdg_cache_home)
        env.setdefault("NO_COLOR", "1")
        env.setdefault("TERM", "dumb")
        cmd_path = Path(self.command).expanduser()
        if cmd_path.is_absolute() and cmd_path.parent.exists():
            env["PATH"] = f"{cmd_path.parent}:{env.get('PATH', '')}"
        return env

    def _run(self, args: List[str], timeout: int = 180, check: bool = True) -> subprocess.CompletedProcess:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.xdg_config_home.mkdir(parents=True, exist_ok=True)
        self.xdg_cache_home.mkdir(parents=True, exist_ok=True)
        completed = subprocess.run(
            [*self._resolved_command, *args],
            capture_output=True,
            text=True,
            timeout=timeout,
            env=self._env(),
        )
        if check and completed.returncode != 0:
            stderr = _strip_ansi(completed.stderr).strip()
            stdout = _strip_ansi(completed.stdout).strip()
            raise RuntimeError(stderr or stdout or f"qmd {' '.join(args)} failed with exit code {completed.returncode}")
        return completed

    def _load_meta(self) -> Dict[str, Any]:
        if not self.meta_path.exists():
            return {}
        try:
            return json.loads(self.meta_path.read_text(encoding="utf-8"))
        except Exception:
            return {}

    def _save_meta(self, data: Dict[str, Any]) -> None:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.meta_path.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")

    def _list_collections_text(self) -> str:
        result = self._run(["collection", "list"], timeout=60, check=False)
        return _strip_ansi((result.stdout or "") + "\n" + (result.stderr or ""))

    def _collection_exists(self, name: str) -> bool:
        try:
            text = self._list_collections_text()
            return f"{name} (qmd://{name}/)" in text
        except Exception:
            return False

    def _ensure_collection(self, path: Path) -> None:
        path.mkdir(parents=True, exist_ok=True)
        collection_name = path.name or "docs"
        if self._collection_exists(collection_name):
            return
        self._run(["collection", "add", str(path), "*.md"], timeout=180)

    def _export_sessions(self) -> Dict[str, Any]:
        if not self.sessions_enabled:
            return {"enabled": False, "exported": 0, "removed": 0}

        self.sessions_export_dir.mkdir(parents=True, exist_ok=True)
        now = time.time()
        cutoff = now - (self.sessions_retention_days * 86400)
        db = SessionDB()
        try:
            sessions = db.export_all()
        finally:
            db.close()

        keep_paths: set[Path] = set()
        exported = 0
        for session in sessions:
            started_at = float(session.get("started_at") or 0)
            if started_at and started_at < cutoff:
                continue
            session_id = str(session.get("id") or "")
            if not session_id:
                continue
            title = str(session.get("title") or "")
            source = str(session.get("source") or "unknown")
            started_label = datetime.fromtimestamp(started_at or now).strftime("%Y-%m-%d %H:%M:%S")
            fname = f"{started_label[:10]}-{_safe_slug(source)}-{_safe_slug(title or session_id)}-{_safe_slug(session_id)}.md"
            out_path = self.sessions_export_dir / fname
            keep_paths.add(out_path)

            lines = [
                f"# Session {session_id}",
                "",
                f"- Source: {source}",
                f"- Title: {title or '(untitled)'}",
                f"- Started: {started_label}",
            ]
            if session.get("ended_at"):
                lines.append(f"- Ended: {datetime.fromtimestamp(float(session['ended_at'])).strftime('%Y-%m-%d %H:%M:%S')}")
            lines.append("")
            lines.append("## Transcript")
            lines.append("")
            for msg in session.get("messages") or []:
                role_key = str(msg.get("role") or "unknown").strip().lower()
                role = role_key.title() or "Unknown"
                content = str(msg.get("content") or "").strip()

                rendered_blocks: List[str] = []
                if role_key == "tool":
                    summary = summarize_tool_content(msg.get("tool_name"), content)
                    if summary:
                        rendered_blocks.append(summary)
                elif role_key == "assistant":
                    tool_summary = summarize_assistant_tool_calls(msg.get("tool_calls"))
                    if tool_summary:
                        rendered_blocks.append(f"[{tool_summary}]")
                    if content:
                        rendered_blocks.append(content)
                else:
                    if content:
                        rendered_blocks.append(content)

                if not rendered_blocks:
                    continue

                lines.append(f"### {role}")
                lines.append("")
                lines.extend(rendered_blocks)
                lines.append("")
            text = "\n".join(lines).rstrip() + "\n"
            previous = out_path.read_text(encoding="utf-8") if out_path.exists() else None
            if previous != text:
                out_path.write_text(text, encoding="utf-8")
                exported += 1

        removed = 0
        for existing in self.sessions_export_dir.glob("*.md"):
            if existing not in keep_paths:
                existing.unlink(missing_ok=True)
                removed += 1
        return {"enabled": True, "exported": exported, "removed": removed, "kept": len(keep_paths)}

    def ensure_initialized(self, force: bool = False) -> None:
        cache_key = self._cache_key()
        if not force and cache_key in self._initialized_keys:
            return

        with self._init_lock:
            if not force and cache_key in self._initialized_keys:
                return
            self.state_dir.mkdir(parents=True, exist_ok=True)
            self.xdg_config_home.mkdir(parents=True, exist_ok=True)
            self.xdg_cache_home.mkdir(parents=True, exist_ok=True)
            self.memory_path.mkdir(parents=True, exist_ok=True)
            self._ensure_collection(self.memory_path)
            if self.sessions_enabled:
                self.sessions_export_dir.mkdir(parents=True, exist_ok=True)
                self._ensure_collection(self.sessions_export_dir)
            self._initialized_keys.add(cache_key)

    def _is_sync_stale(self, now: Optional[int] = None) -> bool:
        if self.update_interval_seconds <= 0:
            return True
        meta = self._load_meta()
        last_sync = int(meta.get("last_sync", 0) or 0)
        now = int(now or time.time())
        return (now - last_sync) >= self.update_interval_seconds

    def schedule_sync_if_stale(self, force: bool = False) -> bool:
        if not force and not self._is_sync_stale():
            return False

        cache_key = self._cache_key()
        with self._sync_lock:
            if cache_key in self._syncing_keys:
                return False
            self._syncing_keys.add(cache_key)

        def _runner():
            try:
                self.sync(force=True)
            finally:
                with self._sync_lock:
                    self._syncing_keys.discard(cache_key)

        thread = threading.Thread(
            target=_runner,
            name=f"qmd-sync-{self.memory_collection}",
            daemon=True,
        )
        thread.start()
        return True

    def sync(self, force: bool = False) -> bool:
        try:
            self.ensure_initialized()
            now = int(time.time())
            meta = self._load_meta()
            last_sync = int(meta.get("last_sync", 0) or 0)
            if not force and self.update_interval_seconds > 0 and (now - last_sync) < self.update_interval_seconds:
                self.last_error = None
                return False

            session_export = self._export_sessions()
            self._run(["update"], timeout=240)
            if self.embed_on_sync:
                self._run(["embed"], timeout=900)
            meta["last_sync"] = now
            meta["last_session_export"] = session_export
            self._save_meta(meta)
            self.last_error = None
            return True
        except Exception as exc:
            self.last_error = str(exc)
            logger.warning("QMD memory sync failed: %s", exc)
            return False

    def _prepare_search_query(self, query: str) -> str:
        query = str(query or "")
        if self.search_mode != "query":
            return query

        lines = [line.strip() for line in query.splitlines() if line.strip()]
        if len(lines) <= 1:
            return lines[0] if lines else ""
        if all(_QMD_STRUCTURED_QUERY_RE.match(line) for line in lines):
            return "\n".join(lines)
        return re.sub(r"\s+", " ", query).strip()

    def _search_collection(self, query: str, collection: str, limit: int) -> List[Dict[str, Any]]:
        prepared_query = self._prepare_search_query(query)
        args = [self.search_mode, prepared_query, "--json", "-n", str(limit), "-c", collection]
        result = self._run(args, timeout=900 if self.search_mode == "query" else 240)
        payload = _extract_json_payload(result.stdout)
        if isinstance(payload, list):
            return payload
        return []

    def search_context(self, query: str, include_sessions: bool = False) -> Optional[str]:
        query = (query or "").strip()
        if not query:
            return ""
        try:
            self.ensure_initialized()
            payload: List[Dict[str, Any]] = []
            seen_files: set[str] = set()

            collections = [self.memory_collection]
            if include_sessions and self.sessions_enabled:
                collections.append(self.sessions_collection)

            for collection in collections:
                if not collection:
                    continue
                for item in self._search_collection(query, collection, self.max_results):
                    if not isinstance(item, dict):
                        continue
                    file_ref = str(item.get("file") or "")
                    if file_ref and file_ref in seen_files:
                        continue
                    if file_ref:
                        seen_files.add(file_ref)
                    payload.append(item)
                    if len(payload) >= self.max_results:
                        break
                if len(payload) >= self.max_results:
                    break

            lines: List[str] = []
            total_chars = 0
            for item in payload:
                snippet = str(item.get("snippet") or "").strip()
                if not snippet:
                    continue
                snippet = re.sub(r"^@@.*?\n", "", snippet, count=1, flags=re.DOTALL).strip()
                file_ref = str(item.get("file") or "")
                title = str(item.get("title") or file_ref or "memory")
                block = f"- {title} ({file_ref})\n{snippet}"
                if total_chars + len(block) > self.max_injected_chars:
                    remaining = self.max_injected_chars - total_chars
                    if remaining > 120:
                        lines.append(block[:remaining].rstrip())
                    break
                lines.append(block)
                total_chars += len(block) + 2

            self.last_error = None
            if not lines:
                return ""
            return "QMD Recall:\n" + "\n\n".join(lines)
        except Exception as exc:
            self.last_error = str(exc)
            logger.warning("QMD memory search failed: %s", exc)
            return None

    def status_info(self, sync: bool = False) -> Dict[str, Any]:
        if sync:
            self.sync(force=True)
        meta = self._load_meta()
        collections_text = self._list_collections_text() if Path(self.command).exists() or shutil.which(self.command) else ""
        memory_files = len(list(self.memory_path.glob("*.md"))) if self.memory_path.exists() else 0
        session_files = len(list(self.sessions_export_dir.glob("*.md"))) if self.sessions_export_dir.exists() else 0
        return {
            "backend": "qmd",
            "command": self.command,
            "search_mode": self.search_mode,
            "memory_path": str(self.memory_path),
            "memory_files": memory_files,
            "sessions_enabled": self.sessions_enabled,
            "sessions_export_dir": str(self.sessions_export_dir),
            "session_files": session_files,
            "last_sync": meta.get("last_sync"),
            "last_session_export": meta.get("last_session_export"),
            "last_error": self.last_error,
            "collections_text": collections_text.strip(),
        }
