"""Shared data types: company references, normalized jobs, errors."""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable
from dataclasses import asdict, dataclass, field


class AtsError(Exception):
    """Base class for per-company failures (logged, never fatal for the run)."""


class HttpStatusError(AtsError):
    def __init__(self, status: int, url: str):
        super().__init__(f"HTTP {status} for {url}")
        self.status = status
        self.url = url


@dataclass(frozen=True)
class CompanyRef:
    """A resolved career-site reference.

    ``slug`` is the board slug (Greenhouse/Lever/Ashby) or the tenant (Workday).
    Workday additionally needs ``wd`` (e.g. ``wd5``) and ``site``.
    ``region`` is ``"eu"`` for EU-hosted Lever boards.
    """

    ats: str
    slug: str
    wd: str | None = None
    site: str | None = None
    region: str | None = None

    @property
    def key(self) -> str:
        if self.ats == "workday":
            return f"workday:{self.slug}.{self.wd}/{self.site}"
        return f"{self.ats}:{self.slug}"


@dataclass
class Job:
    title: str
    company: str
    location: str | None
    remote: bool
    department: str | None
    postedDate: str | None
    applyUrl: str | None
    atsSource: str
    descriptionText: str | None
    jobId: str
    salary: dict | None = None
    # extras
    jobUrl: str | None = None
    employmentType: str | None = None
    locations: list[str] = field(default_factory=list)
    companySlug: str | None = None
    # every department the posting belongs to (``department`` is the first one)
    departments: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not self.departments and self.department:
            self.departments = [self.department]

    def to_dict(self) -> dict:
        return asdict(self)


class _Watched:
    """Mixin for the report containers: calls ``owner._changed()`` after every write."""

    _owner = None

    def _notify(self) -> None:
        if self._owner is not None:
            self._owner._changed()

    def __setitem__(self, key, value):
        super().__setitem__(key, value)
        self._notify()

    def update(self, *args, **kwargs):
        super().update(*args, **kwargs)
        self._notify()

    def setdefault(self, key, default=None):
        had = key in self
        out = super().setdefault(key, default)
        if not had:
            self._notify()
        return out


class _WatchedDict(_Watched, dict):
    pass


class _WatchedCounter(_Watched, Counter):
    pass


@dataclass
class RunReport:
    """Non-fatal data-quality notes collected by the adapters (shown in SUMMARY)."""

    # company key -> why the crawl stopped before all postings were visited
    truncated: dict[str, str] = field(default_factory=dict)
    # company key -> number of Workday detail requests that failed
    detail_failures: Counter = field(default_factory=Counter)
    # company key -> jobs kept although a filter fact could not be verified
    unverified: Counter = field(default_factory=Counter)
    # called after every change, so the run can keep its autosaved state current
    on_change: Callable[[], None] | None = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        self.truncated = _WatchedDict(self.truncated)
        self.detail_failures = _WatchedCounter(self.detail_failures)
        self.unverified = _WatchedCounter(self.unverified)
        for container in (self.truncated, self.detail_failures, self.unverified):
            container._owner = self

    def _changed(self) -> None:
        if self.on_change is not None:
            self.on_change()

    def to_dict(self) -> dict:
        return {
            "truncatedCompanies": dict(self.truncated),
            "detailFailures": dict(self.detail_failures),
            "unverifiedJobsIncluded": dict(self.unverified),
        }
