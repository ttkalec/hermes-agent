import logging
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import MessageEvent
from gateway.session import SessionSource


def _ensure_telegram_mock():
    if "telegram" in sys.modules and hasattr(sys.modules["telegram"], "__file__"):
        return

    telegram_mod = MagicMock()
    telegram_mod.ext.ContextTypes.DEFAULT_TYPE = type(None)
    telegram_mod.constants.ParseMode.MARKDOWN_V2 = "MarkdownV2"
    telegram_mod.constants.ChatType.GROUP = "group"
    telegram_mod.constants.ChatType.SUPERGROUP = "supergroup"
    telegram_mod.constants.ChatType.CHANNEL = "channel"
    telegram_mod.constants.ChatType.PRIVATE = "private"

    for name in ("telegram", "telegram.ext", "telegram.constants"):
        sys.modules.setdefault(name, telegram_mod)


_ensure_telegram_mock()

from gateway.platforms.telegram import TelegramAdapter  # noqa: E402


@pytest.mark.asyncio
async def test_send_typing_warns_after_consecutive_failures_and_resets(caplog):
    adapter = TelegramAdapter(PlatformConfig(enabled=True, token="***"))
    adapter._bot = SimpleNamespace(
        send_chat_action=AsyncMock(
            side_effect=[RuntimeError("boom-1"), RuntimeError("boom-2"), RuntimeError("boom-3"), None]
        )
    )

    event = MessageEvent(
        text="hello",
        source=SessionSource(platform=Platform.TELEGRAM, chat_id="123", chat_type="dm"),
        message_id="99",
    )
    metadata = {"_turn_event": event, "turn_correlation_id": event.correlation_id}

    with caplog.at_level(logging.WARNING):
        await adapter.send_typing("123", metadata=metadata)
        await adapter.send_typing("123", metadata=metadata)
        await adapter.send_typing("123", metadata=metadata)

    assert "typing indicator failures" in caplog.text
    assert adapter._typing_failure_counts["123"] == 3

    await adapter.send_typing("123", metadata=metadata)

    assert adapter._typing_failure_counts.get("123", 0) == 0
    assert "first_typing_sent_at" in event.trace
