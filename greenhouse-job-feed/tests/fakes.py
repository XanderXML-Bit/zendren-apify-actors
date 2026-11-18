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
    named_kv: dict = field(default_factory=dict)  # store name -> {key: value}; survives runs
    dataset: list = field(default_factory=list)
    charged: Counter = field(default_factory=Counter)
    charge_calls: list = field(default_factory=list)  # (event, count) actually billed
    crash_on_push: int | None = None  # raise on the Nth push (1-based) of this platform, before the write
    # raise on the Nth push *after* the dataset write, before the charge (the SDK's real gap)
    crash_after_write_on_push: int | None = None
    # ``charge`` calls (1-based, counted per platform) that raise before billing
    fail_charge_calls: set = field(default_factory=set)
    charge_attempts: int = 0
    runs: dict = field(default_factory=dict)  # run id -> platform status ("RUNNING", "SUCCEEDED", ...)
    status_error: Exception | None = None     # raised by the run client's get()

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
    def __init__(self, platform, name):
        self.data = platform.named_kv.setdefault(name, {})

    async def get_value(self, key, default_value=None):
        value = self.data.get(key)
        return copy.deepcopy(value) if value is not None else default_value

    async def set_value(self, key, value):
        self.data[key] = copy.deepcopy(value)


class _Dataset:
    def __init__(self, p):
        self.p = p

    async def get_metadata(self):
        return SimpleNamespace(item_count=len(self.p.dataset))

    async def iterate_items(self, *, fields=None, **_):
        for item in list(self.p.dataset):
            yield {k: item.get(k) for k in fields} if fields else item


class _RunClient:
    def __init__(self, platform, run_id):
        self.p, self.run_id = platform, run_id

    async def get(self):
        if self.p.status_error is not None:
            raise self.p.status_error
        status = self.p.runs.get(self.run_id)
        return None if status is None else {"id": self.run_id, "status": status}


class _ApifyClient:
    def __init__(self, platform):
        self.p = platform

    def run(self, run_id):
        return _RunClient(self.p, run_id)


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
        return _NamedKVS(self.p, name) if name else _KVS(self)

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

    def _charge_default_item(self, n):
        """The platform bills ``apify-default-dataset-item`` itself for every item pushed to the default dataset."""
        if self.p.prices.get(DEFAULT_ITEM) and n:
            self.p.charged[DEFAULT_ITEM] += n
            self.p.charge_calls.append((DEFAULT_ITEM, n))

    async def push_data(self, data, *, charged_event_name=None):
        self.pushes += 1
        if self.p.crash_on_push is not None and self.pushes >= self.p.crash_on_push:
            raise RuntimeError("simulated migration/crash")
        items = data if isinstance(data, list) else [data]
        if charged_event_name is None:
            self.p.dataset.extend(items)
            self._charge_default_item(len(items))
            return SimpleNamespace(event_charge_limit_reached=False, charged_count=0,
                                   chargeable_within_limit={})
        n = self.cm.compute_push_data_limit(len(items), charged_event_name, is_default_dataset=True)
        self.p.dataset.extend(items[:n])
        self._charge_default_item(n)
        if self.p.crash_after_write_on_push is not None and self.pushes >= self.p.crash_after_write_on_push:
            raise RuntimeError("simulated crash after dataset write")
        return await self.cm.charge(charged_event_name, count=n)
