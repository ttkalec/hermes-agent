"""Mission Control worker for Hermes.

Runs Hermes as a task worker against a Mission Control dashboard.
This keeps the normal CLI/gateway behavior untouched while providing a
purpose-built autonomous work loop for Mission Control tasks.
"""

from __future__ import annotations

import json
import os
import socket
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional
from urllib import error, request

from hermes_cli.config import load_config
from hermes_cli.runtime_provider import format_runtime_provider_error, resolve_runtime_provider
from run_agent import AIAgent

DEFAULT_URL = "http://127.0.0.1:3027"
DEFAULT_AGENT_NAME = "Hermes"


@dataclass
class MissionControlSettings:
    url: str = DEFAULT_URL
    agent_name: str = DEFAULT_AGENT_NAME
    interval_seconds: int = 30
    requires_review: bool = True
    max_iterations: int = 60
    enabled_toolsets: Optional[list[str]] = None
    model: Optional[str] = None
    provider: Optional[str] = None


class MissionControlClient:
    def __init__(self, base_url: str):
        self.base_url = base_url.rstrip("/")

    def _post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        data = json.dumps(payload).encode("utf-8")
        req = request.Request(
            f"{self.base_url}{path}",
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"Mission Control HTTP {exc.code}: {body}") from exc
        except error.URLError as exc:
            raise RuntimeError(f"Mission Control unreachable at {self.base_url}: {exc.reason}") from exc

    def heartbeat(self, *, agent: str, message: str, auto_claim: bool = True) -> dict[str, Any]:
        return self._post(
            "/api/mission-control/heartbeat",
            {"agent": agent, "message": message, "autoClaim": auto_claim},
        )

    def report(
        self,
        *,
        task_id: str,
        outcome: str,
        summary: str,
        blocked_reason: str | None = None,
        error_message: str | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "outcome": outcome,
            "summary": summary,
        }
        if blocked_reason:
            payload["blockedReason"] = blocked_reason
        if error_message:
            payload["error"] = error_message
        return self._post(f"/api/mission-control/tasks/{task_id}/report", payload)

    def set_worker_lock(self, *, owner: str, locked: bool) -> dict[str, Any]:
        return self._post("/api/mission-control/worker-lock", {"owner": owner, "locked": locked})


class WorkerLock:
    def __init__(self, agent_name: str):
        self.agent_name = agent_name
        hermes_home = Path(os.getenv("HERMES_HOME", Path.home() / ".hermes"))
        self.lock_path = hermes_home / "mission-control-worker.lock"
        self.owner = f"{agent_name}@{socket.gethostname()}:{os.getpid()}"
        self._fh = None

    def acquire(self) -> str:
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        fh = open(self.lock_path, "a+", encoding="utf-8")
        try:
            import fcntl

            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            fh.seek(0)
            existing = fh.read().strip() or "another Hermes worker"
            fh.close()
            raise RuntimeError(f"Mission Control worker already running: {existing}")

        fh.seek(0)
        fh.truncate(0)
        fh.write(self.owner)
        fh.flush()
        self._fh = fh
        return self.owner

    def release(self) -> None:
        if not self._fh:
            return
        try:
            import fcntl

            self._fh.seek(0)
            self._fh.truncate(0)
            fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
        finally:
            self._fh.close()
            self._fh = None


def _truthy(value: str | None) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _load_settings(args) -> MissionControlSettings:
    cfg = load_config()
    mc = cfg.get("mission_control", {}) if isinstance(cfg, dict) else {}
    model_cfg = cfg.get("model", {}) if isinstance(cfg, dict) else {}

    if isinstance(model_cfg, dict):
        default_model = model_cfg.get("default")
        default_provider = model_cfg.get("provider")
    else:
        default_model = model_cfg
        default_provider = None

    toolsets = None
    platform_toolsets = cfg.get("platform_toolsets", {}) if isinstance(cfg, dict) else {}
    if isinstance(platform_toolsets, dict):
        cli_toolsets = platform_toolsets.get("cli")
        if isinstance(cli_toolsets, list) and cli_toolsets:
            toolsets = cli_toolsets

    return MissionControlSettings(
        url=getattr(args, "url", None)
        or os.getenv("MISSION_CONTROL_URL")
        or mc.get("url")
        or DEFAULT_URL,
        agent_name=getattr(args, "agent_name", None)
        or os.getenv("MISSION_CONTROL_AGENT")
        or mc.get("agent_name")
        or DEFAULT_AGENT_NAME,
        interval_seconds=int(
            getattr(args, "interval", None)
            or os.getenv("MISSION_CONTROL_INTERVAL_SECONDS")
            or mc.get("interval_seconds")
            or 30
        ),
        requires_review=not _truthy(str(getattr(args, "done", False))) if hasattr(args, "done") else bool(
            mc.get("requires_review", True)
        ),
        max_iterations=int(
            getattr(args, "max_iterations", None)
            or mc.get("max_iterations")
            or cfg.get("agent", {}).get("max_turns", 60)
        ),
        enabled_toolsets=toolsets,
        model=getattr(args, "model", None) or default_model,
        provider=getattr(args, "provider", None) or default_provider,
    )


def _build_task_prompt(task: dict[str, Any]) -> str:
    return f"""You are Hermes working autonomously from Mission Control.

Task ID: {task.get('id')}
Title: {task.get('title')}
Project: {task.get('project')}
Priority: {task.get('priority')}
Description:
{task.get('description')}

Instructions:
- Complete the task as fully as you can using your available tools.
- Be proactive and execute the work, not just comment on it.
- If the task is blocked by missing information, missing access, or an external dependency, start your final answer with exactly: BLOCKED:
- If you are blocked, explain the blocker clearly and what unblocks it.
- If you fail unexpectedly, explain the failure as clearly as possible.
- End with a concise operator-friendly summary.
"""


def _classify_result(result: dict[str, Any], requires_review: bool) -> tuple[str, str | None]:
    final_response = (result.get("final_response") or "").strip()
    upper = final_response.upper()

    if result.get("completed") and final_response:
        return ("review" if requires_review else "done"), None

    if upper.startswith("BLOCKED:") or "\nBLOCKED:" in upper:
        blocker = final_response.split("BLOCKED:", 1)[-1].strip() if "BLOCKED:" in upper else final_response
        return "blocked", blocker or "Blocked without details."

    return "failed", None


def _summarize_result(task: dict[str, Any], result: dict[str, Any], outcome: str, extra_reason: str | None = None) -> str:
    final_response = (result.get("final_response") or "").strip()
    error_message = (result.get("error") or "").strip()
    api_calls = result.get("api_calls")

    sections = [
        f"Task: {task.get('title')}",
        f"Outcome: {outcome}",
    ]

    if isinstance(api_calls, int):
        sections.append(f"API calls: {api_calls}")
    if result.get("interrupted"):
        sections.append("Interrupted: yes")
    if result.get("partial"):
        sections.append("Partial: yes")
    if extra_reason:
        sections.append(f"Reason: {extra_reason}")
    if error_message:
        sections.append(f"Error: {error_message[:1000]}")
    if final_response:
        sections.append("Response:\n" + final_response[:2800])

    text = "\n\n".join(sections).strip()
    return text[:4000]


def _create_agent(settings: MissionControlSettings) -> AIAgent:
    try:
        runtime = resolve_runtime_provider(requested=settings.provider)
    except Exception as exc:
        raise RuntimeError(format_runtime_provider_error(exc)) from exc

    return AIAgent(
        model=settings.model or "anthropic/claude-opus-4.6",
        api_key=runtime.get("api_key"),
        base_url=runtime.get("base_url"),
        provider=runtime.get("provider"),
        api_mode=runtime.get("api_mode"),
        max_iterations=settings.max_iterations,
        enabled_toolsets=settings.enabled_toolsets,
        quiet_mode=False,
        verbose_logging=False,
        platform="cli",
    )


def _run_task_once(settings: MissionControlSettings, client: MissionControlClient) -> bool:
    heartbeat = client.heartbeat(
        agent=settings.agent_name,
        message="Mission Control worker heartbeat. Checking for assigned work.",
        auto_claim=True,
    )
    task = heartbeat.get("assignedTask")
    if not task:
        print("No task assigned. Backlog is empty or only review items remain.")
        return False

    print(f"Working task {task.get('id')}: {task.get('title')}")
    agent = _create_agent(settings)

    try:
        result = agent.run_conversation(_build_task_prompt(task))
        outcome, reason = _classify_result(result, settings.requires_review)
        summary = _summarize_result(task, result, outcome, reason)
        error_message = (result.get("error") or "").strip() or None
        client.report(
            task_id=task.get("id"),
            outcome=outcome,
            summary=summary,
            blocked_reason=reason if outcome == "blocked" else None,
            error_message=error_message if outcome == "failed" else None,
        )

        if outcome in {"review", "done"}:
            print(f"Completed task {task.get('id')} → {outcome}")
        else:
            print(f"Task {task.get('id')} ended as {outcome}.", file=sys.stderr)
    except KeyboardInterrupt:
        raise
    except Exception as exc:
        failure_text = str(exc)
        summary = f"Task: {task.get('title')}\n\nOutcome: failed\n\nError: {failure_text[:3000]}"
        client.report(
            task_id=task.get("id"),
            outcome="failed",
            summary=summary,
            error_message=failure_text[:3000],
        )
        print(f"Task {task.get('id')} failed: {failure_text}", file=sys.stderr)

    client.heartbeat(
        agent=settings.agent_name,
        message="Mission Control worker finished a task cycle and is ready for the next task.",
        auto_claim=True,
    )
    return True


def cmd_mission_control(args) -> None:
    settings = _load_settings(args)
    client = MissionControlClient(settings.url)
    lock = WorkerLock(settings.agent_name)
    owner = lock.acquire()
    client.set_worker_lock(owner=owner, locked=True)

    try:
        if getattr(args, "once", False):
            _run_task_once(settings, client)
            return

        print(
            f"Starting Mission Control worker against {settings.url} as {settings.agent_name} every {settings.interval_seconds}s"
        )
        while True:
            try:
                worked = _run_task_once(settings, client)
            except KeyboardInterrupt:
                print("\nMission Control worker stopped.")
                return
            except Exception as exc:
                print(f"Mission Control worker error: {exc}", file=sys.stderr)
                worked = False

            sleep_for = 1 if worked else settings.interval_seconds
            time.sleep(max(1, sleep_for))
    finally:
        try:
            client.set_worker_lock(owner=owner, locked=False)
        except Exception:
            pass
        lock.release()
