"""End-to-end through src.main.run with an offline Greenhouse and a fake Actor:
charging gate, budget, restarts, incremental mode, per-board errors, SUMMARY."""

import logging
from datetime import date
from decimal import Decimal as D

import pytest

import src.main as main
from src import incremental
from src.main import run
from tests.fakes import FakeActor, Platform
from tests.fixtures import ClientFactory, FakeClient, board, make_job

PRICES = {"job": D("0.0015")}
STORE = incremental.STORE_NAME
REC = incremental.record_key("default")


@pytest.fixture
def gh(monkeypatch):
    """Offline Greenhouse. ``gh.boards`` is editable between runs; ``gh.client`` records requests."""

    class State:
        boards = {"acme": board(60, name="Acme"), "beta": board(60, name="Beta Corp")}
        client = None

    s = State()

    def factory():
        s.client = FakeClient(s.boards)
        return ClientFactory(s.client)()

    monkeypatch.setattr(main, "RateLimitedClient", factory)
    monkeypatch.delenv("CHARGING_ENABLED", raising=False)
    return s


def ids(p):
    return [(j["boardToken"], j["jobId"]) for j in p.dataset]


async def go(p, inp=None):
    a = FakeActor(p, {"boards": ["acme"], "maxItems": 100, "includeContent": "none", **(inp or {})})
    await run(a)
    return a


def summary(p):
    return p.kv["SUMMARY"]


# ---------------------------------------------------------------- charging gate
@pytest.mark.parametrize("value", [None, "0", "true", ""])
async def test_gate_off_never_charges(monkeypatch, gh, value):
    if value is not None:
        monkeypatch.setenv("CHARGING_ENABLED", value)
    p = Platform(prices=dict(PRICES), max_total=D("1"))
    await go(p, {"maxItems": 7})
    assert len(p.dataset) == 7 and p.charge_calls == [] and not p.charged


async def test_gate_on_charges_every_job_and_nothing_else(monkeypatch, gh):
    monkeypatch.setenv("CHARGING_ENABLED", "1")
    p = Platform(prices=dict(PRICES), max_total=D("10"))
    a = await go(p, {"maxItems": 12})
    assert len(p.dataset) == 12 and dict(p.charged) == {"job": 12}   # no custom start event: the platform bills it
    assert summary(p)["chargingEnabled"] and summary(p)["jobsPushed"] == 12
    assert summary(p)["stoppedReason"] == "maxItems" and not a.failed


async def test_gate_on_non_ppe_run_is_not_stopped(monkeypatch, gh):
    monkeypatch.setenv("CHARGING_ENABLED", "1")
    p = Platform(prices={}, ppe=False)
    await go(p, {"maxItems": 9})
    assert len(p.dataset) == 9 and not p.charged


async def test_gate_on_without_job_price_refuses_to_deliver(monkeypatch, gh):
    monkeypatch.setenv("CHARGING_ENABLED", "1")
    p = Platform(prices={"something-else": D("1")})
    a = await go(p)
    assert p.dataset == [] and "no price" in a.failed and gh.client is None


# ---------------------------------------------------------------- budget
async def test_exhausted_budget_fetches_and_pushes_nothing(monkeypatch, gh):
    monkeypatch.setenv("CHARGING_ENABLED", "1")
    p = Platform(prices=dict(PRICES), max_total=D("0"))
    await go(p)
    assert gh.client is None and p.dataset == [] and summary(p)["stoppedReason"] == "budget"
    assert summary(p)["boards"]["acme"]["reason"] == "budget"


async def test_budget_stops_exactly_at_limit_and_never_pushes_unbillable(monkeypatch, gh):
    monkeypatch.setenv("CHARGING_ENABLED", "1")
    p = Platform(prices=dict(PRICES), max_total=D("0.0045"))   # exactly 3 jobs
    await go(p, {"maxItems": 50})
    assert len(p.dataset) == 3 == p.charged["job"] and p.spent() <= p.max_total
    assert summary(p)["stoppedReason"] == "budget" and summary(p)["boards"]["acme"]["reason"] == "budget"


async def test_paid_charging_with_dataset_item_event_fails_before_fetching(monkeypatch, gh):
    monkeypatch.setenv("CHARGING_ENABLED", "1")
    p = Platform(prices={**PRICES, "apify-default-dataset-item": D("0.0005")}, max_total=D("10"))
    a = await go(p, {"maxItems": 5})
    assert a.failed and "apify-default-dataset-item" in a.failed and "Remove" in a.failed
    assert gh.client is None and p.dataset == [] and not p.charged and p.charge_calls == []


async def test_dataset_item_event_at_zero_price_and_start_event_are_allowed(monkeypatch, gh):
    monkeypatch.setenv("CHARGING_ENABLED", "1")
    p = Platform(prices={**PRICES, "apify-default-dataset-item": D("0"), "apify-actor-start": D("0.00005")},
                 max_total=D("10"))
    a = await go(p, {"maxItems": 3})
    assert not a.failed and len(p.dataset) == 3 and dict(p.charged) == {"job": 3}


async def test_stops_when_push_reports_zero_charged(monkeypatch, gh):
    monkeypatch.setenv("CHARGING_ENABLED", "1")
    p = Platform(prices=dict(PRICES), max_total=D("10"))
    a = FakeActor(p, {"boards": ["acme"], "maxItems": 50, "includeContent": "none", "onlyNewJobs": True})
    real_push = a.push_data

    async def push(data, *, charged_event_name=None):
        if len(p.dataset) == 4:
            p.max_total = p.spent()   # budget vanishes (other charges)
        return await real_push(data, charged_event_name=charged_event_name)

    a.push_data = push
    await run(a)
    assert len(p.dataset) == 4 == p.charged["job"] and summary(p)["stoppedReason"] == "budget"
    # the job that could not be billed was not delivered, so it is not marked seen either
    store = p.named_kv[STORE][REC]["seen"]
    assert sorted(store) == sorted(f"{t}|{i}" for t, i in ids(p))


async def test_warns_when_dataset_item_event_enabled_but_charging_is_off(gh, caplog):
    p = Platform(prices={**PRICES, "apify-default-dataset-item": D("0.0001")})
    with caplog.at_level(logging.WARNING):
        await go(p, {"maxItems": 1})
    assert "apify-default-dataset-item" in caplog.text and "OFF" in caplog.text


async def test_platform_start_event_is_not_a_warning(gh, caplog):
    p = Platform(prices={**PRICES, "apify-actor-start": D("0.00005")})
    with caplog.at_level(logging.WARNING):
        await go(p, {"maxItems": 1})
    assert "look ENABLED" not in caplog.text


# ---------------------------------------------------------------- restarts
async def test_restart_after_crash_is_idempotent(monkeypatch, gh):
    monkeypatch.setenv("CHARGING_ENABLED", "1")
    p = Platform(prices=dict(PRICES), max_total=D("10"))
    p.crash_on_push = 31   # 30 jobs delivered; state last persisted at 20 (PERSIST_EVERY)
    first = FakeActor(p, {"boards": ["acme"], "maxItems": 40, "includeContent": "none"})
    with pytest.raises(RuntimeError):
        await run(first)
    assert len(p.dataset) == 30 and p.charged["job"] == 30
    assert p.kv["GHFEED_STATE"]["pushed"] == 20    # older than the dataset

    p.crash_on_push = None
    await go(p, {"maxItems": 40})                  # restarted run, same platform storage
    got = ids(p)
    assert len(got) == 40 and len(set(got)) == 40  # no duplicates, maxItems honoured across the restart
    assert p.charged["job"] == 40                  # nothing billed twice
    assert summary(p)["resumed"] and summary(p)["jobsPushed"] == 40 and summary(p)["jobsPushedThisRun"] == 10


async def test_crash_between_write_and_charge_never_recharges(monkeypatch, gh):
    monkeypatch.setenv("CHARGING_ENABLED", "1")
    p = Platform(prices=dict(PRICES), max_total=D("10"))
    p.crash_after_write_on_push = 3                # 3rd row written, its charge never sent
    with pytest.raises(RuntimeError):
        await run(FakeActor(p, {"boards": ["acme"], "maxItems": 6, "includeContent": "none"}))
    assert len(p.dataset) == 3 and p.charged["job"] == 2
    p.crash_after_write_on_push = None
    await go(p, {"maxItems": 6})
    assert len(set(ids(p))) == len(p.dataset) == 6   # delivered once each
    assert p.charged["job"] == 5                     # the ambiguous row is not charged again
    assert list(summary(p)["unbilledJobs"]) == [f"acme|{ids(p)[2][1]}"]


async def test_restart_after_completion_does_nothing(monkeypatch, gh):
    monkeypatch.setenv("CHARGING_ENABLED", "1")
    p = Platform(prices=dict(PRICES), max_total=D("10"))
    await go(p, {"maxItems": 5})
    gh.client = None
    before = dict(p.charged)
    await go(p, {"maxItems": 5})
    assert gh.client is None and dict(p.charged) == before and len(p.dataset) == 5
    assert summary(p)["stoppedReason"] == "maxItems"


async def test_restart_respects_budget_left(monkeypatch, gh):
    monkeypatch.setenv("CHARGING_ENABLED", "1")
    p = Platform(prices=dict(PRICES), max_total=D("0.0045"))
    await go(p, {"maxItems": 50})
    await go(p, {"maxItems": 50})
    assert len(p.dataset) == 3 and p.charged["job"] == 3


async def test_fake_matches_sdk_surface():
    """Guard the fakes against drifting from the installed SDK's API."""
    from apify import Actor
    from apify._charging import ChargingManager
    for name in ("get_input", "get_charging_manager", "use_state", "open_key_value_store",
                 "open_dataset", "set_value", "get_value", "set_status_message", "fail", "charge", "push_data"):
        assert hasattr(Actor, name), name
    for name in ("get_pricing_info", "get_charged_event_count", "compute_push_data_limit", "charge"):
        assert hasattr(ChargingManager, name), name


# ---------------------------------------------------------------- incremental mode
async def test_incremental_first_run_all_second_run_none(gh):
    p = Platform()
    await go(p, {"onlyNewJobs": True, "maxItems": 1000})
    assert len(p.dataset) == 60
    inc = summary(p)["incremental"]
    assert inc["firstRun"] and inc["newJobsDelivered"] == 60 and inc["trackedJobs"] == 60
    assert inc["storeName"] == "greenhouse-job-feed-state" and REC in p.named_kv[STORE]

    p.dataset.clear(); p.kv.clear()                 # a brand-new run (own dataset / state) on the same named store
    await go(p, {"onlyNewJobs": True, "maxItems": 1000})
    assert p.dataset == []
    s = summary(p)
    assert s["jobsPushed"] == 0 and not s["incremental"]["firstRun"] and s["incremental"]["alreadySeenSkipped"] == 60
    assert s["boards"]["acme"] == {"name": "Acme", "count": 0, "reason": "no_matches", "jobsOnBoard": 60, "matchedFilters": 60}


async def test_incremental_new_job_only(gh):
    p = Platform()
    await go(p, {"onlyNewJobs": True, "maxItems": 1000})
    gh.boards["acme"]["jobs"].append(make_job(900, title="Brand new"))
    p.dataset.clear(); p.kv.clear()
    await go(p, {"onlyNewJobs": True, "maxItems": 1000})
    assert [j["title"] for j in p.dataset] == ["Brand new"]
    assert summary(p)["incremental"]["alreadySeenSkipped"] == 60 and summary(p)["incremental"]["trackedJobs"] == 61


async def test_incremental_max_items_leftovers_come_next_run(gh):
    p = Platform()
    await go(p, {"onlyNewJobs": True, "maxItems": 25})
    first = set(ids(p))
    assert len(first) == 25 and summary(p)["stoppedReason"] == "maxItems"
    assert len(p.named_kv[STORE][REC]["seen"]) == 25   # only what was delivered is marked
    p.dataset.clear(); p.kv.clear()
    await go(p, {"onlyNewJobs": True, "maxItems": 25})
    second = set(ids(p))
    assert len(second) == 25 and not (first & second)


async def test_incremental_off_does_not_touch_the_store(gh):
    p = Platform()
    await go(p, {"maxItems": 3})
    assert p.named_kv == {} and summary(p)["incremental"] is None


async def test_incremental_state_keys_are_independent(gh):
    p = Platform()
    await go(p, {"onlyNewJobs": True, "stateKey": "daily eu/jobs", "maxItems": 5})
    assert list(p.named_kv[STORE]) == [incremental.record_key("daily eu/jobs")]
    p.dataset.clear(); p.kv.clear()
    await go(p, {"onlyNewJobs": True, "stateKey": "other", "maxItems": 5})
    assert len(p.dataset) == 5 and sorted(p.named_kv[STORE]) == sorted([incremental.record_key("daily eu/jobs"), incremental.record_key("other")])


async def test_incremental_prunes_after_90_days_and_refreshes_listed_jobs(gh, monkeypatch):
    monkeypatch.setattr(incremental, "today_utc", lambda: date(2026, 10, 19))
    p = Platform()
    p.named_kv[STORE] = {REC: {"version": 1, "seen": {
        "acme|1001": "2026-07-22",      # 89 days: kept, and still listed -> refreshed to today
        "acme|1002": "2026-07-20",      # 91 days: pruned on load -> delivered again
        "acme|7777": "2026-07-22",      # 89 days but gone from the board: kept until it ages out
        "acme|1000": "2026-07-01",      # 110 days: pruned -> delivered again
    }}}
    gh.boards["acme"] = board(3)
    await go(p, {"onlyNewJobs": True, "maxItems": 100})
    assert sorted(j["jobId"] for j in p.dataset) == [1000, 1002]
    seen = p.named_kv[STORE][REC]["seen"]
    assert seen["acme|1001"] == "2026-10-19"                 # refreshed because it is still listed
    assert seen["acme|7777"] == "2026-07-22"                 # not listed: date untouched
    assert seen["acme|1000"] == seen["acme|1002"] == "2026-10-19"
    assert summary(p)["incremental"]["pruned"] == 2 and summary(p)["incremental"]["alreadySeenSkipped"] == 1


async def test_incremental_restart_does_not_redeliver_or_forget(monkeypatch, gh):
    monkeypatch.setenv("CHARGING_ENABLED", "1")
    p = Platform(prices=dict(PRICES), max_total=D("10"))
    p.crash_on_push = 31
    with pytest.raises(RuntimeError):
        await run(FakeActor(p, {"boards": ["acme"], "maxItems": 40, "includeContent": "none", "onlyNewJobs": True}))
    assert len(p.dataset) == 30
    p.crash_on_push = None
    await go(p, {"maxItems": 40, "onlyNewJobs": True})
    assert len(set(ids(p))) == len(p.dataset) == 40 and p.charged["job"] == 40
    assert len(p.named_kv[STORE][REC]["seen"]) == 40   # restart-delivered jobs are in the store too


async def test_incremental_with_filters_only_marks_matching_delivered_jobs(gh):
    p = Platform()
    gh.boards["acme"] = {"name": "Acme", "jobs": [make_job(0, title="Keep"), make_job(1, title="Skip")]}
    await go(p, {"onlyNewJobs": True, "keyword": "keep"})
    assert list(p.named_kv[STORE][REC]["seen"]) == ["acme|1000"]
    p.dataset.clear(); p.kv.clear()
    await go(p, {"onlyNewJobs": True})                      # filter removed: the skipped job is new
    assert [j["title"] for j in p.dataset] == ["Skip"]


# ---------------------------------------------------------------- boards, errors, summary
async def test_bad_entries_are_reported_and_other_boards_continue(gh):
    p = Platform()
    await go(p, {"boards": ["acme", "https://example.com/careers", "", "https://job-boards.eu.greenhouse.io/x"],
                 "maxItems": 5})
    s = summary(p)
    assert len(p.dataset) == 5 and [b["input"] for b in s["invalidInputs"]] == [
        "https://example.com/careers", "https://job-boards.eu.greenhouse.io/x"]
    assert "EU" in s["invalidInputs"][1]["error"]


async def test_all_entries_invalid_fails_with_summary(gh):
    p = Platform()
    a = await go(p, {"boards": ["https://example.com", "-"]})
    assert a.failed and "No valid" in a.failed and len(summary(p)["invalidInputs"]) == 2 and gh.client is None


async def test_unknown_board_is_an_error_entry_and_others_continue(gh):
    p = Platform()
    await go(p, {"boards": ["ghost", "acme", "https://boards.greenhouse.io/embed/job_board?for=Acme"], "maxItems": 4})
    s = summary(p)
    assert len(p.dataset) == 4 and s["boards"]["ghost"]["reason"] == "error" and "not found" in s["errors"]["ghost"]
    assert list(s["boards"]) == ["ghost", "acme"]          # embed URL duplicates acme: deduplicated
    assert s["boards"]["acme"]["count"] == 4 and s["boards"]["acme"]["name"] == "Acme"
    assert "ghost" not in {t for t, _ in ids(p)}


async def test_http_failure_of_one_board_keeps_the_rest(gh, monkeypatch):
    from src.models import HttpStatusError
    orig = FakeClient.get_json

    async def get_json(self, url, params=None):
        if "/beta" in url:
            raise HttpStatusError(503, url)
        return await orig(self, url, params)

    monkeypatch.setattr(FakeClient, "get_json", get_json)
    p = Platform()
    await go(p, {"boards": ["beta", "acme"], "maxItems": 10})
    s = summary(p)
    assert len(p.dataset) == 10 and s["boards"]["beta"]["reason"] == "error" and "503" in s["errors"]["beta"]
    assert s["boards"]["acme"]["reason"] == "cap_reached"


async def test_max_items_is_split_fairly_across_boards(gh):
    p = Platform()
    await go(p, {"boards": ["acme", "beta"], "maxItems": 10})
    s = summary(p)
    assert s["boards"]["acme"]["count"] == 5 and s["boards"]["beta"]["count"] == 5
    assert {b["reason"] for b in s["boards"].values()} == {"cap_reached"}
    assert s["boards"]["beta"]["name"] == "Beta Corp"


async def test_short_board_hands_its_share_to_the_other(gh):
    gh.boards["acme"] = board(3)
    p = Platform()
    await go(p, {"boards": ["acme", "beta"], "maxItems": 20})
    s = summary(p)
    assert s["boards"]["acme"] == {"name": "Acme", "count": 3, "reason": "completed", "jobsOnBoard": 3, "matchedFilters": 3}
    assert s["boards"]["beta"]["count"] == 17


async def test_no_matches_reason_and_filters_in_input(gh):
    p = Platform()
    await go(p, {"keyword": "zzz-nothing"})
    assert summary(p)["boards"]["acme"]["reason"] == "no_matches" and summary(p)["stoppedReason"] == "completed"


async def test_filters_and_options_reach_the_fetch_layer(gh):
    gh.boards["acme"] = {"name": "Acme", "jobs": [
        make_job(0, depts=(2,), offices=(12,), updated="2026-10-18T10:00:00-04:00"),
        make_job(1, depts=(4,), offices=(12,), updated="2026-10-18T10:00:00-04:00"),
        make_job(2, depts=(2,), offices=(21,), updated="2026-10-18T10:00:00-04:00"),
        make_job(3, depts=(2,), offices=(12,), updated="2025-01-01T10:00:00-04:00"),
    ]}
    p = Platform()
    await go(p, {"departments": ["Engineering"], "offices": ["North America"], "updatedSince": "2026-01-01",
                 "includePayRanges": True, "includeQuestions": True, "includeContent": "markdown"})
    assert [j["jobId"] for j in p.dataset] == [1000]
    item = p.dataset[0]
    assert item["payRanges"][0]["min"] == 150000 and item["questions"][0]["label"] == "First Name"
    assert item["content"].startswith("## About")


async def test_invalid_updated_since_fails_clearly(gh):
    p = Platform()
    a = await go(p, {"updatedSince": "whenever"})
    assert a.failed and "updatedSince" in a.failed and p.dataset == []
    assert summary(p)["jobsPushed"] == 0 and "updatedSince" in summary(p)["error"]


async def test_pricing_refusal_still_writes_summary(monkeypatch, gh):
    monkeypatch.setenv("CHARGING_ENABLED", "1")
    p = Platform(prices={"something-else": D("1")})
    a = await go(p)
    assert a.failed and summary(p) == {"jobsPushed": 0, "error": a.failed, "boards": {}}


async def test_departments_offices_record_and_name_warnings(gh, caplog):
    p = Platform()
    with caplog.at_level(logging.WARNING):
        await go(p, {"boards": ["acme", "beta"], "maxItems": 4, "departments": ["Engineering", "Nope"]})
    trees = p.kv["DEPARTMENTS_OFFICES"]
    assert set(trees) == {"acme", "beta"}
    assert trees["acme"]["departments"][0]["name"] == "Engineering"
    assert summary(p)["unmatchedFilters"]["acme"]["departments"] == ["Nope"]
    assert "no department/office named 'Nope'" in caplog.text


async def test_summary_survives_when_pay_detail_fails(gh, monkeypatch):
    from src.models import GreenhouseError
    orig = FakeClient.get_json

    async def get_json(self, url, params=None):
        if url.endswith("/jobs/1000"):
            raise GreenhouseError("boom")
        return await orig(self, url, params)

    monkeypatch.setattr(FakeClient, "get_json", get_json)
    p = Platform()
    gh.boards["acme"] = board(2)
    await go(p, {"includePayRanges": True})
    assert summary(p)["detailFailures"] == {"acme": [1000]}
    assert next(j for j in p.dataset if j["jobId"] == 1000)["payRanges"] is None


async def test_incremental_unexpected_failure_still_saves_delivered_jobs(monkeypatch, gh):
    p = Platform()
    p.crash_on_push = 8
    with pytest.raises(RuntimeError):
        await run(FakeActor(p, {"boards": ["acme"], "maxItems": 40, "includeContent": "none", "onlyNewJobs": True}))
    assert len(p.dataset) == 7
    assert len(p.named_kv[STORE][REC]["seen"]) == 7   # not just the last periodic checkpoint


# ---------------------------------------------------------------- unpaid recovered rows vs incremental history
async def test_unconfirmed_recovered_row_never_enters_the_incremental_history(monkeypatch, gh):
    monkeypatch.setenv("CHARGING_ENABLED", "1")
    p = Platform(prices=dict(PRICES), max_total=D("10"))
    inp = {"boards": ["acme"], "maxItems": 1, "includeContent": "none", "onlyNewJobs": True}
    p.crash_after_write_on_push = 1                      # row written, its charge never sent
    with pytest.raises(RuntimeError):
        await run(FakeActor(p, inp))
    assert len(p.dataset) == 1 and p.charged["job"] == 0
    unpaid = f"acme|{p.dataset[0]['jobId']}"
    assert unpaid not in p.named_kv[STORE].get(REC, {}).get("seen", {})   # the crashed run did not mark it either

    p.crash_after_write_on_push = None
    await go(p, {"maxItems": 1, "onlyNewJobs": True})       # restart: counted as delivered, charge unconfirmed
    s = summary(p)
    assert s["jobsPushed"] == 1 and list(s["unbilledJobs"]) == [unpaid] and p.charged["job"] == 0
    assert unpaid not in p.named_kv[STORE].get(REC, {}).get("seen", {})

    await go(p, {"maxItems": 1, "onlyNewJobs": True})       # a second restart after the pending marker was cleared
    assert unpaid not in p.named_kv[STORE].get(REC, {}).get("seen", {})
    assert list(summary(p)["unbilledJobs"]) == [unpaid]

    p.dataset.clear(); p.kv.clear()                         # next scheduled run: the job is new again and gets billed
    await go(p, {"maxItems": 1000, "onlyNewJobs": True})
    assert unpaid in {f"{t}|{i}" for t, i in ids(p)} and p.charged["job"] == len(p.dataset)


async def test_confirmed_rows_before_the_unpaid_one_are_still_recorded(monkeypatch, gh):
    monkeypatch.setenv("CHARGING_ENABLED", "1")
    p = Platform(prices=dict(PRICES), max_total=D("10"))
    p.crash_after_write_on_push = 3
    with pytest.raises(RuntimeError):
        await run(FakeActor(p, {"boards": ["acme"], "maxItems": 3, "includeContent": "none", "onlyNewJobs": True}))
    p.crash_after_write_on_push = None
    await go(p, {"maxItems": 3, "onlyNewJobs": True})
    recorded = set(p.named_kv[STORE][REC]["seen"])
    delivered = {f"{t}|{i}" for t, i in ids(p)}
    assert len(delivered) == 3 and recorded == delivered - set(summary(p)["unbilledJobs"]) and len(recorded) == 2


# ---------------------------------------------------------------- state cap / lock through run()
async def test_summary_reports_evicted_state_entries(monkeypatch, gh):
    monkeypatch.setattr(incremental, "MAX_ENTRIES", 10)
    p = Platform()
    await go(p, {"onlyNewJobs": True, "maxItems": 25})
    inc = summary(p)["incremental"]
    assert inc["stateEvicted"] == 15 and inc["trackedJobs"] == 10 and inc["maxTrackedJobs"] == 10
    assert len(p.named_kv[STORE][REC]["seen"]) == 10


LOCK = "LOCK-" + incremental.record_key("default")


async def test_second_run_with_the_same_state_key_stops_while_the_first_is_live(monkeypatch, gh):
    p = Platform(runs={"run-1": "RUNNING"})
    monkeypatch.setenv("ACTOR_RUN_ID", "run-1")
    await go(p, {"onlyNewJobs": True, "maxItems": 3})
    assert p.named_kv[STORE].get(LOCK) is None               # released when the first run finished

    p.named_kv[STORE][LOCK] = {"runId": "run-1", "at": "2026-10-19T10:00:00+00:00"}   # first run is still going
    monkeypatch.setenv("ACTOR_RUN_ID", "run-2")
    gh.client = None
    before = len(p.dataset)
    a = await go(p, {"onlyNewJobs": True, "maxItems": 3})
    assert "another run is using this stateKey" in a.failed and gh.client is None and len(p.dataset) == before
    assert p.named_kv[STORE][LOCK]["runId"] == "run-1"       # the live run keeps its lock
    assert "another run" in summary(p)["error"]


async def test_stale_lock_of_a_finished_run_is_taken_over_and_released(monkeypatch, gh):
    p = Platform(runs={"run-1": "FAILED"})
    p.named_kv[STORE] = {LOCK: {"runId": "run-1", "at": "2026-10-18T10:00:00+00:00"}}
    monkeypatch.setenv("ACTOR_RUN_ID", "run-2")
    a = await go(p, {"onlyNewJobs": True, "maxItems": 3})
    assert not a.failed and len(p.dataset) == 3 and p.named_kv[STORE].get(LOCK) is None


async def test_lock_is_released_when_the_run_crashes(monkeypatch, gh):
    p = Platform()
    p.crash_on_push = 2
    monkeypatch.setenv("ACTOR_RUN_ID", "run-1")
    with pytest.raises(RuntimeError):
        await run(FakeActor(p, {"boards": ["acme"], "maxItems": 5, "includeContent": "none", "onlyNewJobs": True}))
    assert p.named_kv[STORE].get(LOCK) is None


async def test_runs_without_incremental_mode_ignore_the_lock(monkeypatch, gh):
    p = Platform(runs={"run-1": "RUNNING"})
    p.named_kv[STORE] = {LOCK: {"runId": "run-1", "at": "x"}}
    monkeypatch.setenv("ACTOR_RUN_ID", "run-2")
    a = await go(p, {"maxItems": 3})
    assert not a.failed and len(p.dataset) == 3 and p.named_kv[STORE][LOCK]["runId"] == "run-1"


async def test_local_run_without_run_id_skips_the_lock(monkeypatch, gh):
    monkeypatch.delenv("ACTOR_RUN_ID", raising=False)
    p = Platform(runs={"run-1": "RUNNING"})
    a = await go(p, {"onlyNewJobs": True, "maxItems": 3})
    assert not a.failed and not any(k.startswith("LOCK-") for k in p.named_kv[STORE])
