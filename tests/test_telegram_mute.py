"""
tests/test_telegram_mute.py

Covers standing-list item 17 (README/CLAUDE.md "Next up" / KNOWN DEBT):
operator /mute and /unmute commands so Tier 2 chatter (trade opened/
resolved, rejected opportunity) can be silenced during high-volume paper
trading without disabling the agent entirely. Tier 1 (leg failure, feed
health) and permission requests must keep bypassing mute — the Session 20
feed-down alert was explicitly designed never to be silenced.
"""

import sys
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

PROJECT_ROOT = Path(__file__).parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from core.events import (
    EventBus,
    FeedHealthEvent,
    LegFailureEvent,
    RejectedOpportunityEvent,
    TelegramNotificationEvent,
    TradeExecutedEvent,
    TradeResolvedEvent,
)
from karbot.core.config import KarbotConfig, TelegramConfig
from agents.notifications.telegram_agent import TelegramNotificationAgent


def _make_agent() -> TelegramNotificationAgent:
    config = KarbotConfig()
    config.telegram = TelegramConfig(enabled=True, notify_on_trade=True, notify_on_rejection=True)
    bus = EventBus()
    agent = TelegramNotificationAgent(bus=bus, config=config)
    agent.register_subscriptions()
    agent.bus.publish = AsyncMock()
    agent._outbound_queue.put = AsyncMock(wraps=agent._outbound_queue.put)
    return agent


class TestMuteCommand:
    @pytest.mark.asyncio
    async def test_mute_sets_flag(self):
        agent = _make_agent()
        assert agent._muted is False
        await agent._handle_operator_reply("/mute")
        assert agent._muted is True

    @pytest.mark.asyncio
    async def test_mute_is_case_insensitive(self):
        agent = _make_agent()
        await agent._handle_operator_reply("/MUTE")
        assert agent._muted is True

    @pytest.mark.asyncio
    async def test_mute_sends_confirmation(self):
        agent = _make_agent()
        await agent._handle_operator_reply("/mute")
        text = agent._outbound_queue.put.await_args.args[0]
        assert "mute" in text.lower()

    @pytest.mark.asyncio
    async def test_unmute_clears_flag(self):
        agent = _make_agent()
        agent._muted = True
        await agent._handle_operator_reply("/unmute")
        assert agent._muted is False

    @pytest.mark.asyncio
    async def test_unmute_sends_confirmation(self):
        agent = _make_agent()
        agent._muted = True
        await agent._handle_operator_reply("/unmute")
        text = agent._outbound_queue.put.await_args.args[0]
        assert "unmute" in text.lower()

    @pytest.mark.asyncio
    async def test_mute_does_not_resolve_a_pending_permission_request(self):
        """Like the kill switch, /mute must short-circuit before the
        yes/no permission-resolution path — it isn't a permission reply."""
        agent = _make_agent()
        agent._pending_requests["req-1"] = object()
        await agent._handle_operator_reply("/mute")
        assert "req-1" in agent._pending_requests

    @pytest.mark.asyncio
    async def test_ordinary_message_does_not_toggle_mute(self):
        agent = _make_agent()
        await agent._handle_operator_reply("yes")
        assert agent._muted is False

    @pytest.mark.asyncio
    async def test_word_mute_without_slash_does_not_toggle(self):
        """Bare 'mute' could appear in an ordinary reply; require the
        explicit /mute command form to avoid accidental toggling."""
        agent = _make_agent()
        await agent._handle_operator_reply("mute")
        assert agent._muted is False


class TestMuteSuppressesTier2Only:
    @pytest.mark.asyncio
    async def test_trade_executed_suppressed_when_muted(self):
        agent = _make_agent()
        agent._muted = True
        await agent._handle_trade_executed(TradeExecutedEvent(trade_id="t1"))
        agent._outbound_queue.put.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_trade_executed_sends_when_unmuted(self):
        agent = _make_agent()
        await agent._handle_trade_executed(TradeExecutedEvent(trade_id="t1"))
        agent._outbound_queue.put.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_trade_resolved_suppressed_when_muted(self):
        agent = _make_agent()
        agent._muted = True
        await agent._handle_trade_resolved(TradeResolvedEvent(trade_id="t1"))
        agent._outbound_queue.put.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_rejected_opportunity_suppressed_when_muted(self):
        agent = _make_agent()
        agent._muted = True
        await agent._handle_rejected_opportunity(
            RejectedOpportunityEvent(opportunity_id="o1", reason="test")
        )
        agent._outbound_queue.put.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_tier2_generic_notification_suppressed_when_muted(self):
        agent = _make_agent()
        agent._muted = True
        await agent._handle_notification(
            TelegramNotificationEvent(message="fyi", tier=2)
        )
        agent._outbound_queue.put.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_tier1_generic_notification_not_suppressed_when_muted(self):
        agent = _make_agent()
        agent._muted = True
        await agent._handle_notification(
            TelegramNotificationEvent(message="critical", tier=1)
        )
        agent._outbound_queue.put.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_leg_failure_not_suppressed_when_muted(self):
        agent = _make_agent()
        agent._muted = True
        await agent._handle_leg_failure(LegFailureEvent(trade_id="t1"))
        agent._outbound_queue.put.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_feed_down_not_suppressed_when_muted(self):
        """The Session 20 feed-down alert must keep bypassing mute —
        an inventory-bearing agent going dark is not optional chatter."""
        agent = _make_agent()
        agent._muted = True
        agent._feed_connected["kalshi"] = True  # establish a prior state
        await agent._handle_feed_health(
            FeedHealthEvent(platform="kalshi", connected=False)
        )
        agent._outbound_queue.put.assert_awaited_once()
        text = agent._outbound_queue.put.await_args.args[0]
        assert "FEED DOWN" in text
