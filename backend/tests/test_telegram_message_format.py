from __future__ import annotations

import re
from html.parser import HTMLParser

import pytest

from app.services.telegram_message_format import agent_message_plain_text, render_agent_message_chunks


class _TelegramHtmlReader(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.open_tags: list[str] = []
        self.text: list[str] = []

    def handle_starttag(self, tag: str, _attrs: list[tuple[str, str | None]]) -> None:
        assert tag in {"b", "i", "s", "code", "pre", "a"}
        self.open_tags.append(tag)

    def handle_endtag(self, tag: str) -> None:
        assert self.open_tags
        assert self.open_tags.pop() == tag

    def handle_data(self, data: str) -> None:
        self.text.append(data)


def _utf16_length(value: str) -> int:
    return len(value.encode("utf-16-le")) // 2


def test_plain_and_inline_markdown_render_without_report_markup() -> None:
    assert render_agent_message_chunks("I can **help** with that, if you want.") == [
        "I can <b>help</b> with that, if you want."
    ]
    assert agent_message_plain_text("I can **help** with that, if you want.") == "I can help with that, if you want."

    assert render_agent_message_chunks("*one* ~~two~~") == ["<i>one</i> <s>two</s>"]
    assert agent_message_plain_text("*one* ~~two~~") == "one two"


def test_links_allow_only_safe_schemes_and_escape_attributes() -> None:
    rendered = render_agent_message_chunks(
        "[site](https://example.test/?a=1&b=2) [mail](mailto:person@example.test) "
        "[unsafe](javascript:alert(1))"
    )[0]

    assert '<a href="https://example.test/?a=1&amp;b=2">site</a>' in rendered
    assert '<a href="mailto:person@example.test">mail</a>' in rendered
    assert "href=\"javascript:" not in rendered
    assert "unsafe" in rendered
    assert "[unsafe]" not in rendered
    assert agent_message_plain_text("[unsafe](javascript:alert(1))") == "unsafe"


def test_raw_html_is_escaped_and_images_use_only_alt_text() -> None:
    assert render_agent_message_chunks('<script>alert("x")</script>') == [
        '&lt;script&gt;alert("x")&lt;/script&gt;'
    ]
    assert render_agent_message_chunks("Before ![a picture](https://images.test/x.png) after") == [
        "Before a picture after"
    ]
    assert "<img" not in render_agent_message_chunks("![a picture](https://images.test/x.png)")[0]


def test_inline_and_fenced_code_keep_markdown_characters_literal() -> None:
    assert render_agent_message_chunks("`a < b & *x*`") == ["<code>a &lt; b &amp; *x*</code>"]
    assert render_agent_message_chunks("```python\n<value> & *literal*\n```") == [
        "<pre><code>&lt;value&gt; &amp; *literal*\n</code></pre>"
    ]
    assert agent_message_plain_text("`a < b & *x*`") == "a < b & *x*"


def test_headings_lists_quotes_and_tables_degrade_to_readable_text() -> None:
    rendered = render_agent_message_chunks(
        "# Things\n\n- first\n- **second**\n\n> a quote\n> on two lines\n\n"
        "| Name | Count |\n| --- | --- |\n| apples | 2 |"
    )[0]

    assert "# Things" not in rendered
    assert "Things" in rendered
    assert "- first\n- <b>second</b>" in rendered
    assert "&gt; a quote\n&gt; on two lines" in rendered
    assert "Name: apples, Count: 2" in rendered
    _assert_balanced(rendered)


def test_long_formatted_and_linked_replies_chunk_with_balanced_html_and_utf16_limit() -> None:
    label = "🙂<&" * 35
    source = f"**{label}** and [{label}](https://e.test)"
    chunks = render_agent_message_chunks(source, max_chars=120)

    assert len(chunks) > 2
    assert all(len(chunk) <= 120 for chunk in chunks)
    parsers = [_assert_balanced(chunk) for chunk in chunks]
    assert all(_utf16_length("".join(parser.text)) <= 120 for parser in parsers)
    displayed = "".join("".join(parser.text) for parser in parsers)
    assert displayed == f"{label} and {label}"
    assert all(not re.search(r"&(?!amp;|lt;|gt;|quot;|#\d+;|#x[\da-fA-F]+;)", chunk) for chunk in chunks)


def test_long_code_and_long_text_chunks_preserve_all_characters() -> None:
    code = "<> & *🙂 " * 70
    chunks = render_agent_message_chunks(f"```\n{code}\n```", max_chars=100)

    assert len(chunks) > 1
    parsers = [_assert_balanced(chunk) for chunk in chunks]
    assert all(len(chunk) <= 100 for chunk in chunks)
    assert "".join("".join(parser.text) for parser in parsers) == f"{code}\n"


def test_empty_rendering_stays_safe_for_preview_and_plain_fallback() -> None:
    assert render_agent_message_chunks("---") == ["---"]
    assert agent_message_plain_text("---") == "---"
    assert render_agent_message_chunks("![](https://images.test/empty.png)") == ["![](https://images.test/empty.png)"]
    assert render_agent_message_chunks("") == []
    assert agent_message_plain_text("") == ""


def test_new_formatting_flushes_nearly_full_previous_chunk_before_opening_tag() -> None:
    chunks = render_agent_message_chunks(f"{'x' * 117} **styled**", max_chars=120)

    assert chunks == ["x" * 117 + " ", "<b>styled</b>"]
    for chunk in chunks:
        _assert_balanced(chunk)


def test_inline_code_inside_other_formatting_does_not_nest_telegram_entities() -> None:
    rendered = render_agent_message_chunks("**before `literal * code` after**")[0]

    assert rendered == "<b>before literal * code after</b>"
    assert "<code>" not in rendered
    _assert_balanced(rendered)


@pytest.mark.parametrize("max_chars", [0, -1, True])
def test_chunk_limit_must_be_positive(max_chars: int) -> None:
    with pytest.raises(ValueError):
        render_agent_message_chunks("hello", max_chars=max_chars)


def _assert_balanced(chunk: str) -> _TelegramHtmlReader:
    parser = _TelegramHtmlReader()
    parser.feed(chunk)
    parser.close()
    assert parser.open_tags == []
    return parser
