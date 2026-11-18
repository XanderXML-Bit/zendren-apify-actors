"""Parse career URLs / ``ats:slug`` strings into :class:`CompanyRef`."""

from __future__ import annotations

import re
from urllib.parse import parse_qs, urlsplit

from .models import CompanyRef

ATS_ALIASES = {
    "greenhouse": "greenhouse", "gh": "greenhouse",
    "lever": "lever",
    "ashby": "ashby", "ashbyhq": "ashby",
    "workday": "workday", "wd": "workday",
}
_PREFIX_RE = re.compile(r"^\s*([A-Za-z]+)\s*:\s*(?!//)(.+?)\s*$")
_WORKDAY_RE = re.compile(
    r"^([A-Za-z0-9_-]+)\.(wd\d+)(?:\.myworkdayjobs\.com)?/(?:[a-z]{2}(?:-[A-Za-z]{2})?/)?([^/\s?#]+)$"
)
_LOCALE_RE = re.compile(r"^[a-z]{2}([-_][A-Za-z]{2})?$")
_SLUG_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


def _clean_slug(slug: str, original: str) -> str:
    slug = slug.strip().strip("/")
    if not _SLUG_RE.match(slug):
        raise ValueError(f"invalid company slug in {original!r}")
    return slug


def _parse_prefixed(ats: str, rest: str, original: str) -> CompanyRef:
    if ats == "workday":
        m = _WORKDAY_RE.match(rest.strip())
        if not m:
            raise ValueError(
                f"{original!r}: Workday refs look like 'workday:tenant.wd5/SiteName'"
            )
        return CompanyRef("workday", m.group(1), wd=m.group(2), site=m.group(3))
    region = None
    if ats == "lever" and rest.lower().startswith("eu/"):
        region, rest = "eu", rest[3:]
    return CompanyRef(ats, _clean_slug(rest, original), region=region)


def parse_company(entry: str) -> CompanyRef:
    """Resolve one ``companies`` input entry. Raises ``ValueError`` if unusable."""
    original = entry
    entry = (entry or "").strip()
    if not entry:
        raise ValueError("empty company entry")

    m = _PREFIX_RE.match(entry)
    if m and m.group(1).lower() in ATS_ALIASES:
        return _parse_prefixed(ATS_ALIASES[m.group(1).lower()], m.group(2), original)

    url = entry if "//" in entry else f"https://{entry}"
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    segs = [s for s in parts.path.split("/") if s]

    if host.endswith("greenhouse.io"):
        # boards.greenhouse.io/{slug}, job-boards.greenhouse.io/{slug}, EU variant,
        # and the embed form boards.greenhouse.io/embed/job_board?for={slug}
        qs = parse_qs(parts.query)
        if "for" in qs:
            return CompanyRef("greenhouse", _clean_slug(qs["for"][0], original))
        if segs and segs[0] not in {"embed"}:
            return CompanyRef("greenhouse", _clean_slug(segs[0], original))
        raise ValueError(f"no Greenhouse board slug in {original!r}")

    if host.endswith("lever.co") and host.startswith(("jobs.", "jobs.eu.")):
        if not segs:
            raise ValueError(f"no Lever slug in {original!r}")
        region = "eu" if host.startswith("jobs.eu.") else None
        return CompanyRef("lever", _clean_slug(segs[0], original), region=region)

    if host == "jobs.ashbyhq.com":
        if not segs:
            raise ValueError(f"no Ashby slug in {original!r}")
        return CompanyRef("ashby", _clean_slug(segs[0], original))

    if host.endswith(".myworkdayjobs.com"):
        labels = host.split(".")
        if len(labels) < 4 or not re.fullmatch(r"wd\d+", labels[1]):
            raise ValueError(f"cannot read Workday tenant/pod from {original!r}")
        site = next((s for s in segs if not _LOCALE_RE.match(s) and s != "job"), None)
        if not site:
            raise ValueError(f"no Workday site name in {original!r}")
        return CompanyRef("workday", labels[0], wd=labels[1], site=site)

    raise ValueError(
        f"unrecognised company {original!r}: use a Greenhouse/Lever/Ashby/Workday "
        "career URL or 'ats:slug'"
    )
