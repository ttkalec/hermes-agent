"""Tests for Telegram early placeholder ("Thinking…") on long turns.

When a Telegram text message triggers a turn that takes a long time,
the adapter should send a lightweight placeholder message so the user
sees an immediate ack rather than silence for minutes.
"""

import asyncio
import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import (
    BasePlatformAdapter,
    MessageEvent,
    MessageType,
    SendResult,
    SessionSource,
)


def _make_adapter(*, platform=Platform.TELEGRAM, handler_delay=0.0, handler_response="Hello!"):
    """Create a minimal BasePlatformAdapter subclass for testing.

    Args:
        platform: Which platform to simulate.
        handler_delay: How long the message handler takes (seconds).
        handler_response: What the handler returns.
    """
    config = PlatformConfig(enabled=True, token="test-token")

    class _TestAdapter(BasePlatformAdapter):
        name = "test"

        async def connect(self):
            pass

        async def disconnect(self):
            pass

        async def send(self, chat_id, content, reply_to=None, metadata=None) -> SendResult:
            return SendResult(success=True, message_id="msg_1")

        async def send_typing(self, chat_id, metadata=None):
            pass

        async def send_image(self, chat_id, image_url, caption=None, reply_to=None, metadata=None) -> SendResult:
            return SendResult(success=True)

        async def send_voice(self, chat_id, audio_path, metadata=None) -> SendResult:
            return SendResult(success=True)

        async def get_chat_info(self, chat_id):
            return {}

    adapter = _TestAdapter(config, platform)
    adapter.platform = platform
    adapter._active_sessions = {}
    adapter._pending_messages = {}
    adapter._background_tasks = set()

    # Set up the message handler with configurable delay
    async def _slow_handler(event):
        if handler_delay > 0:
            await asyncio.sleep(handler_delay)
        return handler_response

    adapter._message_handler = _slow_handler

    # Spy on send
    adapter.send = AsyncMock(return_value=SendResult(success=True, message_id="msg_placeholder"))
    adapter.send_typing = AsyncMock()
    adapter.edit_message = AsyncMock(return_value=SendResult(success=False, error="Not supported"))

    return adapter


def _make_event(text="Hello", msg_type=MessageType.TEXT, chat_id="12345"):
    return MessageEvent(
        text=text,
        message_type=msg_type,
        source=SessionSource(platform=Platform.TELEGRAM, chat_id=chat_id, chat_type="dm"),
    )


class TestEarlyPlaceholder:
    @pytest.mark.asyncio
    async def test_placeholder_sent_on_long_turn(self):
        """A turn that takes longer than the grace period should send a placeholder."""
        adapter = _make_adapter(handler_delay=0.5, handler_response="Done!")

        event = _make_event()

        with patch.dict(os.environ, {"HERMES_TELEGRAM_PLACEHOLDER_GRACE_SECONDS": "0.1"}):
            await adapter._process_message_background(event, "session_1")

        # Should have multiple sends: placeholder + final response
        calls = adapter.send.call_args_list
        assert len(calls) >= 2, f"Expected >=2 sends (placeholder + response), got {len(calls)}"

        # First send should be the placeholder
        first_content = calls[0].kwargs.get("content") or calls[0][1].get("content", "")
        assert "💭" in first_content

    @pytest.mark.asyncio
    async def test_placeholder_not_sent_on_fast_turn(self):
        """A turn that completes quickly should NOT send a placeholder."""
        adapter = _make_adapter(handler_delay=0.0, handler_response="Quick reply")

        event = _make_event()

        with patch.dict(os.environ, {"HERMES_TELEGRAM_PLACEHOLDER_GRACE_SECONDS": "0.5"}):
            await adapter._process_message_background(event, "session_2")

        # Should only have the final response send (no placeholder)
        calls = adapter.send.call_args_list
        contents = [
            c.kwargs.get("content") or c[1].get("content", "")
            for c in calls
        ]
        placeholder_sends = [c for c in contents if c == "💭"]
        assert len(placeholder_sends) == 0, "Placeholder should not be sent for fast turns"

    @pytest.mark.asyncio
    async def test_placeholder_not_sent_for_commands(self):
        """Commands should not trigger placeholder."""
        adapter = _make_adapter(handler_delay=0.5, handler_response="/status output")

        event = _make_event(text="/status", msg_type=MessageType.COMMAND)

        with patch.dict(os.environ, {"HERMES_TELEGRAM_PLACEHOLDER_GRACE_SECONDS": "0.1"}):
            await adapter._process_message_background(event, "session_3")

        calls = adapter.send.call_args_list
        contents = [
            c.kwargs.get("content") or c[1].get("content", "")
            for c in calls
        ]
        placeholder_sends = [c for c in contents if c == "💭"]
        assert len(placeholder_sends) == 0

    @pytest.mark.asyncio
    async def test_placeholder_not_sent_for_non_telegram(self):
        """Non-Telegram platforms should not get placeholder."""
        adapter = _make_adapter(platform=Platform.DISCORD, handler_delay=0.5, handler_response="Done")

        event = _make_event()
        event.source.platform = Platform.DISCORD

        with patch.dict(os.environ, {"HERMES_TELEGRAM_PLACEHOLDER_GRACE_SECONDS": "0.1"}):
            await adapter._process_message_background(event, "session_4")

        calls = adapter.send.call_args_list
        contents = [
            c.kwargs.get("content") or c[1].get("content", "")
            for c in calls
        ]
        placeholder_sends = [c for c in contents if c == "💭"]
        assert len(placeholder_sends) == 0

    @pytest.mark.asyncio
    async def test_placeholder_sent_at_recorded_in_trace(self):
        """When placeholder fires, placeholder_sent_at should be in the event trace."""
        adapter = _make_adapter(handler_delay=0.5, handler_response="Done!")

        event = _make_event()

        with patch.dict(os.environ, {"HERMES_TELEGRAM_PLACEHOLDER_GRACE_SECONDS": "0.1"}):
            await adapter._process_message_background(event, "session_5")

        assert "placeholder_sent_at" in event.trace

    @pytest.mark.asyncio
    async def test_no_placeholder_on_empty_response(self):
        """If the handler returns None, placeholder still works (no crash)."""
        adapter = _make_adapter(handler_delay=0.3, handler_response=None)

        event = _make_event()

        with patch.dict(os.environ, {"HERMES_TELEGRAM_PLACEHOLDER_GRACE_SECONDS": "0.1"}):
            # Should not raise
            await adapter._process_message_background(event, "session_6")
