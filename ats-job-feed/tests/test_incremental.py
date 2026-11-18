"""Incremental mode (``onlyNewJobs``): offline tests with fake sources and a fake named store."""

import copy
from decimal import Decimal as D

import pytest

import src.incremental as inc
import src.pipeline as pl
from apify import Event
from src.ats import Job
from src.incremental import STORE_NAME, SeenState, default_state_key, record_key
from src.main import run
from src.ats import parse_company
from tests.fakes import FakeActor, Platform

NEW = {"apify-actor-start": D("0.00005"), "job-scraped": D("0.0015")}
OLD = {"actor-start": D("0.01"), "job-scraped": D("0.003")}
INP = {"companies": ["lever:acme"], "maxItems": 100, "includeDescription": False, "onlyNewJobs": True}
BOARDS: dict[str, list[tuple[str, str]]] = {}  # slug -> [(jobId, title)]
DAY = {"now": 20000}


@pytest.fixture(autouse=True)
def world(monkeypatch):
    BOARDS.clear()
    BOARDS["acme"] = [(str(i), f"Engineer {i}") for i in range(10)]
    DAY["now"] = 20000
    monkeypatch.setattr(inc, "today", lambda: DAY["now"])
    monkeypatch.delenv("CHARGING_ENABLED", raising=False)

    async def fake(client, ref, flt, include_description=True, report=None):
        for job_id, title in list(BOARDS[ref.slug]):
            job = Job(title=title, company=ref.slug, location="X", remote=False, department=None,
                      postedDate=None, applyUrl=None, atsSource=ref.ats, descriptionText=None,
                      jobId=job_id, companySlug=ref.slug)
            if flt.match(job):
                yield job

    monkeypatch.setattr(pl, "fetch_company", fake)


def next_run(prev: Platform, **kw) -> Platform:
    """A new run: fresh default storage and billing, same named stores (they persist)."""
    return Platform(prices=kw.pop("prices", dict(prev.prices)), max_total=kw.pop("max_total", prev.max_total),
                    named_kv=prev.named_kv, **kw)


async def go(p, inp=None):
    a = FakeActor(p, {**INP, **(inp or {})})
    await run(a)
    return a


def ids(p):
    return [j["jobId"] for j in p.dataset]


def state_of(p, key=None):
    store = p.named_kv[STORE_NAME]
    return store[key or next(iter(store))]


async def test_first_run_emits_everything_and_creates_state():
    p = Platform()
    await go(p)
    assert ids(p) == [str(i) for i in range(10)]
    st = state_of(p)
    assert st["v"] == 1 and len(st["jobs"]) == 10
    assert set(st["jobs"].values()) == {20000}
    assert "lever|acme|3" in st["jobs"]
    s = p.kv["SUMMARY"]
    assert s["onlyNewJobs"] and s["newJobs"] == 10 and s["skippedSeenJobs"] == 0
    assert s["stateStore"] == STORE_NAME and s["stateKey"].startswith("companies-")
    assert STORE_NAME not in p.kv and "JOBFEED_STATE" in p.kv  # run-scoped state stays in the default store


async def test_second_run_without_changes_pushes_and_charges_nothing(monkeypatch):
    monkeypatch.setenv("CHARGING_ENABLED", "1")
    p1 = Platform(prices=dict(NEW), max_total=D("10"))
    await go(p1)
    assert p1.charged["job-scraped"] == 10
    p2 = next_run(p1)
    a = await go(p2)
    assert not a.failed and p2.dataset == [] and p2.charge_calls == []
    s = p2.kv["SUMMARY"]
    assert s["newJobs"] == 0 and s["skippedSeenJobs"] == 10 and s["stoppedReason"] == "completed"
    assert s["perCompany"]["lever:acme"] == {"count": 0, "reason": "no_new_jobs"}


async def test_new_job_appears_only_it_is_pushed():
    p1 = Platform()
    await go(p1)
    BOARDS["acme"].append(("99", "Brand new role"))
    p2 = next_run(p1)
    await go(p2)
    assert ids(p2) == ["99"]
    s = p2.kv["SUMMARY"]
    assert s["newJobs"] == 1 and s["skippedSeenJobs"] == 10
    assert len(state_of(p2)["jobs"]) == 11


async def test_seen_jobs_get_last_seen_refreshed_and_removed_jobs_keep_theirs():
    p1 = Platform()
    await go(p1)
    BOARDS["acme"] = [j for j in BOARDS["acme"] if j[0] != "0"]  # job 0 was taken down
    DAY["now"] = 20010
    p2 = next_run(p1)
    await go(p2)
    jobs = state_of(p2)["jobs"]
    assert jobs["lever|acme|1"] == 20010 and jobs["lever|acme|0"] == 20000


async def test_max_items_caps_new_jobs_and_leftovers_come_next_run():
    p1 = Platform()
    await go(p1, {"maxItems": 4})
    assert ids(p1) == ["0", "1", "2", "3"]
    assert p1.kv["SUMMARY"]["stoppedReason"] == "maxItems"
    assert len(state_of(p1)["jobs"]) == 4  # undelivered jobs are NOT remembered
    p2 = next_run(p1)
    await go(p2, {"maxItems": 4})
    assert ids(p2) == ["4", "5", "6", "7"]  # known jobs do not use up the cap
    p3 = next_run(p2)
    await go(p3, {"maxItems": 4})
    assert ids(p3) == ["8", "9"] and p3.kv["SUMMARY"]["stoppedReason"] == "completed"
    p4 = next_run(p3)
    await go(p4)
    assert p4.dataset == []


async def test_known_jobs_do_not_consume_fair_share():
    BOARDS["beta"] = [(f"b{i}", f"Role {i}") for i in range(10)]
    p1 = Platform()
    await go(p1, {"companies": ["lever:acme"]})  # acme remembered under its own default key
    key = p1.kv["SUMMARY"]["stateKey"]
    p2 = next_run(p1)
    await go(p2, {"companies": ["lever:acme", "lever:beta"], "stateKey": key, "maxItems": 6})
    assert sorted(ids(p2)) == sorted(f"b{i}" for i in range(6))  # acme has nothing new: beta gets all 6


async def test_budget_stop_leaves_undelivered_jobs_unmarked(monkeypatch):
    monkeypatch.setenv("CHARGING_ENABLED", "1")
    p1 = Platform(prices=dict(NEW), max_total=D("0.00005") + D("0.0015") * 3)
    p1.charged["apify-actor-start"] = 1
    await go(p1)
    assert len(p1.dataset) == 3 == p1.charged["job-scraped"]
    assert p1.kv["SUMMARY"]["stoppedReason"] == "budget"
    assert len(state_of(p1)["jobs"]) == 3
    p2 = next_run(p1, max_total=D("10"))
    p2.charged["apify-actor-start"] = 1
    await go(p2)
    assert ids(p2) == [str(i) for i in range(3, 10)]
    assert p2.charged["job-scraped"] == 7


async def test_budget_exhausted_mid_push_does_not_mark_the_unpushed_job(monkeypatch):
    monkeypatch.setenv("CHARGING_ENABLED", "1")
    p = Platform(prices=dict(NEW), max_total=D("10"))
    p.charged["apify-actor-start"] = 1
    a = FakeActor(p, dict(INP))
    real_push = a.push_data

    async def push(data, *, charged_event_name=None):
        if len(p.dataset) == 4:
            p.max_total = p.spent()  # budget gone: the next push writes and charges nothing
        return await real_push(data, charged_event_name=charged_event_name)

    a.push_data = push
    await run(a)
    assert len(p.dataset) == 4 and len(state_of(p)["jobs"]) == 4


async def test_old_pricing_works_in_incremental_mode(monkeypatch):
    monkeypatch.setenv("CHARGING_ENABLED", "1")
    p1 = Platform(prices=dict(OLD), max_total=D("10"))
    await go(p1, {"maxItems": 5})
    assert p1.charged["actor-start"] == 1 and p1.charged["job-scraped"] == 5
    p2 = next_run(p1)
    await go(p2)
    assert p2.charged["actor-start"] == 1 and p2.charged["job-scraped"] == 5  # a new run pays its own start


async def test_pruning_drops_entries_not_seen_for_retention_days():
    p1 = Platform()
    await go(p1)
    BOARDS["acme"] = [("0", "Engineer 0")]  # only job 0 stays listed; 1-9 vanish
    DAY["now"] = 20000 + 60
    p2 = next_run(p1)
    await go(p2)
    assert len(state_of(p2)["jobs"]) == 10  # nothing is 90 days old yet
    DAY["now"] = 20000 + 60 + 40  # 100 days after the vanished jobs were last seen, 40 after job 0
    p3 = next_run(p2)
    await go(p3)
    assert list(state_of(p3)["jobs"]) == ["lever|acme|0"]
    assert p3.dataset == []  # job 0 was refreshed, so it is still known
    # a vanished job that comes back after expiry is new again
    BOARDS["acme"].append(("5", "Engineer 5"))
    p4 = next_run(p3)
    await go(p4)
    assert ids(p4) == ["5"]


async def test_still_listed_jobs_never_expire():
    p = Platform()
    await go(p)
    for step in range(1, 6):  # 150 days in 30-day steps
        DAY["now"] = 20000 + 30 * step
        p = next_run(p)
        await go(p)
        assert p.dataset == []
    assert len(state_of(p)["jobs"]) == 10


async def test_seen_state_prunes_on_load_and_save_and_rejects_garbage():
    p = Platform()
    p.named_kv[STORE_NAME] = {"STATE-x": {"v": 1, "jobs": {"a|b|1": 100, "a|b|2": 19950, "a|b|3": 19909,
                                                           "a|b|4": "bad", 5: 20000}}}
    kvs = await FakeActor(p, {}).open_key_value_store(name=STORE_NAME)
    st = SeenState(kvs, "STATE-x", day=20000)
    await st.load()
    assert set(st.jobs) == {"a|b|2"} and st.pruned == 4 and st.loaded
    st.mark("a|b|9")
    st.day = 20041  # job 2 is now 91 days old
    await st.save()
    assert set(p.named_kv[STORE_NAME]["STATE-x"]["jobs"]) == {"a|b|9"}
    p.named_kv[STORE_NAME]["STATE-y"] = {"jobs": "not a dict"}
    st2 = SeenState(kvs, "STATE-y", day=20000)
    await st2.load()
    assert st2.jobs == {} and not st2.loaded


async def test_restart_mid_run_neither_redelivers_nor_loses_jobs():
    BOARDS["acme"] = [(str(i), f"Engineer {i}") for i in range(60)]
    p = Platform()
    p.crash_on_push = 31  # 30 delivered; run state last persisted at 25
    with pytest.raises(RuntimeError):
        await run(FakeActor(p, dict(INP)))
    assert len(p.dataset) == 30
    # a hard kill: the final save did not happen; the named store only has what the last periodic save wrote
    key = next(iter(p.named_kv[STORE_NAME]))
    p.named_kv[STORE_NAME][key]["jobs"] = {k: v for k, v in p.named_kv[STORE_NAME][key]["jobs"].items()
                                           if int(k.rsplit("|", 1)[1]) < 25}
    p.crash_on_push = None
    await go(p)  # the restarted run, same storages
    assert len(ids(p)) == 60 == len(set(ids(p)))
    assert p.kv["SUMMARY"]["newJobs"] == 60 and p.kv["SUMMARY"]["resumed"]
    assert len(state_of(p)["jobs"]) == 60
    p2 = next_run(p)
    await go(p2)
    assert p2.dataset == []


async def test_restart_with_lost_run_state_still_remembers_pushed_jobs():
    """The named store has every pushed job (saved on failure); a rerun on fresh storage re-delivers none."""
    BOARDS["acme"] = [(str(i), f"Engineer {i}") for i in range(40)]
    p = Platform()
    p.crash_on_push = 11
    with pytest.raises(RuntimeError):
        await run(FakeActor(p, dict(INP)))
    p2 = next_run(p)
    await go(p2)
    assert sorted(ids(p2), key=int) == [str(i) for i in range(10, 40)]


async def test_restart_with_charging_does_not_double_bill(monkeypatch):
    monkeypatch.setenv("CHARGING_ENABLED", "1")
    BOARDS["acme"] = [(str(i), f"Engineer {i}") for i in range(60)]
    p = Platform(prices=dict(NEW), max_total=D("10"))
    p.charged["apify-actor-start"] = 1
    p.crash_on_push = 31
    with pytest.raises(RuntimeError):
        await run(FakeActor(p, dict(INP)))
    p.crash_on_push = None
    await go(p)
    assert len(set(ids(p))) == 60 == len(ids(p)) and p.charged["job-scraped"] == 60


async def test_persist_state_event_saves_and_listener_is_removed():
    BOARDS["acme"] = [(str(i), f"Engineer {i}") for i in range(5)]
    p = Platform()
    a = FakeActor(p, dict(INP))
    real_push = a.push_data
    snapshots = []

    async def push(data, **kw):
        if len(p.dataset) == 2:
            await a.emit(Event.PERSIST_STATE)  # e.g. a migration notice mid-run
            snapshots.append(copy.deepcopy(state_of(p)["jobs"]))
        return await real_push(data, **kw)

    a.push_data = push
    await run(a)
    assert len(snapshots[0]) == 2  # exactly the two pushed jobs were saved
    assert a.listeners[Event.PERSIST_STATE] == []


async def test_state_key_override_isolates_and_shares_state():
    p1 = Platform()
    await go(p1, {"stateKey": "daily-eng"})
    assert p1.kv["SUMMARY"]["stateKey"] == "daily-eng"
    assert list(p1.named_kv[STORE_NAME]) == [record_key("daily-eng")]
    p2 = next_run(p1)
    await go(p2, {"stateKey": "daily-eng"})
    assert p2.dataset == []  # same key: remembered
    p3 = next_run(p2)
    await go(p3, {"stateKey": "daily-eng-v2"})  # the documented way to reset
    assert len(p3.dataset) == 10
    p4 = next_run(p3)
    await go(p4)  # default key differs from both explicit ones
    assert len(p4.dataset) == 10
    assert len(p4.named_kv[STORE_NAME]) == 3


async def test_default_state_key_follows_the_company_list_not_its_order():
    BOARDS["beta"] = [("b1", "Role")]
    p1 = Platform()
    await go(p1, {"companies": ["lever:acme", "lever:beta"]})
    p2 = next_run(p1)
    await go(p2, {"companies": ["https://jobs.lever.co/beta", "lever:acme", "lever:acme"]})
    assert p2.dataset == []  # same set of companies, different spelling and order
    p3 = next_run(p2)
    await go(p3, {"companies": ["lever:acme"]})
    assert len(p3.dataset) == 10  # a different company list is a different feed


def test_default_state_key_is_stable():
    refs = [parse_company("lever:b"), parse_company("greenhouse:a")]
    assert default_state_key(refs) == default_state_key(list(reversed(refs)))
    assert default_state_key(refs).startswith("companies-") and len(default_state_key(refs)) == 26


async def test_filter_change_does_not_corrupt_state():
    BOARDS["acme"] = [("1", "Data Engineer"), ("2", "Designer"), ("3", "Data Analyst"), ("4", "Cook")]
    p1 = Platform()
    await go(p1, {"keyword": "data"})
    assert sorted(ids(p1)) == ["1", "3"]
    p2 = next_run(p1)
    await go(p2, {"keyword": "designer, cook"})  # different filter: only unseen matches come out
    assert sorted(ids(p2)) == ["2", "4"]
    p3 = next_run(p2)
    await go(p3, {})  # no filter: everything is already remembered
    assert p3.dataset == []
    assert len(state_of(p3)["jobs"]) == 4
    BOARDS["acme"].append(("5", "Data Scientist"))
    p4 = next_run(p3)
    await go(p4, {"keyword": "data"})
    assert ids(p4) == ["5"]


async def test_filtered_out_jobs_are_not_remembered():
    BOARDS["acme"] = [("1", "Data Engineer"), ("2", "Designer")]
    p1 = Platform()
    await go(p1, {"keyword": "data"})
    assert list(state_of(p1)["jobs"]) == ["lever|acme|1"]


async def test_non_incremental_run_does_not_touch_named_store():
    p = Platform()
    await go(p, {"onlyNewJobs": False})
    assert p.named_kv == {} and len(p.dataset) == 10
    s = p.kv["SUMMARY"]
    assert s["onlyNewJobs"] is False and s["newJobs"] is None and s["stateStore"] is None
    p2 = next_run(p)
    await go(p2, {"onlyNewJobs": False})
    assert len(p2.dataset) == 10  # no memory without the flag


async def test_failing_company_keeps_its_old_entries_and_run_continues():
    BOARDS["beta"] = [("b1", "Role")]
    p1 = Platform()
    await go(p1, {"companies": ["lever:acme", "lever:beta"]})
    del BOARDS["acme"]  # fetching acme now raises KeyError
    p2 = next_run(p1)
    await go(p2, {"companies": ["lever:acme", "lever:beta"]})
    assert p2.dataset == [] and "lever:acme" in p2.kv["SUMMARY"]["failedCompanies"]
    assert len(state_of(p2)["jobs"]) == 11


def test_record_key_follows_apify_rules():
    assert record_key("daily-eng").startswith("STATE-daily-eng-")  # readable prefix + hash of the original
    k = record_key("my feed/2026: eng?")
    assert all(c.isalnum() or c in "!-_.'()" for c in k) and k.startswith("STATE-my_feed_2026__eng_-")
    assert record_key("a b") != record_key("a_b")  # sanitizing never merges two different keys
    long = record_key("x" * 1000)
    assert len(long) == 256 and long == record_key("x" * 1000) and long != record_key("x" * 999 + "y")
    assert len(record_key("é" * 300)) <= 256
