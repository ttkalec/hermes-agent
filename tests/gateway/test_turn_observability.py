from datetime import datetime, timedelta

from gateway.config import Platform
from gateway.platforms.base import MessageEvent
from gateway.session import SessionSource
from gateway.turn_trace import format_turn_snapshot, record_timestamp, set_phase, slow_turn_warning_message


def _make_event(ts=None):
    return MessageEvent(
        text="hello",
        timestamp=ts or datetime(2026, 3, 18, 19, 0, 0),
        source=SessionSource(
            platform=Platform.TELEGRAM,
            chat_id="123",
            chat_type="dm",
        ),
        message_id="42",
    )


def test_message_event_initializes_correlation_and_received_trace():
    ts = datetime(2026, 3, 18, 19, 0, 0)
    event = _make_event(ts)

    assert event.correlation_id
    assert event.turn_phase == "received"
    assert event.trace["received_at"] == ts


def test_turn_snapshot_reports_phase_and_timestamps():
    event = _make_event()
    set_phase(event, "waiting_for_model")
    record_timestamp(event, "batch_flushed_at", when=event.timestamp + timedelta(seconds=1))

    snapshot = format_turn_snapshot(event)

    assert "phase=waiting_for_model" in snapshot
    assert "received_at=" in snapshot
    assert "batch_flushed_at=" in snapshot


def test_slow_turn_warning_message_includes_elapsed_and_phase():
    event = _make_event()
    set_phase(event, "tool:web_search")
    record_timestamp(event, "first_model_call_at", when=event.timestamp + timedelta(seconds=2))

    warning = slow_turn_warning_message(event, elapsed_seconds=30.0)

    assert "slow_turn" in warning
    assert "elapsed=30.0s" in warning
    assert "phase=tool:web_search" in warning
    assert "first_model_call_at=" in warning
