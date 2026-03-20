import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

from agent.qmd_memory import QmdMemoryManager


def _manager(tmp_path: Path) -> QmdMemoryManager:
    return QmdMemoryManager(
        tmp_path / "hermes-home",
        {
            "qmd": {
                "command": "qmd",
                "search_mode": "query",
                "max_results": 6,
                "max_injected_chars": 2400,
                "update_interval_seconds": 300,
                "embed_on_sync": True,
                "sessions": {
                    "enabled": True,
                    "retention_days": 120,
                },
            }
        },
    )


def test_search_context_does_not_sync_on_hot_path(tmp_path):
    mgr = _manager(tmp_path)
    mgr.ensure_initialized = MagicMock()
    mgr.schedule_sync_if_stale = MagicMock(return_value=False)
    mgr._search_collection = MagicMock(return_value=[])

    result = mgr.search_context("hello")

    assert result == ""
    mgr.ensure_initialized.assert_called_once_with()
    mgr.schedule_sync_if_stale.assert_not_called()


def test_search_context_skips_sessions_by_default(tmp_path):
    mgr = _manager(tmp_path)
    mgr.ensure_initialized = MagicMock()
    mgr._search_collection = MagicMock(
        side_effect=[
            [{"title": "memory", "file": "memory.md", "snippet": "remember this"}],
            [{"title": "session", "file": "session.md", "snippet": "old session"}],
        ]
    )

    result = mgr.search_context("hello")

    assert "remember this" in result
    searched = [call.args[1] for call in mgr._search_collection.call_args_list]
    assert searched == [mgr.memory_collection]


def test_search_context_can_include_sessions_when_requested(tmp_path):
    mgr = _manager(tmp_path)
    mgr.ensure_initialized = MagicMock()
    mgr._search_collection = MagicMock(
        side_effect=[
            [],
            [{"title": "session", "file": "session.md", "snippet": "old session"}],
        ]
    )

    result = mgr.search_context("what were we doing last time?", include_sessions=True)

    assert "old session" in result
    searched = [call.args[1] for call in mgr._search_collection.call_args_list]
    assert searched == [mgr.memory_collection, mgr.sessions_collection]


def test_ensure_initialized_is_cached(tmp_path):
    mgr = _manager(tmp_path)
    mgr._ensure_collection = MagicMock()

    mgr.ensure_initialized()
    mgr.ensure_initialized()

    assert mgr._ensure_collection.call_count == 2


def test_schedule_sync_if_stale_starts_background_sync(tmp_path):
    mgr = _manager(tmp_path)
    mgr.sync = MagicMock()
    mgr._load_meta = MagicMock(return_value={"last_sync": 0})

    with patch("agent.qmd_memory.threading.Thread") as mock_thread:
        started = MagicMock()
        mock_thread.return_value.start = started

        scheduled = mgr.schedule_sync_if_stale()

    assert scheduled is True
    mock_thread.assert_called_once()
    started.assert_called_once_with()


def test_run_pins_matching_node_for_absolute_qmd_wrapper(tmp_path):
    install_root = tmp_path / "nvm"
    bin_dir = install_root / "bin"
    package_dir = install_root / "lib" / "node_modules" / "@tobilu" / "qmd"
    package_bin = package_dir / "bin"
    script_path = package_dir / "dist" / "cli" / "qmd.js"
    command_path = bin_dir / "qmd"
    node_path = bin_dir / "node"

    package_bin.mkdir(parents=True)
    script_path.parent.mkdir(parents=True)
    bin_dir.mkdir(parents=True)
    (package_bin / "qmd").write_text("#!/bin/sh\n", encoding="utf-8")
    script_path.write_text("console.log('ok')\n", encoding="utf-8")
    node_path.write_text("", encoding="utf-8")
    command_path.symlink_to(package_bin / "qmd")

    mgr = QmdMemoryManager(
        tmp_path / "hermes-home",
        {
            "qmd": {
                "command": str(command_path),
            }
        },
    )

    with patch("agent.qmd_memory.subprocess.run") as mock_run:
        mock_run.return_value = subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")
        mgr._run(["collection", "list"], check=False)

    invoked = mock_run.call_args.args[0]
    assert invoked == [str(node_path), str(script_path.resolve()), "collection", "list"]


def test_run_falls_back_to_command_when_qmd_wrapper_cannot_be_resolved(tmp_path):
    command_path = tmp_path / "qmd"
    command_path.write_text("#!/bin/sh\n", encoding="utf-8")

    mgr = QmdMemoryManager(
        tmp_path / "hermes-home",
        {
            "qmd": {
                "command": str(command_path),
            }
        },
    )

    with patch("agent.qmd_memory.subprocess.run") as mock_run:
        mock_run.return_value = subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")
        mgr._run(["query", "hello"], check=False)

    invoked = mock_run.call_args.args[0]
    assert invoked == [str(command_path), "query", "hello"]


def test_search_collection_normalizes_plain_multiline_query_mode_input(tmp_path):
    mgr = _manager(tmp_path)

    captured_args = {}

    def fake_run(args, timeout=0, check=True):
        captured_args["args"] = args
        return subprocess.CompletedProcess(args=args, returncode=0, stdout="[]", stderr="")

    mgr._run = fake_run

    result = mgr._search_collection("first line\nsecond line", "memories", 3)

    assert result == []
    assert captured_args["args"] == ["query", "first line second line", "--json", "-n", "3", "-c", "memories"]


def test_search_collection_preserves_structured_query_mode_input(tmp_path):
    mgr = _manager(tmp_path)

    captured_args = {}

    def fake_run(args, timeout=0, check=True):
        captured_args["args"] = args
        return subprocess.CompletedProcess(args=args, returncode=0, stdout="[]", stderr="")

    mgr._run = fake_run

    result = mgr._search_collection("lex: exact phrase\nintent: debug telegram delays", "memories", 3)

    assert result == []
    assert captured_args["args"] == ["query", "lex: exact phrase\nintent: debug telegram delays", "--json", "-n", "3", "-c", "memories"]


def test_export_sessions_sanitizes_tool_noise(tmp_path):
    mgr = _manager(tmp_path)
    session = {
        "id": "sess_123",
        "title": "Noisy session",
        "source": "cli",
        "started_at": 2000000000,
        "messages": [
            {"role": "user", "content": "What happened last time?"},
            {
                "role": "tool",
                "tool_name": "todo",
                "content": '{"todos": [{"id": "a", "status": "completed"}], "summary": {"total": 1, "completed": 1}}',
            },
            {
                "role": "tool",
                "tool_name": "search_files",
                "content": '{"total_count": 215, "matches": [{"path": "a.py", "line": 1, "content": "x"}]}',
            },
            {"role": "assistant", "content": "You fixed the polling conflict and isolated the gateways."},
        ],
    }

    fake_db = MagicMock()
    fake_db.export_all.return_value = [session]

    with patch("agent.qmd_memory.SessionDB", return_value=fake_db):
        result = mgr._export_sessions()

    assert result["enabled"] is True
    exported_files = list(mgr.sessions_export_dir.glob("*.md"))
    assert len(exported_files) == 1
    text = exported_files[0].read_text(encoding="utf-8")
    assert "What happened last time?" in text
    assert "todo list updated" in text.lower()
    assert "search_files returned 215 matches" in text.lower()
    assert '"todos"' not in text
    assert '"matches"' not in text
