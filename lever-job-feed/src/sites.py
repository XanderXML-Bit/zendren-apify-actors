"""Site input parsing: Lever site names and every jobs.lever.co / jobs.eu.lever.co URL shape."""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import unquote, urlsplit

GLOBAL = "global"
EU = "eu"
REGIONS = ("auto", GLOBAL, EU)

HOSTS = {GLOBAL: "https://api.lever.co", EU: "https://api.eu.lever.co"}
# page host -> region; the API hosts are accepted too (people paste them from docs)
_HOST_REGION = {
    "jobs.lever.co": GLOBAL, "api.lever.co": GLOBAL,
    "jobs.eu.lever.co": EU, "api.eu.lever.co": EU,
}
_SITE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.\-]{0,99}$")


@dataclass(frozen=True)
class SiteRef:
    """A Lever site. ``region`` is the host the input pointed at (None = unknown, try both)."""

    site: str
    region: str | None = None


def parse_site(entry: str) -> SiteRef:
    """``spotify``, ``jobs.lever.co/spotify``, ``https://jobs.eu.lever.co/x/<id>/apply?lever-source=y``,
    ``api.lever.co/v0/postings/spotify?mode=json`` -> SiteRef. Raises ValueError with a clear message."""
    raw = (entry or "").strip()
    if not raw:
        raise ValueError("empty entry")
    first = raw.split("/")[0].lower().removeprefix("www.")
    looks_like_url = "/" in raw or "?" in raw or first in _HOST_REGION
    if not looks_like_url:
        if not _SITE_RE.match(raw):
            raise ValueError("not a valid Lever site name (letters, digits, '-', '_' and '.' only)")
        return SiteRef(raw)
    parts = urlsplit(raw if "://" in raw else "//" + raw)
    host = (parts.hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    if host not in _HOST_REGION:
        raise ValueError(f"unsupported host {host or raw!r}: expected jobs.lever.co or jobs.eu.lever.co")
    segments = [unquote(s) for s in parts.path.split("/") if s]
    if host.startswith("api."):  # /v0/postings/<site>[/<id>]
        if len(segments) >= 3 and segments[0] == "v0" and segments[1] == "postings":
            segments = segments[2:]
        else:
            segments = []
    if not segments:
        raise ValueError("URL has no site name (expected jobs.lever.co/<site>)")
    site = segments[0]
    if not _SITE_RE.match(site):
        raise ValueError(f"{site!r} is not a valid Lever site name")
    return SiteRef(site, _HOST_REGION[host])


def split_entries(raw) -> list[str]:
    """Accept strings, comma/newline separated strings, or {'url': ...} objects."""
    out: list[str] = []
    for item in raw or []:
        if isinstance(item, dict):
            item = item.get("url") or item.get("site") or ""
        if isinstance(item, str):
            out += [p.strip() for p in re.split(r"[,\n]", item) if p.strip()]
    return out
