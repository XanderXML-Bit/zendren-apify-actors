"""In-memory stand-ins for the Apify ``Actor`` (no network, no SDK storage).

``Platform`` is what survives a migration/restart: the key-value store records,
the dataset and the platform's charged-event counters. A fresh ``FakeActor`` on the
same ``Platform`` simulates the restarted run. Charging semantics mirror the SDK's
``ChargingManagerImplementation`` (``push_data`` with ``charged_event_name`` pushes
at most what the budget allows, then charges; ``charge`` returns ``charged_count``).
"""

from __future__ import annotations

import copy
import logging
import math
import zlib
from collections import Counter
from dataclasses import dataclass, field
from decimal import Decimal
from types import SimpleNamespace

DEFAULT_ITEM = "apify-default-dataset-item"



@dataclass
class Platform:
    prices: dict[str, Decimal] = field(default_factory=dict)
    max_total: Decimal = Decimal("inf")
    ppe: bool = True
    kv: dict = field(default_factory=dict)
    named: dict = field(default_factory=dict)  # named key-value stores: name -> {key: value}
    dataset: list = field(default_factory=list)
    charged: Counter = field(default_factory=Counter)
    charge_calls: list = field(default_factory=list)  # (event, count) actually billed
    crash_on_push: int | None = None  # raise on the Nth push (1-based) of this platform, before the write
    # raise on the Nth push *after* the dataset write, before the charge (the SDK's real gap)
    crash_after_write_on_push: int | None = None
    # ``charge`` calls (1-based, counted per platform) that raise before billing
    fail_charge_calls: set = field(default_factory=set)
    charge_attempts: int = 0
    runs: dict = field(default_factory=dict)  # run id -> status, what ``apify_client.run(id).get()`` reports
    run_lookup_error: bool = False

    def spent(self) -> Decimal:
        return sum((self.prices.get(e, Decimal(0)) * n for e, n in self.charged.items()), Decimal(0))


class FakeChargingManager:
    def __init__(self, p: Platform) -> None:
        self.p = p

    def get_pricing_info(self):
        return SimpleNamespace(is_pay_per_event=self.p.ppe, per_event_prices=dict(self.p.prices),
                               max_total_charge_usd=self.p.max_total, pricing_model="PAY_PER_EVENT")

    def get_charged_event_count(self, event):
        return self.p.charged[event]

    def _price(self, event):
        return self.p.prices.get(event, Decimal(0))

    def calculate_max_event_charge_count_within_limit(self, event):
        price = self._price(event)
        if not price:
            return None
        r = (self.p.max_total - self.p.spent()) / price
        return max(0, math.floor(r)) if r.is_finite() else None

    def is_event_charge_limit_reached(self, event):
        m = self.calculate_max_event_charge_count_within_limit(event)
        return m is not None and m <= 0

    def compute_chargeable(self):
        return {e: self.calculate_max_event_charge_count_within_limit(e) for e in self.p.prices}

    def compute_push_data_limit(self, items_count, event, *, is_default_dataset):
        combined = self._price(event) + (self._price(DEFAULT_ITEM) if is_default_dataset else 0)
        if not combined:
            return items_count
        r = (self.p.max_total - self.p.spent()) / combined
        return min(items_count, max(0, math.floor(r)) if r.is_finite() else items_count)

    async def charge(self, event, *, count=1, idempotency_key=None):
        self.p.charge_attempts += 1
        if self.p.charge_attempts in self.p.fail_charge_calls:
            raise RuntimeError("simulated charge failure")
        if not self.p.ppe:
            return SimpleNamespace(event_charge_limit_reached=False, charged_count=0,
                                   chargeable_within_limit={})
        m = self.calculate_max_event_charge_count_within_limit(event)
        n = min(count, m if m is not None else count)
        if n == 0:
            return SimpleNamespace(event_charge_limit_reached=True, charged_count=0,
                                   chargeable_within_limit=self.compute_chargeable())
        self.p.charged[event] += n
        self.p.charge_calls.append((event, n))
        return SimpleNamespace(event_charge_limit_reached=self.is_event_charge_limit_reached(event),
                               charged_count=n, chargeable_within_limit=self.compute_chargeable())


class _KVS:
    def __init__(self, actor):
        self.actor = actor

    async def persist_autosaved_values(self):
        for key, value in self.actor.states.items():
            self.actor.p.kv[key] = copy.deepcopy(value)


class _NamedKVS:
    def __init__(self, data: dict):
        self.data = data

    async def get_value(self, key, default_value=None):
        return copy.deepcopy(self.data.get(key, default_value))

    async def set_value(self, key, value):
        self.data[key] = copy.deepcopy(value)

    async def delete_value(self, key):
        self.data.pop(key, None)


class _RunClient:
    def __init__(self, p, run_id):
        self.p, self.run_id = p, run_id

    async def get(self):
        if self.p.run_lookup_error:
            raise RuntimeError("simulated API failure")
        status = self.p.runs.get(self.run_id)
        return {"id": self.run_id, "status": status} if status else None


class _ApifyClient:
    def __init__(self, p):
        self.p = p

    def run(self, run_id):
        return _RunClient(self.p, run_id)


class _Dataset:
    def __init__(self, p):
        self.p = p

    async def get_metadata(self):
        return SimpleNamespace(item_count=len(self.p.dataset))

    async def iterate_items(self, *, fields=None, **_):
        for item in list(self.p.dataset):
            yield {k: item.get(k) for k in fields} if fields else item


class FakeActor:
    def __init__(self, platform: Platform, inp: dict) -> None:
        self.p = platform
        self.inp = inp
        self.cm = FakeChargingManager(platform)
        self.states: dict = {}
        self.log = logging.getLogger("fake-actor")
        self.status: list[str] = []
        self.failed: str | None = None
        self.pushes = 0
        self.apify_client = _ApifyClient(platform)

    # --- SDK surface used by src.main.run -------------------------------------
    async def get_input(self):
        return self.inp

    def get_charging_manager(self):
        return self.cm

    async def use_state(self, default_value=None, *, key=None, **_):
        persisted = self.p.kv.get(key)  # what survived the (simulated) restart
        self.states[key] = copy.deepcopy(persisted) if persisted is not None else default_value
        return self.states[key]

    async def open_key_value_store(self, *, name=None, **_):
        if name:
            return _NamedKVS(self.p.named.setdefault(name, {}))
        return _KVS(self)

    async def open_dataset(self, **_):
        return _Dataset(self.p)

    async def get_value(self, key, default_value=None, **_):
        value = self.p.kv.get(key)
        return copy.deepcopy(value) if value is not None else default_value

    async def set_value(self, key, value, **_):
        self.p.kv[key] = copy.deepcopy(value)

    async def set_status_message(self, msg, **_):
        self.status.append(msg)

    async def fail(self, *, status_message=None, **_):
        self.failed = status_message

    async def charge(self, event, count=1, **kw):
        return await self.cm.charge(event, count=count, **kw)

    async def push_data(self, data, *, charged_event_name=None):
        self.pushes += 1
        if self.p.crash_on_push is not None and self.pushes >= self.p.crash_on_push:
            raise RuntimeError("simulated migration/crash")
        items = data if isinstance(data, list) else [data]
        if charged_event_name is None:
            self.p.dataset.extend(items)  # (synthetic dataset-item charging is not modelled)
            return SimpleNamespace(event_charge_limit_reached=False, charged_count=0,
                                   chargeable_within_limit={})
        n = self.cm.compute_push_data_limit(len(items), charged_event_name, is_default_dataset=True)
        self.p.dataset.extend(items[:n])
        if self.p.crash_after_write_on_push is not None and self.pushes >= self.p.crash_after_write_on_push:
            raise RuntimeError("simulated crash after dataset write")
        return await self.cm.charge(charged_event_name, count=n)


# ---------------------------------------------------------------------------------
# Fake Lever Postings API (httpx.MockTransport) with the server-side semantics verified live
# ---------------------------------------------------------------------------------
import httpx  # noqa: E402

from src.http import RateLimitedClient  # noqa: E402

BASE_MS = 1_790_000_000_000  # 2026-09


def posting(pid, title="Engineer", *, team="Platform", department="Engineering", location="London",
            all_locations=None, commitment="Full-time", level=None, workplace="hybrid", country="GB",
            created=None, salary=None, salary_desc=None, lists=None, opening="", description="<div>About <b>us</b></div>",
            additional="<div>EEO</div>", **extra):
    cats = {"team": team, "department": department, "location": location,
            "allLocations": all_locations if all_locations is not None else [location]}
    if commitment is not None:
        cats["commitment"] = commitment
    if level:
        cats["level"] = level
    if team is None:
        del cats["team"]
    p = {
        "id": str(pid), "text": title, "categories": cats, "country": country, "workplaceType": workplace,
        "opening": opening, "openingPlain": opening,
        "description": description, "descriptionPlain": "About us",
        "descriptionBody": description, "descriptionBodyPlain": "About us",
        "lists": lists if lists is not None else [
            {"text": "What you'll do", "content": "<li>Build things</li><li>Ship &amp; learn</li>"}],
        "additional": additional, "additionalPlain": "EEO",
        "hostedUrl": f"https://jobs.lever.co/acme/{pid}", "applyUrl": f"https://jobs.lever.co/acme/{pid}/apply",
        "createdAt": created if created is not None else BASE_MS + (zlib.crc32(str(pid).encode()) % 10000) * 1000,
    }
    if salary:
        p["salaryRange"] = salary
    if salary_desc:
        p["salaryDescription"] = f"<div>{salary_desc}</div>"
        p["salaryDescriptionPlain"] = salary_desc
    p.update(extra)
    return p


def _ci(values, wanted):
    return values is not None and str(values).lower() in {w.lower() for w in wanted}


class FakeLever:
    """``boards[(region, site)] = [postings]``; unknown boards answer 404 like Lever does."""

    def __init__(self, boards: dict, fail=None):
        self.boards = boards
        self.requests: list[httpx.Request] = []
        self.fail = fail or {}  # (region, site) -> status code

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        region = "eu" if request.url.host == "api.eu.lever.co" else "global"
        parts = request.url.path.strip("/").split("/")
        site = parts[2]
        if (region, site) in self.fail:
            return httpx.Response(self.fail[(region, site)], json={"ok": False})
        if (region, site) not in self.boards:
            return httpx.Response(404, json={"ok": False, "error": "Document not found"})
        q = request.url.params
        out = []
        for p in self.boards[(region, site)]:
            c = p["categories"]
            ok = True
            for api, field_ in (("team", "team"), ("department", "department"),
                                ("commitment", "commitment"), ("level", "level")):
                want = q.get_list(api)
                if want and not _ci(c.get(field_), want):
                    ok = False
            want = q.get_list("location")
            if want and not set(want) & set(c.get("allLocations") or []):  # case-sensitive
                ok = False
            if ok:
                out.append(p)
        skip = int(q.get("skip", 0))
        limit = int(q.get("limit", len(out) or 1))
        return httpx.Response(200, json=out[skip:skip + limit])

    def client(self) -> RateLimitedClient:
        return RateLimitedClient(transport=httpx.MockTransport(self.handler), delay=0, backoff_base=0,
                                 max_retries=1)

    def params_of(self, i=-1) -> dict:
        return {k: self.requests[i].url.params.get_list(k) for k in dict.fromkeys(self.requests[i].url.params.keys())}
