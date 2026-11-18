"""Shared types: errors, the run report and the normalized job."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field


class GreenhouseError(Exception):
    """Per-board failure (logged and reported, never fatal for the whole run)."""


class HttpStatusError(GreenhouseError):
    def __init__(self, status: int, url: str):
        super().__init__(f"HTTP {status} for {url}")
        self.status = status
        self.url = url


@dataclass(frozen=True)
class BoardRef:
    """A Greenhouse job board, identified by its (lower-case) board token."""

    token: str

    @property
    def key(self) -> str:
        return self.token


def job_key(board_token: str, job_id) -> str:
    """Identity of a job across runs and restarts: ``<board token>|<job id>``."""
    return f"{board_token}|{job_id}"


@dataclass
class Job:
    """One delivered row. ``data`` is the dataset item (camelCase, see dataset_schema)."""

    board_token: str
    job_id: int | str
    data: dict

    @property
    def key(self) -> str:
        return job_key(self.board_token, self.job_id)

    def to_dict(self) -> dict:
        return self.data


@dataclass
class RunReport:
    """What the fetch layer saw per board. Plain data; ``on_change`` lets the caller persist it."""

    names: dict[str, str] = field(default_factory=dict)            # token -> board name
    on_board: dict[str, int] = field(default_factory=dict)         # token -> jobs listed by Greenhouse
    matched: dict[str, int] = field(default_factory=dict)          # token -> jobs passing the filters
    already_seen: dict[str, int] = field(default_factory=dict)     # token -> matching jobs skipped (incremental)
    detail_failures: dict[str, list] = field(default_factory=dict)  # token -> job ids whose detail request failed
    unmatched_filters: dict[str, dict] = field(default_factory=dict)  # token -> {"departments": [...], "offices": [...]}
    hierarchy_unavailable: dict[str, str] = field(default_factory=dict)  # token -> why the tree could not be read
    trees: dict[str, dict] = field(default_factory=dict)           # token -> {"departments": [...], "offices": [...]}
    on_change: Callable[[], None] | None = None

    def changed(self) -> None:
        if self.on_change:
            self.on_change()

    def notes(self) -> dict:
        return {
            "names": dict(self.names),
            "onBoard": dict(self.on_board),
            "matched": dict(self.matched),
            "alreadySeen": dict(self.already_seen),
            "detailFailures": {k: list(v) for k, v in self.detail_failures.items()},
            "unmatchedFilters": {k: dict(v) for k, v in self.unmatched_filters.items()},
            "hierarchyUnavailable": dict(self.hierarchy_unavailable),
        }
