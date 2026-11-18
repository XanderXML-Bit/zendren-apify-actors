"""Board token / URL parsing: every URL shape we accept, and clear errors for the rest."""

import pytest

from src.refs import parse_board


@pytest.mark.parametrize("entry,token", [
    ("figma", "figma"),
    ("  Figma  ", "figma"),                       # tokens are case-insensitive: stored lower-case
    ("my_board-2", "my_board-2"),
    ("greenhouse:stripe", "stripe"),
    ("gh: stripe", "stripe"),
    ("https://boards.greenhouse.io/figma", "figma"),
    ("https://boards.greenhouse.io/figma/", "figma"),
    ("boards.greenhouse.io/figma", "figma"),      # no scheme
    ("http://boards.greenhouse.io/figma", "figma"),
    ("https://boards.greenhouse.io/figma/jobs/5426468004", "figma"),
    ("https://boards.greenhouse.io/figma/jobs/5426468004?gh_src=abc", "figma"),
    ("https://job-boards.greenhouse.io/figma", "figma"),
    ("https://job-boards.greenhouse.io/figma/jobs/5426468004", "figma"),
    ("https://boards.greenhouse.io/embed/job_board?for=figma", "figma"),
    ("https://job-boards.greenhouse.io/embed/job_board?for=Figma", "figma"),
    ("https://boards.greenhouse.io/embed/job_board/js?for=figma", "figma"),
    ("https://boards.greenhouse.io/embed/job_app?for=figma&token=5426468004", "figma"),
    ("https://boards-api.greenhouse.io/v1/boards/figma/jobs?content=true", "figma"),
    ("https://boards-api.greenhouse.io/v1/boards/figma", "figma"),
    # an embed snippet hosted on the company's own site
    ("https://www.example.com/embed/job_board?for=acme", "acme"),
])
def test_accepted(entry, token):
    assert parse_board(entry).token == token


@pytest.mark.parametrize("entry,fragment", [
    ("", "empty"),
    ("   ", "empty"),
    ("not a token", "valid Greenhouse board token"),
    ("-bad", "valid Greenhouse board token"),
    ("https://boards.greenhouse.io/", "no board token"),
    ("https://boards.greenhouse.io/embed/job_board", "no board token"),
    ("https://boards-api.greenhouse.io/v2/other", "no board token"),
    ("https://www.greenhouse.io/", "no board token"),
    ("https://careers.example.com/jobs/123?gh_jid=123", "gh_jid"),
    ("https://example.com/careers", "not a board token"),
    ("https://job-boards.eu.greenhouse.io/acme", "EU"),
    ("job-boards.eu.greenhouse.io/embed/job_board?for=acme", "EU"),
])
def test_rejected_with_reason(entry, fragment):
    with pytest.raises(ValueError, match=fragment):
        parse_board(entry)
