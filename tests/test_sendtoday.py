"""/sendtoday tests — all network faked (see test_spec_rules fakes + conftest)."""
from __future__ import annotations

from datetime import timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.bot.handlers import cmd_sendtoday, format_sendtoday_summary
from app.models.mint import MintStatus
from tests.test_spec_rules import (  # noqa: F401  (env fixture)
    GROUP, T0, TOPIC, discover, env, fire, make_drop, make_stage, notifications, only_row,
)
from app.database.models import MintOpportunityDB
from sqlalchemy import select

START = T0 + timedelta(hours=3)  # same UTC day as T0 (12:00 -> 15:00)


@pytest.mark.asyncio
async def test_sendtoday_sends_eligible_now_once_and_blocks_auto_duplicate(env):
    await discover(env, make_stage(START))
    rec = await only_row(env)
    assert len(env["notifier"].sent) == 1  # discovery on qualify
    env["clock"].now = T0 + timedelta(minutes=5)
    res = await env["svc"].send_today()
    assert [r["sent"] for r in res] == [True]
    # discovery + early reminder
    assert env["notifier"].sent == [(GROUP, TOPIC, rec.id), (GROUP, TOPIC, rec.id)]
    row = await only_row(env)
    assert row.status == MintStatus.NOTIFIED.value and row.notification_sent_at is not None
    assert len(await notifications(env)) == 2
    # automatic job is cancelled; even if a stale one fires it does not resend
    assert f"notify_{rec.id}" not in env["sched"].jobs
    env["clock"].now = START - timedelta(minutes=15)
    assert await env["svc"].send_alert(rec.id) is False
    # running /sendtoday again doesn't resend
    res2 = await env["svc"].send_today()
    assert res2[0]["sent"] is False and res2[0]["reason"] == "already alerted"
    assert len(env["notifier"].sent) == 2


@pytest.mark.asyncio
async def test_sendtoday_schedules_deletion_at_start_plus_10(env):
    await discover(env, make_stage(START))
    rec = await only_row(env)
    await env["svc"].send_today()
    assert env["sched"].jobs[f"delete_{rec.id}"]["run_at"] == START + timedelta(minutes=10)
    assert (await notifications(env))[0].message_thread_id == TOPIC


@pytest.mark.asyncio
async def test_sendtoday_skips_when_fresh_data_fails_and_keeps_auto_schedule(env):
    # Pending at discovery (no discovery alert) so we can assert sendtoday sends nothing.
    await discover(env, make_stage(START), minted="600")
    rec = await only_row(env)
    env["provider"].drop.total_supply = "800"
    env["provider"].offer = 13.0  # $10 mint needs $14
    res = await env["svc"].send_today()
    assert res[0]["sent"] is False and "RULE2_OFFER_RATIO" in res[0]["reason"]
    assert env["notifier"].sent == []
    assert (await only_row(env)).status == MintStatus.SCHEDULED.value
    assert f"notify_{rec.id}" in env["sched"].jobs


@pytest.mark.asyncio
async def test_sendtoday_ignores_other_days_and_started_mints(env):
    await discover(env, make_stage(T0 + timedelta(days=1, hours=1), uid="tomorrow"))
    await discover(env, make_stage(START, uid="today"))
    discovery_count = len(env["notifier"].sent)  # both were qualified at discovery
    env["clock"].now = START + timedelta(minutes=1)  # today's mint has started
    res = await env["svc"].send_today()
    assert res == []
    assert len(env["notifier"].sent) == discovery_count  # no extra reminder from /sendtoday


@pytest.mark.asyncio
async def test_sendtoday_rechecks_rejected_mint_on_fresh_data(env):
    # 50% minted at discovery is a dynamic failure -> scheduled; mark rejected to simulate
    await discover(env, make_stage(START), minted="500")
    async with env["factory"]() as s:
        r = (await s.execute(select(MintOpportunityDB))).scalar_one()
        r.status = MintStatus.REJECTED.value
        await s.commit()
    res = await env["svc"].send_today()  # fresh data still 50%
    assert res[0]["sent"] is False and "RULE1C_MINTED" in res[0]["reason"]
    env["provider"].drop.total_supply = "900"
    res = await env["svc"].send_today()
    assert res[0]["sent"] is True


def test_summary_lists_sent_and_skipped_with_reasons():
    out = format_sendtoday_summary([
        {"name": "A", "start": START, "sent": True, "reason": "sent"},
        {"name": "B<x>", "start": START, "sent": False, "reason": "already alerted"},
    ])
    assert "1 sent, 1 skipped" in out and "A" in out
    assert "B&lt;x&gt; — already alerted" in out


@pytest.mark.asyncio
async def test_cmd_sendtoday_admin_only():
    update = MagicMock()
    update.effective_user.id = 999
    update.effective_message.reply_text = AsyncMock()
    monitor = MagicMock()
    monitor.alerts.send_today = AsyncMock(return_value=[])
    ctx = MagicMock()
    ctx.bot_data = {"admin_ids": [1], "monitor": monitor, "settings": MagicMock(restrict_to_admins=False)}
    await cmd_sendtoday(update, ctx)
    monitor.alerts.send_today.assert_not_called()

    update.effective_user.id = 1
    await cmd_sendtoday(update, ctx)
    monitor.alerts.send_today.assert_awaited_once()
    assert "No upcoming mints" in update.effective_message.reply_text.call_args[0][0]
