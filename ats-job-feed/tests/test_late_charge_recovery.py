"""Offline regression tests for review round 4: R4-H1 (late remote commit across a restart).

A timed-out charge request may still be executing remotely and commit *after* the restarted
process took its charged-count snapshot. Policy: an ambiguous charge is never re-sent, on
restart either (job charges stay <= rows, the start fee stays <= 1).
"""

from decimal import Decimal as D
from types import SimpleNamespace

import pytest

import src.main as main_mod
from src.main import run
from tests.fakes import Platform
from tests.test_charge_failures_and_live_notes import INP, PRICES, SplitActor, SplitChargingManager, fake_source  # noqa: F401


class Deferred:
    """Remote requests that timed out client-side but have not committed yet."""
    def __init__(self, p):
        self.p = p
        self.pending = []

    def commit(self):
        for event, n in self.pending:
            self.p.charged[event] += n
            self.p.charge_calls.append((event, n))
        self.pending.clear()


class LateCommitManager(SplitChargingManager):
    def __init__(self, p, deferred, defer_event):
        super().__init__(p)
        self.deferred = deferred
        self.defer_event = defer_event
        self.armed = True

    async def charge(self, event, *, count=1, idempotency_key=None):
        if event == self.defer_event and self.armed:
            self.armed = False
            self.deferred.pending.append((event, count))
            raise TimeoutError("response lost; request still executing remotely")
        return await super().charge(event, count=count, idempotency_key=idempotency_key)


def late_actor(p, deferred, event=None, inp=None):
    a = SplitActor(p, {**INP, **(inp or {})})
    a.cm = LateCommitManager(p, deferred, event)
    a.cm.armed = event is not None
    return a


async def test_late_commit_of_job_charge_across_restart_never_exceeds_rows():
    p = Platform(prices=dict(PRICES), max_total=D("10"))
    deferred = Deferred(p)
    with pytest.raises(TimeoutError):
        await run(late_actor(p, deferred, "job-scraped"))
    assert len(p.dataset) == 1 and p.charged["job-scraped"] == 0
    restarted = late_actor(p, deferred)  # snapshot taken: remote count is still 0
    assert restarted.cm.get_charged_event_count("job-scraped") == 0
    deferred.commit()  # the original request lands after the snapshot
    await run(restarted)
    assert len(p.dataset) == 10
    assert p.charged["job-scraped"] <= len(p.dataset)
    assert p.charged["job-scraped"] == 10  # 1 late original + 9 new rows; no re-send
    assert p.charged["actor-start"] == 1
    assert list(p.kv["SUMMARY"]["unbilledJobs"]) == ["lever|acme|0"]


async def test_job_charge_that_never_commits_stays_unbilled_but_never_resent():
    p = Platform(prices=dict(PRICES), max_total=D("10"))
    deferred = Deferred(p)
    with pytest.raises(TimeoutError):
        await run(late_actor(p, deferred, "job-scraped"))
    await run(late_actor(p, deferred))  # the pending request is lost for good
    assert len(p.dataset) == 10 and p.charged["job-scraped"] == 9
    assert [c for c in p.charge_calls if c[0] == "job-scraped"] == [("job-scraped", 1)] * 9


async def test_late_commit_of_start_fee_across_restart_charges_it_once():
    p = Platform(prices=dict(PRICES), max_total=D("10"))
    deferred = Deferred(p)
    with pytest.raises(TimeoutError):
        await run(late_actor(p, deferred, "actor-start"))
    # the attempt flag was persisted before the charge was sent
    assert p.kv[main_mod.STATE_KEY]["startChargeAttempted"] is True
    restarted = late_actor(p, deferred)
    assert restarted.cm.get_charged_event_count("actor-start") == 0
    deferred.commit()
    await run(restarted)
    assert len(p.dataset) == 10 and p.charged["job-scraped"] == 10
    assert p.charged["actor-start"] == 1


async def test_start_fee_that_never_commits_is_not_resent_on_restart():
    p = Platform(prices=dict(PRICES), max_total=D("10"))
    deferred = Deferred(p)
    with pytest.raises(TimeoutError):
        await run(late_actor(p, deferred, "actor-start"))
    await run(late_actor(p, deferred))
    assert len(p.dataset) == 10
    assert p.charged["actor-start"] == 0  # under-billed, never double-billed
    await run(late_actor(p, deferred))  # completion-only restart: still no start fee
    assert p.charged["actor-start"] == 0


async def test_refused_start_fee_is_not_marked_attempted():
    p = Platform(prices=dict(PRICES), max_total=D("0.005"))  # cannot afford the start fee
    await run(late_actor(p, Deferred(p)))
    assert p.kv[main_mod.STATE_KEY]["startChargeAttempted"] is False
    assert p.kv["SUMMARY"]["stoppedReason"] == "budget"
    p.max_total = D("10")  # user raises the limit and the run is restarted: now it charges
    await run(late_actor(p, Deferred(p)))
    assert p.charged["actor-start"] == 1 and len(p.dataset) == 10


async def test_start_flag_is_persisted_before_the_charge_is_sent():
    p = Platform(prices=dict(PRICES), max_total=D("10"))
    a = late_actor(p, Deferred(p))
    seen = {}
    real = a.cm.charge

    async def spy(event, *, count=1, idempotency_key=None):
        if event == "actor-start":
            seen["persisted"] = (p.kv.get(main_mod.STATE_KEY) or {}).get("startChargeAttempted")
        return await real(event, count=count, idempotency_key=idempotency_key)

    a.cm.charge = spy
    await run(a)
    assert seen["persisted"] is True


async def test_recovery_note_is_durable_before_pending_marker_is_cleared():
    """R5-M1: interruption right after the marker clear must keep the unbilled warning."""
    p = Platform(prices=dict(PRICES), max_total=D("10"))
    deferred = Deferred(p)
    with pytest.raises(TimeoutError):
        await run(late_actor(p, deferred, "job-scraped"))
    a = late_actor(p, deferred)
    real_set = a.set_value

    async def interrupt_after_clear(key, value, **kwargs):
        await real_set(key, value, **kwargs)
        if key == main_mod.PENDING_KEY and value == {"pending": None}:
            raise TimeoutError("process lost after clearing the recovery marker")

    a.set_value = interrupt_after_clear
    with pytest.raises(TimeoutError):
        await run(a)
    assert p.kv[main_mod.PENDING_KEY] == {"pending": None}
    assert list(p.kv[main_mod.STATE_KEY]["notes"]["unbilledJobs"]) == ["lever|acme|0"]
    await run(late_actor(p, deferred))
    assert list(p.kv["SUMMARY"]["unbilledJobs"]) == ["lever|acme|0"]
