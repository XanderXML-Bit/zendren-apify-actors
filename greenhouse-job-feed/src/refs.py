"""Turn a board token or any Greenhouse board/job URL into a :class:`BoardRef`."""

from __future__ import annotations

import re
from urllib.parse import parse_qs, urlsplit

from .models import BoardRef

_TOKEN_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")
_RESERVED_FIRST_SEGMENT = {"embed", "v1", "jobs", "api", "login", "users", "sign_in"}
_PREFIX_RE = re.compile(r"^\s*(greenhouse|gh)\s*:\s*(?!//)(.+?)\s*$", re.I)


def _clean_token(token: str, original: str) -> str:
    token = (token or "").strip().strip("/")
    if not _TOKEN_RE.match(token):
        what = repr(token) if token != original.strip() else "this"
        raise ValueError(f"{original!r}: {what} is not a valid Greenhouse board token "
                         "(letters, digits, '-' and '_'; or paste the board URL)")
    return token.lower()


def parse_board(entry: str) -> BoardRef:
    """Resolve one ``boards`` input entry. Raises ``ValueError`` with a user-readable reason.

    Accepted: a bare token (``figma``); ``boards.greenhouse.io/<token>`` and
    ``job-boards.greenhouse.io/<token>`` (also with ``/jobs/<id>``); embed links
    (``.../embed/job_board?for=<token>``, ``/embed/job_app?for=<token>&token=<id>``,
    ``/embed/job_board/js?for=<token>``); and the API URL
    ``boards-api.greenhouse.io/v1/boards/<token>/...``.
    """
    original = entry
    entry = (entry or "").strip()
    if not entry:
        raise ValueError("empty board entry")

    m = _PREFIX_RE.match(entry)
    if m:
        entry = m.group(2)

    looks_like_url = "/" in entry or "?" in entry or "." in entry
    if not looks_like_url:
        return BoardRef(_clean_token(entry, original))

    parts = urlsplit(entry if "//" in entry else f"https://{entry}")
    host = (parts.hostname or "").lower()
    segs = [s for s in parts.path.split("/") if s]
    qs = parse_qs(parts.query)

    if host == "greenhouse.io" or host.endswith(".greenhouse.io"):
        if host.endswith(".eu.greenhouse.io") or host == "eu.greenhouse.io":
            raise ValueError(
                f"{original!r}: EU-hosted Greenhouse boards are not supported "
                "(their API host could not be verified)")
        if host == "boards-api.greenhouse.io":
            if len(segs) >= 3 and segs[0] == "v1" and segs[1] == "boards":
                return BoardRef(_clean_token(segs[2], original))
            raise ValueError(f"{original!r}: no board token in this API URL (expected /v1/boards/<token>)")
        if qs.get("for"):
            return BoardRef(_clean_token(qs["for"][0], original))
        if segs and segs[0].lower() not in _RESERVED_FIRST_SEGMENT:
            return BoardRef(_clean_token(segs[0], original))
        raise ValueError(f"{original!r}: no board token found in this Greenhouse URL")

    if qs.get("for") and any(s.startswith("embed") or s.startswith("job_board") for s in segs):
        return BoardRef(_clean_token(qs["for"][0], original))
    if "gh_jid" in qs or "gh_src" in qs:
        raise ValueError(
            f"{original!r}: company-site links with gh_jid do not contain the Greenhouse board token. "
            "Use the token or the company's boards.greenhouse.io / job-boards.greenhouse.io URL")
    raise ValueError(f"{original!r}: not a board token or Greenhouse board URL")
