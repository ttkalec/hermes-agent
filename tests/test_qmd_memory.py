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