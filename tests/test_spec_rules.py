"""
Spec rule tests (rules 1-5). Every external dependency is faked:
- OpenSea / price feeds  -> FakeProvider (no HTTP)
- Telegram               -> FakeNotifier (records calls, sends nothing)
- APScheduler            -> FakeScheduler (records jobs; tests "fire" them)
- Clock                  -> FakeClock
Network sockets are additionally blocked by tests/conftest.py.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Optional

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config import Settings
from app.database.models import Base, MintOpportunityDB, NotificationDB
from app.filters.mint_filter import FilterConfig, MintFilter
from app.models.mint import MintOpportunity, MintStatus
from app.monitor.processor import AlertService
from app.providers.base import RawDrop, RawDropStage
from app.providers.opensea import OpenSeaProvider

GROUP = -1001111111111
TOPIC = 4242
ETH_USD = Decimal("2000")
T0 = datetime(2026, 10, 20, 12, 0, tzinfo=timezone.utc)  # "now" at discovery


# ─── Fakes ────────────────────────────────────────────────────────────────────

class FakeClock:
    def __init__(self, now: datetime) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now


class FakeScheduler:
    def __init__(self) -> None:
        self.jobs: dict[str, dict] = {}

    def schedule_notification(self, mint_id, run_at, send_fn):
        self.jobs[f"notify_{mint_id}"] = {"run_at": run_at, "fn": send_fn, "args": [mint_id]}
        return f"notify_{mint_id}"

    def schedule_deletion(self, mint_id, message_id, run_at, delete_fn):
        self.jobs[f"delete_{mint_id}"] = {"run_at": run_at, "fn": delete_fn, "args": [mint_id, message_id]}
        return f"delete_{mint_id}"

    def cancel_job(self, job_id):
        self.jobs.pop(job_id, None)


class FakeNotifier:
    def __init__(self) -> None:
        self.sent: list[tuple[int, Optional[int], str]] = []
        self.deleted: list[tuple[int, int]] = []
        self._next = 100

    async def send_to_target(self, rec, chat_id, topic_id=None):
        self._next += 1
        self.sent.append((chat_id, topic_id, rec.id))
        return self._next

    async def delete_target_message(self, chat_id, message_id, mint_id):
        self.deleted.append((chat_id, message_id))
        return True


class FakeProvider(OpenSeaProvider):
    """Real normalize_mint(); all network methods replaced by in-memory data."""

    def __init__(self) -> None:
        super().__init__(api_key="fake")
        self.drop: Optional[RawDrop] = None
        self.offer: Optional[float] = 20.0
        self.eth: Optional[Decimal] = ETH_USD

    async def get_mint_details(self, slug):
        return self.drop.model_copy(deep=True) if self.drop else None

    async def get_collection_offer_price(self, slug, chain=None, contract_address=None):
        return self.offer

    async def get_reliable_eth_price_usd(self):
        return self.eth

    async def get_eth_price_usd(self):  # pragma: no cover - must not be used for thresholds
        raise AssertionError("unreliable price path used")


def wei_for_usd(usd: str) -> str:
    return str(int(Decimal(usd) / ETH_USD * Decimal(10) ** 18))


def make_stage(start: datetime, *, price_usd="10", stage_type="public_sale", label="Public",
               allowlist=None, uid="stage-1", end: Optional[datetime] = None) -> RawDropStage:
    return RawDropStage(
        uuid=uid, stage_type=stage_type, label=label,
        price=wei_for_usd(price_usd) if price_usd is not None else None,
        price_currency_address="0x0000000000000000000000000000000000000000",
        start_time=start.isoformat(), end_time=(end or start + timedelta(hours=2)).isoformat(),
        max_per_wallet="5", allowlist_wallet_count=allowlist,
    )


def make_drop(stage: RawDropStage, minted="800", max_supply="1000", chain="ethereum") -> RawDrop:
    return RawDrop(
        collection_slug="cool", collection_name="Cool Cats", chain=chain,
        contract_address="0xAbC0000000000000000000000000000000000001",
        drop_type="seadrop", is_minting=False, opensea_url="https://opensea.io/collection/cool",
        next_stage=stage, stages=[stage], total_supply=minted, max_supply=max_supply,
    )


def settings() -> Settings:
    return Settings(
        telegram_bot_token="fake-token", telegram_group_id=GROUP, public_mint_topic_id=TOPIC,
        opensea_api_key="fake-key", _env_file=None,
    )


@pytest_asyncio.fixture
async def env():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    clock = FakeClock(T0)
    provider, notifier, sched = FakeProvider(), FakeNotifier(), FakeScheduler()
    svc = AlertService(settings(), provider, notifier, sched, session_factory=factory, clock=clock)
    yield {"svc": svc, "clock": clock, "provider": provider, "notifier": notifier,
           "sched": sched, "factory": factory}
    await engine.dispose()


async def discover(env, stage: RawDropStage, **drop_kw) -> str:
    drop = make_drop(stage, **drop_kw)
    env["provider"].drop = drop
    return await env["svc"].process(drop.model_copy(deep=True), stage, ETH_USD)


async def only_row(env) -> MintOpportunityDB:
    async with env["factory"]() as s:
        rows = (await s.execute(select(MintOpportunityDB))).scalars().all()
    assert len(rows) == 1
    return rows[0]


async def notifications(env) -> list[NotificationDB]:
    async with env["factory"]() as s:
        return list((await s.execute(select(NotificationDB))).scalars().all())


async def fire(env, job_id: str, at: Optional[datetime] = None):
    job = env["sched"].jobs.pop(job_id)
    env["clock"].now = at or job["run_at"]
    if job_id.startswith("notify_"):
        return await env["svc"].send_alert(*job["args"][:1])
    return await env["svc"].delete_alert(job["args"][0])


START = T0 + timedelta(hours=2)


# ─── Rule 1: eligibility ──────────────────────────────────────────────────────

def _mint(**kw) -> MintOpportunity:
    base = dict(
        external_id="x", project_name="p", chain="ethereum", contract_address="0x1",
        mint_type="public_sale", mint_price_usd=Decimal("10"), offer_price_usd=Decimal("14"),
        minted_percentage=Decimal("70"), mint_start_time=T0 + timedelta(hours=1),
        mint_url="u", collection_slug="c",
    )
    base.update(kw)
    return MintOpportunity(**base)


def ev(**kw):
    return MintFilter(FilterConfig()).evaluate(_mint(**kw), now=T0)


def test_rule1_baseline_passes():
    assert ev().passed


@pytest.mark.parametrize("label", ["Allowlist", "Whitelist Phase", "WL", "Presale", "GTD", "OG mint"])
def test_rule1_public_stage_with_allowlist_label_rejected(label):
    r = ev(stage_label=label)
    assert not r.passed and r.rule == "RULE1A_PUBLIC"


def test_rule1_public_stage_with_allowlist_wallets_rejected():
    r = ev(allowlist_wallet_count=250)
    assert not r.passed and r.rule == "RULE1A_PUBLIC"


def test_rule1_price_cap_is_13_usd():
    assert ev(mint_price_usd=Decimal("13"), offer_price_usd=Decimal("18.2")).passed
    r = ev(mint_price_usd=Decimal("13.001"), offer_price_usd=Decimal("100"))
    assert not r.passed and r.rule == "RULE1B_PRICE"


def test_rule1_unknown_price_rejected_not_treated_as_free():
    r = ev(mint_price_usd=Decimal("0"), price_unreliable_reason="stage price missing")
    assert not r.passed and r.rule == "RULE1B_PRICE"


def test_rule1_minted_70_percent_required_and_missing_supply_rejected():
    assert not ev(minted_percentage=Decimal("69.99")).passed
    r = ev(minted_percentage=None)
    assert not r.passed and r.rule == "RULE1C_MINTED"


def test_rule1_start_time_must_be_reliable():
    assert ev(mint_start_time=T0 - timedelta(minutes=1)).rule == "RULE1D_START_TIME"
    assert ev(mint_start_time=T0 + timedelta(days=400)).rule == "RULE1D_START_TIME"
    assert ev(mint_end_time=T0 + timedelta(minutes=30)).rule == "RULE1D_START_TIME"  # end < start
    assert ev(start_time_unreliable_reason="placeholder").rule == "RULE1D_START_TIME"


def test_rule1_offer_must_exist_and_be_credible():
    assert ev(offer_price_usd=None).rule == "RULE1E_OFFER"
    assert ev(mint_price_usd=Decimal("0"), offer_price_usd=Decimal("0")).rule == "RULE1E_OFFER"


def test_rule1_config_cannot_loosen_spec():
    cfg = FilterConfig(max_mint_price_usd=Decimal("50"), min_offer_multiplier=Decimal("1.1"),
                       min_minted_percentage=Decimal("10"))
    assert cfg.max_mint_price_usd == Decimal("13")
    assert cfg.min_offer_multiplier == Decimal("1.40")
    assert cfg.min_minted_percentage == Decimal("70")
    with pytest.raises(Exception):
        Settings(telegram_bot_token="t", telegram_group_id=1, public_mint_topic_id=1,
                 opensea_api_key="k", max_mint_price_usd=Decimal("20"), _env_file=None)


@pytest.mark.asyncio
async def test_rule1_normalize_rejects_unreliable_price_sources():
    p = FakeProvider()
    st = make_stage(START)
    # Non-ETH native chain: ETH/USD conversion would be wrong
    m = await p.normalize_mint(make_drop(st, chain="polygon"), st, float(ETH_USD), offer_price_usd=20)
    assert m.price_unreliable_reason
    # No live ETH price
    m = await p.normalize_mint(make_drop(st), st, None, offer_price_usd=20)
    assert m.price_unreliable_reason
    # Missing price is NOT free
    st2 = make_stage(START, price_usd=None)
    m = await p.normalize_mint(make_drop(st2), st2, float(ETH_USD), offer_price_usd=20)
    assert m.price_unreliable_reason


@pytest.mark.asyncio
async def test_rule1_offer_in_unknown_currency_is_not_credible():
    p = OpenSeaProvider(api_key="fake")

    async def fake_get(url, params=None):
        return {"offers": [{"status": "ACTIVE", "price": {"value": "5000000000000000000",
                                                          "decimals": 18, "currency": "MEME"}}]}

    async def eth():
        return ETH_USD

    p._get = fake_get
    p.get_reliable_eth_price_usd = eth
    assert await p.get_collection_offer_price("c") is None


# ─── Rule 2: offer >= mint x 1.40 (gross) ─────────────────────────────────────

def test_rule2_ten_dollar_mint_needs_fourteen_offer():
    assert ev(mint_price_usd=Decimal("10"), offer_price_usd=Decimal("14")).passed
    r = ev(mint_price_usd=Decimal("10"), offer_price_usd=Decimal("13.99"))
    assert not r.passed and r.rule == "RULE2_OFFER_RATIO"


def test_rule2_is_exact_no_rounding_in_favour():
    # 9.996 x 1.4 = 13.9944 -> offer 13.99 must fail (old code rounded required to 13.99)
    assert not ev(mint_price_usd=Decimal("9.996"), offer_price_usd=Decimal("13.99")).passed


def test_rule2_uses_gross_offer_no_fee_deduction():
    # exactly 1.40x passes; fees are not subtracted from the offer
    assert ev(mint_price_usd=Decimal("5"), offer_price_usd=Decimal("7")).passed


# ─── Rule 3: one alert at start - 15m, re-checked, right destination ──────────

@pytest.mark.asyncio
async def test_rule3_discovery_never_sends_and_schedules_at_minus_15(env):
    assert await discover(env, make_stage(START)) == "scheduled"
    assert env["notifier"].sent == []
    rec = await only_row(env)
    assert env["sched"].jobs[f"notify_{rec.id}"]["run_at"] == START - timedelta(minutes=15)


@pytest.mark.asyncio
async def test_rule3_sends_once_to_configured_group_and_topic(env):
    await discover(env, make_stage(START))
    rec = await only_row(env)
    assert await fire(env, f"notify_{rec.id}") is True
    assert env["notifier"].sent == [(GROUP, TOPIC, rec.id)]
    # a duplicate/stale job fire does not send again
    env["clock"].now = START - timedelta(minutes=15)
    assert await env["svc"].send_alert(rec.id) is False
    assert len(env["notifier"].sent) == 1


@pytest.mark.asyncio
async def test_rule3_ignores_extra_broadcast_targets(env):
    from app.database.models import BroadcastTargetDB
    async with env["factory"]() as s:
        s.add(BroadcastTargetDB(chat_id=-100999, topic_id=None, is_active=True))
        await s.commit()
    await discover(env, make_stage(START))
    rec = await only_row(env)
    await fire(env, f"notify_{rec.id}")
    assert [(c, t) for c, t, _ in env["notifier"].sent] == [(GROUP, TOPIC)]


@pytest.mark.asyncio
async def test_rule3_late_discovery_never_alerts(env):
    # found 10 min before start (inside the 15-min window): no alert, ever
    assert await discover(env, make_stage(T0 + timedelta(minutes=10))) == "missed"
    assert env["notifier"].sent == [] and env["sched"].jobs == {}


@pytest.mark.asyncio
async def test_rule3_never_sends_at_or_after_start(env):
    await discover(env, make_stage(START))
    rec = await only_row(env)
    assert await fire(env, f"notify_{rec.id}", at=START) is False
    assert env["notifier"].sent == []
    assert (await only_row(env)).status == MintStatus.MISSED.value


@pytest.mark.asyncio
async def test_rule3_late_job_beyond_grace_is_not_sent(env):
    await discover(env, make_stage(START))
    rec = await only_row(env)
    assert await fire(env, f"notify_{rec.id}", at=START - timedelta(minutes=10)) is False
    assert env["notifier"].sent == []


@pytest.mark.asyncio
@pytest.mark.parametrize("mutate", [
    lambda p: setattr(p, "offer", 13.0),                                     # rule 2 now fails
    lambda p: setattr(p, "offer", None),                                     # no offer
    lambda p: setattr(p.drop, "total_supply", "500"),                        # 50% minted
    lambda p: setattr(p, "eth", None),                                       # no reliable price
    lambda p: setattr(p, "drop", None),                                      # cannot confirm
    lambda p: setattr(p.drop.next_stage, "label", "Allowlist"),              # became gated
])
async def test_rule3_recheck_blocks_send_when_any_condition_fails(env, mutate):
    await discover(env, make_stage(START))
    rec = await only_row(env)
    p = env["provider"]
    if p.drop is not None:
        p.drop.stages = [p.drop.next_stage]
    mutate(p)
    assert await fire(env, f"notify_{rec.id}") is False
    assert env["notifier"].sent == []
    assert (await only_row(env)).status == MintStatus.REJECTED.value


@pytest.mark.asyncio
async def test_rule3_dynamic_failure_at_discovery_is_rechecked_at_send_time(env):
    # 60% minted at discovery, 80% by alert time -> one alert at start-15
    await discover(env, make_stage(START), minted="600")
    rec = await only_row(env)
    assert rec.status == MintStatus.SCHEDULED.value
    env["provider"].drop.total_supply = "800"
    assert await fire(env, f"notify_{rec.id}") is True


@pytest.mark.asyncio
async def test_rule3_static_failure_at_discovery_never_scheduled(env):
    assert await discover(env, make_stage(START, stage_type="allowlist")) == "rejected"
    assert env["sched"].jobs == {}


# ─── Rule 4: delete at start + 10m ────────────────────────────────────────────

@pytest.mark.asyncio
async def test_rule4_deletion_scheduled_at_start_plus_10(env):
    await discover(env, make_stage(START))
    rec = await only_row(env)
    await fire(env, f"notify_{rec.id}")
    job = env["sched"].jobs[f"delete_{rec.id}"]
    assert job["run_at"] == START + timedelta(minutes=10)
    assert (await notifications(env))[0].delete_scheduled_at.replace(tzinfo=timezone.utc) == START + timedelta(minutes=10)
    assert await fire(env, f"delete_{rec.id}") is True
    assert env["notifier"].deleted == [(GROUP, 101)]
    assert (await only_row(env)).status == MintStatus.DELETED.value


@pytest.mark.asyncio
async def test_rule4_early_delete_job_does_not_delete(env):
    await discover(env, make_stage(START))
    rec = await only_row(env)
    await fire(env, f"notify_{rec.id}")
    assert await fire(env, f"delete_{rec.id}", at=START) is False
    assert env["notifier"].deleted == []
    assert env["sched"].jobs[f"delete_{rec.id}"]["run_at"] == START + timedelta(minutes=10)


# ─── Rule 5: dedupe, UTC, reschedule, persistence/recovery ────────────────────

@pytest.mark.asyncio
async def test_rule5_same_phase_seen_again_is_not_duplicated(env):
    st = make_stage(START)
    await discover(env, st)
    await discover(env, st)
    # contract address case differences are the same collection
    drop = make_drop(st)
    drop.contract_address = drop.contract_address.upper().replace("0X", "0x")
    env["provider"].drop = drop
    await env["svc"].process(drop, st, ETH_USD)
    rec = await only_row(env)
    await fire(env, f"notify_{rec.id}")
    await discover(env, st)  # seen again after sending
    assert len(env["notifier"].sent) == 1


@pytest.mark.asyncio
async def test_rule5_times_stored_in_utc():
    p = FakeProvider()
    ist = timezone(timedelta(hours=5, minutes=30))
    st = make_stage(START.astimezone(ist))
    m = await p.normalize_mint(make_drop(st), st, float(ETH_USD), offer_price_usd=20)
    assert m.mint_start_time.utcoffset() == timedelta(0)
    assert m.mint_start_time == START


@pytest.mark.asyncio
async def test_rule5_start_change_before_alert_reschedules_both(env):
    await discover(env, make_stage(START))
    rec = await only_row(env)
    new_start = START + timedelta(hours=1)
    assert await discover(env, make_stage(new_start)) == "rescheduled"
    assert env["sched"].jobs[f"notify_{rec.id}"]["run_at"] == new_start - timedelta(minutes=15)
    row = await only_row(env)
    assert row.delete_scheduled_at.replace(tzinfo=timezone.utc) == new_start + timedelta(minutes=10)
    # old alert time: job no longer fires there; at new time it sends, deletion follows new start
    assert await fire(env, f"notify_{rec.id}") is True
    assert env["sched"].jobs[f"delete_{rec.id}"]["run_at"] == new_start + timedelta(minutes=10)


@pytest.mark.asyncio
async def test_rule5_start_change_detected_at_send_time(env):
    await discover(env, make_stage(START))
    rec = await only_row(env)
    new_start = START + timedelta(minutes=30)
    env["provider"].drop = make_drop(make_stage(new_start))
    assert await fire(env, f"notify_{rec.id}") is False
    assert env["sched"].jobs[f"notify_{rec.id}"]["run_at"] == new_start - timedelta(minutes=15)
    assert await fire(env, f"notify_{rec.id}") is True


@pytest.mark.asyncio
async def test_rule5_start_change_after_alert_reschedules_deletion(env):
    await discover(env, make_stage(START))
    rec = await only_row(env)
    await fire(env, f"notify_{rec.id}")
    new_start = START + timedelta(minutes=20)
    env["clock"].now = START - timedelta(minutes=5)
    assert await discover(env, make_stage(new_start)) == "deletion_rescheduled"
    assert env["sched"].jobs[f"delete_{rec.id}"]["run_at"] == new_start + timedelta(minutes=10)
    assert len(env["notifier"].sent) == 1


@pytest.mark.asyncio
async def test_rule5_recovery_rearms_pending_alert(env):
    await discover(env, make_stage(START))
    rec = await only_row(env)
    env["sched"].jobs.clear()  # simulate restart: in-memory jobs gone
    env["clock"].now = T0 + timedelta(minutes=30)
    stats = await env["svc"].recover()
    assert stats["alerts_rescheduled"] == 1
    assert env["sched"].jobs[f"notify_{rec.id}"]["run_at"] == START - timedelta(minutes=15)


@pytest.mark.asyncio
async def test_rule5_recovery_does_not_send_late_alerts(env):
    await discover(env, make_stage(START))
    env["sched"].jobs.clear()
    env["clock"].now = START - timedelta(minutes=5)
    stats = await env["svc"].recover()
    assert stats["alerts_missed"] == 1 and env["sched"].jobs == {}
    assert env["notifier"].sent == []


@pytest.mark.asyncio
async def test_rule5_recovery_deletes_overdue_and_rearms_future_deletions(env):
    await discover(env, make_stage(START))
    rec = await only_row(env)
    await fire(env, f"notify_{rec.id}")
    # restart before deletion time -> re-armed at start+10
    env["sched"].jobs.clear()
    env["clock"].now = START
    stats = await env["svc"].recover()
    assert stats["deletions_rescheduled"] == 1
    assert env["sched"].jobs[f"delete_{rec.id}"]["run_at"] == START + timedelta(minutes=10)
    # restart after deletion time -> deleted immediately on startup
    env["sched"].jobs.clear()
    env["clock"].now = START + timedelta(hours=3)
    stats = await env["svc"].recover()
    assert stats["deleted_overdue"] == 1
    assert env["notifier"].deleted == [(GROUP, 101)]
    assert (await only_row(env)).status == MintStatus.DELETED.value


@pytest.mark.asyncio
async def test_rule5_recovery_fixes_legacy_deletion_time(env):
    """Rows written by the old code had delete_at = max(start+15, now+15); recovery uses start+10."""
    await discover(env, make_stage(START))
    rec = await only_row(env)
    await fire(env, f"notify_{rec.id}")
    async with env["factory"]() as s:
        r = await s.get(MintOpportunityDB, rec.id)
        r.delete_scheduled_at = START + timedelta(minutes=40)
        await s.commit()
    env["sched"].jobs.clear()
    env["clock"].now = START
    await env["svc"].recover()
    assert env["sched"].jobs[f"delete_{rec.id}"]["run_at"] == START + timedelta(minutes=10)


def test_job_functions_are_module_level_for_persistence():
    """APScheduler's SQLAlchemy store pickles jobs by reference."""
    from app.monitor import processor
    assert processor.run_send_job.__qualname__ == "run_send_job"
    assert processor._run_send_job is processor.run_send_job
    assert processor._run_delete_job is processor.run_delete_job


def test_network_is_blocked_in_tests():
    import socket
    with pytest.raises(RuntimeError):
        socket.create_connection(("api.telegram.org", 443))
