"""src.incremental: record keys, the entry cap and the run lock (no Apify, no network)."""

import re
from datetime import date

import pytest

from src import incremental as inc
from src.incremental import IncrementalState, RunLock, record_key
from tests.fakes import Platform, _NamedKVS

TODAY = date(2026, 10, 19)


def store():
    return _NamedKVS(Platform(), "s")


# ---------------------------------------------------------------- record keys
def test_record_key_shape_and_allowed_characters():
    key = record_key("daily-eu")
    assert re.fullmatch(r"STATE-daily-eu-[0-9a-f]{12}", key)
    assert re.fullmatch(r"[A-Za-z0-9!\-_.'()]+", record_key("weird name/ünï? ✓"))


@pytest.mark.parametrize("keys", [
    ["daily/a", "daily?a", "daily_a", "daily a", "daily:a"],           # punctuation sanitizes to the same prefix
    ["Daily", "daily", "DAILY"],                                       # case
    ["ünï", "uni", "üni", "ñ", "é", "日本語", "中文"],                   # unicode (unicode ones all sanitize to ___)
    ["x" * 300 + "a", "x" * 300 + "b", "x" * 250, "x" * 251],          # same long prefix
    ["daily_a", "STATE-daily_a", "daily_a-0123456789ab"],               # sanitized-versus-original, digest-looking keys
])
def test_distinct_state_keys_never_share_a_record(keys):
    records = [record_key(k) for k in keys]
    assert len(set(records)) == len(keys)
    assert all(len(r) <= 200 for r in records)


def test_long_prefix_reserves_room_for_the_digest():
    key = record_key("y" * 5000)
    assert len(key) == 200 and re.search(r"-[0-9a-f]{12}$", key)


def test_default_and_whitespace_policy():
    assert record_key(None) == record_key("") == record_key("   ") == record_key("default")
    assert record_key("  daily  ") == record_key("daily")      # surrounding whitespace is not part of the key
    assert record_key("a b") != record_key("a  b")              # inner whitespace is
    assert IncrementalState(store(), "  ").state_key == "default"


async def test_two_colliding_looking_keys_keep_separate_history():
    st = store()
    a, b = IncrementalState(st, "daily/a", TODAY), IncrementalState(st, "daily?a", TODAY)
    a.mark(["acme|1"]); b.mark(["acme|2"])
    await a.save(); await b.save()
    a2, b2 = IncrementalState(st, "daily/a", TODAY), IncrementalState(st, "daily?a", TODAY)
    await a2.load(); await b2.load()
    assert a2.keys() == {"acme|1"} and b2.keys() == {"acme|2"}


# ---------------------------------------------------------------- cap
async def test_oversized_state_is_trimmed_on_load_after_age_pruning():
    st = store()
    seen = {f"a|{i}": "2026-10-01" for i in range(5)}
    seen.update({f"b|{i}": "2026-10-10" for i in range(3)})
    seen["old|1"] = "2026-01-01"                                  # aged out, not "evicted"
    await st.set_value(record_key("k"), {"version": 1, "seen": seen})
    s = IncrementalState(st, "k", TODAY, max_entries=6)
    await s.load()
    assert s.pruned == 1 and s.evicted == 2 and s.loaded == 6
    assert s.keys() == {"a|2", "a|3", "a|4", "b|0", "b|1", "b|2"}   # oldest date first, then smaller key first


async def test_additions_beyond_the_cap_evict_least_recently_seen_on_save():
    st = store()
    s = IncrementalState(st, "k", date(2026, 10, 1), max_entries=4)
    s.mark(["x|1", "x|2", "x|3", "x|4"])
    await s.save()
    assert s.evicted == 0
    s2 = IncrementalState(st, "k", TODAY, max_entries=4)
    await s2.load()
    s2.touch("x|4")                                                # refreshed: survives
    s2.mark(["n|1", "n|2"])
    await s2.save()
    stored = (await st.get_value(record_key("k")))["seen"]
    assert s2.evicted == 2 and set(stored) == {"x|4", "n|1", "n|2", "x|3"}   # x|1, x|2 went (tie -> smaller key)
    assert len(stored) == 4
    await s2.save()                                                # nothing more to evict, count is stable
    assert s2.evicted == 2


async def test_default_cap_is_200k():
    assert inc.MAX_ENTRIES == 200_000
    s = IncrementalState(store(), "k", TODAY)
    s.mark(f"j|{i}" for i in range(200_001))
    await s.save()
    assert len(s.seen) == 200_000 and s.evicted == 1


# ---------------------------------------------------------------- run lock
def status_of(statuses):
    async def get(run_id):
        v = statuses.get(run_id)
        if isinstance(v, Exception):
            raise v
        return v
    return get


async def test_lock_is_taken_and_released():
    st = store()
    lock = RunLock(st, "k", "run-1", status_of({}))
    assert await lock.acquire() is None
    held = await st.get_value("LOCK-" + record_key("k"))
    assert held["runId"] == "run-1" and held["at"]
    await lock.release()
    assert await st.get_value("LOCK-" + record_key("k")) is None


@pytest.mark.parametrize("status", ["RUNNING", "READY", "TIMING-OUT", "ABORTING"])
async def test_live_run_blocks_a_second_run(status):
    st = store()
    assert await RunLock(st, "k", "run-1", status_of({})).acquire() is None
    second = RunLock(st, "k", "run-2", status_of({"run-1": status}))
    msg = await second.acquire()
    assert "another run is using this stateKey" in msg and "run-1" in msg
    await second.release()                                            # not held: must not remove run-1's lock
    assert (await st.get_value("LOCK-" + record_key("k")))["runId"] == "run-1"


@pytest.mark.parametrize("status", ["SUCCEEDED", "FAILED", "ABORTED", "TIMED-OUT", None])
async def test_finished_or_unknown_run_lock_is_taken_over(status):
    st = store()
    await RunLock(st, "k", "run-1", status_of({})).acquire()
    second = RunLock(st, "k", "run-2", status_of({"run-1": status}))
    assert await second.acquire() is None
    assert (await st.get_value("LOCK-" + record_key("k")))["runId"] == "run-2"


async def test_same_run_restart_keeps_going():
    st = store()
    await RunLock(st, "k", "run-1", status_of({"run-1": "RUNNING"})).acquire()
    assert await RunLock(st, "k", "run-1", status_of({"run-1": "RUNNING"})).acquire() is None


async def test_status_lookup_error_does_not_block():
    st = store()
    await RunLock(st, "k", "run-1", status_of({})).acquire()
    assert await RunLock(st, "k", "run-2", status_of({"run-1": RuntimeError("api down")})).acquire() is None


async def test_locks_of_different_state_keys_are_independent():
    st = store()
    await RunLock(st, "a", "run-1", status_of({})).acquire()
    assert await RunLock(st, "b", "run-2", status_of({"run-1": "RUNNING"})).acquire() is None


async def test_local_run_without_run_id_or_client_skips_the_lock():
    st = store()
    lock = RunLock(st, "k", None, None)
    assert await lock.acquire() is None and st.data == {}
    await lock.release()
    assert await RunLock(st, "k", "run-1", None).acquire() is None      # no client: lock stored, nothing checked
    assert await RunLock(st, "k", "run-2", None).acquire() is None
