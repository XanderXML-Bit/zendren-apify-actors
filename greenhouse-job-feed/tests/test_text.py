from src.text import html_to_markdown, html_to_text, unescape_content
from tests.fixtures import ESCAPED


def test_unescape_turns_escaped_markup_into_html():
    assert unescape_content(ESCAPED).startswith("<h2>About</h2><p>Build <strong>great</strong> things &amp; ship.</p>")
    assert unescape_content(None) == ""
    assert unescape_content("<p>already html</p>") == "<p>already html</p>"


def test_text_strips_tags_and_entities():
    text = html_to_text(ESCAPED)
    assert text == "About\n\nBuild great things & ship.\n\n- Python\n- SQL"


def test_markdown_headings_bold_links_lists():
    md = html_to_markdown(
        "<h2>About</h2><p>We are <strong>great </strong>and <em>fast</em>. <a href='https://x.test/a'>Apply</a></p>"
        "<ul><li>One<ul><li>Sub</li></ul></li><li>Two</li></ul><ol><li>a</li><li>b</li></ol>")
    assert md == ("## About\n\nWe are **great** and *fast*. [Apply](https://x.test/a)\n\n"
                  "- One\n  - Sub\n- Two\n\n1. a\n2. b")


def test_markdown_drops_empty_emphasis_and_script():
    md = html_to_markdown("<p>a<strong> </strong>b<script>evil()</script></p><a href='https://x'> </a>")
    assert md == "a b"


def test_markdown_of_escaped_content():
    assert html_to_markdown(ESCAPED) == "## About\n\nBuild **great** things & ship.\n\n- Python\n- SQL"


def test_empty_inputs():
    assert html_to_text(None) == "" and html_to_markdown("") == ""
