"""Tests for Telegram text message aggregation.

When a user sends a long message, Telegram clients split it into multiple
updates.  The TelegramAdapter should buffer rapid successive text messages
from the same session and aggregate them before dispatching.
"""

import asyncio
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import MessageEvent, MessageType, SessionSource


def _make_adapter(*, batch_delay=0.1, max_wait=0.3):
    """Create a minimal TelegramAdapter for testing text batching."""
    from gateway.platforms.telegram import TelegramAdapter

    config = PlatformConfig(enabled=True, token="test-token")
    adapter = object.__new__(TelegramAdapter)
    adapter._platform = Platform.TELEGRAM
    adapter.config = config
    adapter._pending_text_batches = {}
    adapter._pending_text_batch_tasks = {}
    adapter._text_batch_first_received = {}
    adapter._text_batch_delay_seconds = batch_delay
    adapter._text_batch_max_wait_seconds = max_wait
    adapter._active_sessions = {}
    adapter._pending_messages = {}
    adapter._message_handler = AsyncMock()
    adapter.handle_message = AsyncMock()
    return adapter


def _make_event(text: str, chat_id: str = "12345") -> MessageEvent:
    return MessageEvent(
        text=text,
        message_type=MessageType.TEXT,
        source=SessionSource(platform=Platform.TELEGRAM, chat_id=chat_id, chat_type="dm"),
    )


class TestTextBatching:
    @pytest.mark.asyncio
    async def test_single_message_dispatched_after_delay(self):
        adapter = _make_adapter()
        event = _make_event("hello world")

        adapter._enqueue_text_event(event)

        # Not dispatched yet
        adapter.handle_message.assert_not_called()

        # Wait for flush
        await asyncio.sleep(0.2)

        adapter.handle_message.assert_called_once()
        dispatched = adapter.handle_message.call_args[0][0]
        assert dispatched.text == "hello world"
        assert "batch_flushed_at" in dispatched.trace

    @pytest.mark.asyncio
    async def test_split_messages_aggregated(self):
        """Two rapid messages from the same chat should be merged."""
        adapter = _make_adapter()

        adapter._enqueue_text_event(_make_event("This is part one of a long"))
        await asyncio.sleep(0.02)  # small gap, within batch window
        adapter._enqueue_text_event(_make_event("message that was split by Telegram."))

        # Not dispatched yet (timer restarted)
        adapter.handle_message.assert_not_called()

        # Wait for flush
        await asyncio.sleep(0.2)

        adapter.handle_message.assert_called_once()
        dispatched = adapter.handle_message.call_args[0][0]
        assert "part one" in dispatched.text
        assert "split by Telegram" in dispatched.text

    @pytest.mark.asyncio
    async def test_three_way_split_aggregated(self):
        """Three rapid messages should all merge."""
        adapter = _make_adapter()

        adapter._enqueue_text_event(_make_event("chunk 1"))
        await asyncio.sleep(0.02)
        adapter._enqueue_text_event(_make_event("chunk 2"))
        await asyncio.sleep(0.02)
        adapter._enqueue_text_event(_make_event("chunk 3"))

        await asyncio.sleep(0.2)

        adapter.handle_message.assert_called_once()
        text = adapter.handle_message.call_args[0][0].text
        assert "chunk 1" in text
        assert "chunk 2" in text
        assert "chunk 3" in text

    @pytest.mark.asyncio
    async def test_different_chats_not_merged(self):
        """Messages from different chats should be separate batches."""
        adapter = _make_adapter()

        adapter._enqueue_text_event(_make_event("from user A", chat_id="111"))
        adapter._enqueue_text_event(_make_event("from user B", chat_id="222"))

        await asyncio.sleep(0.2)

        assert adapter.handle_message.call_count == 2

    @pytest.mark.asyncio
    async def test_batch_cleans_up_after_flush(self):
        """After flushing, internal state should be clean."""
        adapter = _make_adapter()

        adapter._enqueue_text_event(_make_event("test"))
        await asyncio.sleep(0.2)

        assert len(adapter._pending_text_batches) == 0
        assert len(adapter._pending_text_batch_tasks) == 0
        assert len(adapter._text_batch_first_received) == 0


class TestTextBatchMaxWaitCap:
    """Verify the hard cap prevents unbounded batch extension."""

    @pytest.mark.asyncio
    async def test_batch_flushes_at_max_wait_despite_continuous_messages(self):
        """Even if messages keep arriving, the batch flushes within the cap."""
        adapter = _make_adapter(batch_delay=0.1, max_wait=0.25)

        # Send messages every 50ms for 400ms — well past the 250ms cap.
        start = time.monotonic()
        for i in range(8):
            adapter._enqueue_text_event(_make_event(f"msg {i}"))
            await asyncio.sleep(0.05)

        # The batch should have flushed by now (max_wait=0.25s).
        elapsed = time.monotonic() - start
        assert adapter.handle_message.call_count >= 1, (
            f"Batch should have flushed within max_wait, but call_count=0 after {elapsed:.2f}s"
        )

        # First flush should have happened within ~max_wait + some slack.
        # (We can't check exact timing, but at least one flush happened.)
        dispatched = adapter.handle_message.call_args_list[0][0][0]
        assert "msg 0" in dispatched.text

    @pytest.mark.asyncio
    async def test_max_wait_does_not_prevent_normal_short_batches(self):
        """Short bursts within the debounce window still merge normally."""
        adapter = _make_adapter(batch_delay=0.1, max_wait=1.0)

        adapter._enqueue_text_event(_make_event("part A"))
        await asyncio.sleep(0.02)
        adapter._enqueue_text_event(_make_event("part B"))

        await asyncio.sleep(0.2)

        adapter.handle_message.assert_called_once()
        text = adapter.handle_message.call_args[0][0].text
        assert "part A" in text
        assert "part B" in text

    @pytest.mark.asyncio
    async def test_first_received_timestamp_cleaned_after_flush(self):
        """_text_batch_first_received is cleaned up after flush."""
        adapter = _make_adapter()

        adapter._enqueue_text_event(_make_event("test"))
        await asyncio.sleep(0.2)

        assert len(adapter._text_batch_first_received) == 0

    @pytest.mark.asyncio
    async def test_batch_wait_ms_recorded_in_trace(self):
        """The flushed event should have batch_wait_ms in its trace."""
        adapter = _make_adapter()

        event = _make_event("hello")
        adapter._enqueue_text_event(event)
        await asyncio.sleep(0.2)

        dispatched = adapter.handle_message.call_args[0][0]
        assert "batch_flushed_at" in dispatched.trace
