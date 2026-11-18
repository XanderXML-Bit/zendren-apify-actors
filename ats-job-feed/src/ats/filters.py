"""Keyword / location / remote filters (pure functions, no network)."""

from __future__ import annotations

from dataclasses import dataclass

from .models import Job


def split_alternatives(value: str | None) -> list[list[str]]:
    """``"data engineer, ml"`` -> ``[["data", "engineer"], ["ml"]]``.

    Commas (or ``|``) separate alternatives (OR); whitespace-separated words
    inside one alternative must all match (AND). Case-insensitive.
    """
    if not value:
        return []
    out = []
    for alt in value.replace("|", ",").split(","):
        words = alt.lower().split()
        if words:
            out.append(words)
    return out


def _matches(haystack: str, alternatives: list[list[str]]) -> bool:
    h = haystack.lower()
    return any(all(w in h for w in words) for words in alternatives)


@dataclass(frozen=True)
class JobFilter:
    keyword: str | None = None
    location: str | None = None
    remote_only: bool = False

    @property
    def keyword_alternatives(self) -> list[list[str]]:
        return split_alternatives(self.keyword)

    @property
    def location_alternatives(self) -> list[list[str]]:
        return split_alternatives(self.location)

    def match_text(self, title: str, department: str | None) -> bool:
        """Keyword test against title + department only."""
        alts = self.keyword_alternatives
        return not alts or _matches(f"{title} {department or ''}", alts)

    def match_location(self, locations: list[str | None]) -> bool:
        """True if some single location satisfies some complete alternative.

        Each location is tested on its own: the AND words of one alternative must
        all appear in the *same* location, so ``"London Germany"`` does not match a
        job in ``London, UK`` plus ``Berlin, Germany``.
        """
        alts = self.location_alternatives
        if not alts:
            return True
        return any(_matches(loc, alts) for loc in locations if loc)

    def match(
        self, job: Job, *, location_unknown: bool = False, remote_unknown: bool = False
    ) -> bool:
        """Apply all filters. ``*_unknown`` means that fact could not be fetched
        (e.g. a failed Workday detail request); an unknown fact is not a mismatch."""
        if self.remote_only and not job.remote and not remote_unknown:
            return False
        departments = job.departments or ([job.department] if job.department else [])
        if not self.match_text(job.title, " ".join(departments)):
            return False
        if location_unknown:
            return True
        return self.match_location([job.location, *job.locations])
