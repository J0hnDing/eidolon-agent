"""Render agent Markdown as safe Telegram HTML or plain text."""

from __future__ import annotations

import html
from dataclasses import dataclass, field
from typing import TypeAlias
from urllib.parse import urlsplit

from markdown_it import MarkdownIt
from markdown_it.token import Token


class _AgentMarkdownParser(MarkdownIt):
    def validateLink(self, url: str) -> bool:  # noqa: N802 - override markdown-it-py's API
        # The renderer applies its own narrow scheme policy and drops unsafe
        # links while retaining their labels. Parsing them here avoids exposing
        # Markdown syntax for rejected destinations as literal reply text.
        return True


_PARSER = _AgentMarkdownParser("default", {"html": False, "linkify": False, "typographer": False})
_MAX_URL_LENGTH = 2048


@dataclass
class _Node:
    token: Token | None = None
    children: list[_Node] = field(default_factory=list)


@dataclass
class _Element:
    tag: str
    children: list[_Rendered] = field(default_factory=list)
    href: str | None = None


_Rendered: TypeAlias = str | _Element


def _parse_markdown(text: str) -> _Node:
    if not isinstance(text, str):
        raise TypeError("Agent message must be text")

    root = _Node()
    parents = [root]
    for token in _PARSER.parse(text):
        if token.nesting == 1:
            child = _Node(token)
            parents[-1].children.append(child)
            parents.append(child)
        elif token.nesting == -1:
            if len(parents) > 1:
                parents.pop()
        else:
            parents[-1].children.append(_Node(token))
    return root


def _attrs(token: Token) -> dict[str, str]:
    return dict(token.attrs or {})


def _safe_href(value: str) -> str | None:
    if not value or len(value) > _MAX_URL_LENGTH or any(ord(char) < 32 or char.isspace() for char in value):
        return None
    try:
        parsed = urlsplit(value)
    except ValueError:
        return None
    scheme = parsed.scheme.casefold()
    if scheme in {"http", "https"}:
        if not parsed.hostname or parsed.username is not None or parsed.password is not None:
            return None
        return value
    if scheme == "mailto" and parsed.path and not parsed.netloc:
        return value
    return None


def _render_inline(tokens: list[Token] | None, *, max_chars: int) -> list[_Rendered]:
    root: list[_Rendered] = []
    containers: list[list[_Rendered]] = [root]
    opened: list[_Element | None] = []

    def add(value: _Rendered) -> None:
        if value:
            containers[-1].append(value)

    for token in tokens or []:
        if token.type in {"strong_open", "em_open", "s_open", "link_open"}:
            element: _Element | None
            if token.type == "link_open":
                href = _safe_href(_attrs(token).get("href", ""))
                # Keep the visible label for long destinations instead of making
                # the URL attribute alone exceed a small caller-supplied limit.
                if href is None or len(f'<a href="{html.escape(href, quote=True)}"></a>') + 4 > max_chars:
                    element = None
                else:
                    element = _Element("a", href=href)
            else:
                tag_name = {"strong_open": "b", "em_open": "i", "s_open": "s"}[token.type]
                element = _Element(tag_name)
            if element is not None:
                containers[-1].append(element)
                containers.append(element.children)
            opened.append(element)
            continue

        if token.type in {"strong_close", "em_close", "s_close", "link_close"}:
            if opened:
                element = opened.pop()
                if element is not None and len(containers) > 1:
                    containers.pop()
            continue

        if token.type == "code_inline":
            # Telegram does not allow code/pre entities to contain other
            # formatting entities. Preserve literal code text if Markdown nests
            # a code span inside bold, italic, strike, or a link.
            if any(element is not None and element.tag in {"b", "i", "s", "a"} for element in opened):
                add(token.content)
            elif len("<code></code>") + len(html.escape(token.content[:1], quote=False)) > max_chars:
                add(token.content)
            else:
                add(_Element("code", [token.content]))
        elif token.type == "image":
            # Agent replies may mention an image, but Telegram delivery does not
            # fetch or embed it. The source alt text remains useful to the user.
            add(token.content)
        elif token.type in {"softbreak", "hardbreak"}:
            add("\n")
        elif token.type not in {"html_inline", "html_block"}:
            add(token.content)
        else:
            # Raw HTML is deliberately treated as visible text. The serializer
            # escapes it just like every other source character.
            add(token.content)
    return root


def _inline_node(node: _Node, *, max_chars: int) -> list[_Rendered]:
    if node.token is None:
        return []
    return _render_inline(node.token.children, max_chars=max_chars)


def _joined(parts: list[list[_Rendered]], separator: str) -> list[_Rendered]:
    result: list[_Rendered] = []
    for part in parts:
        if not part:
            continue
        if result:
            result.append(separator)
        result.extend(part)
    return result


def _list_items(node: _Node) -> list[_Node]:
    return [child for child in node.children if child.token and child.token.type == "list_item_open"]


def _render_list(node: _Node, *, max_chars: int, depth: int = 0) -> list[_Rendered]:
    token = node.token
    ordered = bool(token and token.type == "ordered_list_open")
    try:
        first_number = int(_attrs(token).get("start", "1")) if token else 1
    except ValueError:
        first_number = 1

    lines: list[list[_Rendered]] = []
    for index, item in enumerate(_list_items(node)):
        marker = f"{first_number + index}. " if ordered else "- "
        prefix = "  " * depth + marker
        item_parts: list[list[_Rendered]] = []
        for child in item.children:
            if not child.token:
                continue
            child_type = child.token.type
            if child_type == "paragraph_open":
                inline = next((entry for entry in child.children if entry.token and entry.token.type == "inline"), None)
                if inline is not None:
                    item_parts.append(_inline_node(inline, max_chars=max_chars))
            elif child_type in {"bullet_list_open", "ordered_list_open"}:
                item_parts.append(_render_list(child, max_chars=max_chars, depth=depth + 1))
            elif child_type not in {"paragraph_close", "list_item_close"}:
                item_parts.append(_render_block(child, max_chars=max_chars))

        content = _joined(item_parts, "\n  " * (depth + 1))
        lines.append([prefix, *content])
    return _joined(lines, "\n")


def _inline_text(node: _Node, *, include_link: bool = True) -> str:
    if node.token is None:
        return ""
    return _tokens_text(node.token.children, include_link=include_link)


def _tokens_text(tokens: list[Token] | None, *, include_link: bool = True) -> str:
    result: list[str] = []
    links: list[str | None] = []
    for token in tokens or []:
        if token.type == "link_open":
            links.append(_safe_href(_attrs(token).get("href", "")))
        elif token.type == "link_close":
            href = links.pop() if links else None
            if href and include_link:
                label = "".join(result)
                if label and href not in label:
                    result.append(f" ({href})")
        elif token.type == "image":
            result.append(token.content)
        elif token.type in {"softbreak", "hardbreak"}:
            result.append("\n")
        elif token.type not in {"strong_open", "strong_close", "em_open", "em_close", "s_open", "s_close", "html_inline", "html_block"}:
            result.append(token.content)
        elif token.type in {"html_inline", "html_block"}:
            result.append(token.content)
    return "".join(result)


def _table_text(node: _Node) -> str:
    header_row: list[str] = []
    data_rows: list[list[str]] = []
    for section in node.children:
        if not section.token or section.token.type not in {"thead_open", "tbody_open"}:
            continue
        for row in section.children:
            if not row.token or row.token.type != "tr_open":
                continue
            values = []
            for cell in row.children:
                if cell.token and cell.token.type in {"th_open", "td_open"}:
                    inline = next((part for part in cell.children if part.token and part.token.type == "inline"), None)
                    values.append(_inline_text(inline) if inline else "")
            if section.token.type == "thead_open":
                header_row = values
            else:
                data_rows.append(values)

    if not data_rows:
        return " | ".join(header_row)
    if not header_row:
        return "\n".join(" | ".join(row) for row in data_rows)
    rendered_rows = []
    for row in data_rows:
        cells = [f"{label}: {value}" for label, value in zip(header_row, row, strict=False) if label or value]
        rendered_rows.append(", ".join(cells))
    return "\n".join(rendered_rows)


def _render_block(node: _Node, *, max_chars: int) -> list[_Rendered]:
    token = node.token
    if token is None:
        return _joined([_render_block(child, max_chars=max_chars) for child in node.children], "\n\n")

    if token.type in {"paragraph_open", "heading_open"}:
        inline = next((child for child in node.children if child.token and child.token.type == "inline"), None)
        return _inline_node(inline, max_chars=max_chars) if inline else []
    if token.type in {"bullet_list_open", "ordered_list_open"}:
        return _render_list(node, max_chars=max_chars)
    if token.type == "blockquote_open":
        quoted = _flatten(_joined([_plain_block(child) for child in node.children if child.token], "\n\n")).strip()
        if not quoted:
            return []
        return ["\n".join(f"> {line}" if line else ">" for line in quoted.splitlines())]
    if token.type == "table_open":
        value = _table_text(node)
        return [value] if value else []
    if token.type in {"fence", "code_block"}:
        if len("<pre><code></code></pre>") + len(html.escape(token.content[:1], quote=False)) > max_chars:
            return [token.content]
        return [_Element("pre", [_Element("code", [token.content])])]
    if token.type in {"hr", "paragraph_close", "heading_close", "list_item_close"}:
        return []
    if token.type in {"thead_open", "tbody_open", "tr_open", "th_open", "td_open", "inline"}:
        return []
    if token.type in {"html_block"}:
        return [token.content] if token.content else []
    return _joined([_render_block(child, max_chars=max_chars) for child in node.children], "\n")


def _plain_block(node: _Node) -> list[_Rendered]:
    token = node.token
    if token is None:
        return _joined([_plain_block(child) for child in node.children], "\n")
    if token.type in {"paragraph_open", "heading_open"}:
        inline = next((child for child in node.children if child.token and child.token.type == "inline"), None)
        return [_inline_text(inline)] if inline else []
    if token.type in {"bullet_list_open", "ordered_list_open"}:
        rendered = _render_list(node, max_chars=4096)
        return [_flatten(rendered)]
    if token.type == "blockquote_open":
        quoted = _flatten(_joined([_plain_block(child) for child in node.children if child.token], "\n\n")).strip()
        return ["\n".join(f"> {line}" if line else ">" for line in quoted.splitlines())] if quoted else []
    if token.type == "table_open":
        return [_table_text(node)]
    if token.type in {"fence", "code_block", "html_block"}:
        return [token.content]
    return _joined([_plain_block(child) for child in node.children], "\n")


def _flatten(nodes: list[_Rendered], *, include_link: bool = True) -> str:
    output = []
    for node in nodes:
        if isinstance(node, str):
            output.append(node)
            continue
        text = _flatten(node.children, include_link=include_link)
        if node.tag == "a" and node.href and include_link and text and node.href not in text:
            text += f" ({node.href})"
        output.append(text)
    return "".join(output)


def agent_message_plain_text(text: str) -> str:
    """Remove Markdown formatting while preserving readable structure and links."""

    rendered = _flatten(_render_block(_parse_markdown(text), max_chars=4096)).strip()
    return rendered or text.strip()


def _opening_tag(element: _Element) -> str:
    if element.tag == "a" and element.href is not None:
        return f'<a href="{html.escape(element.href, quote=True)}">'
    return f"<{element.tag}>"


def _closing_tag(element: _Element) -> str:
    return f"</{element.tag}>"


def _has_text(node: _Rendered) -> bool:
    if isinstance(node, str):
        return bool(node)
    return any(_has_text(child) for child in node.children)


def _operations(nodes: list[_Rendered]):
    for node in nodes:
        if isinstance(node, str):
            for character in node:
                yield "text", character
            continue
        if not _has_text(node):
            continue
        yield "open", node
        yield from _operations(node.children)
        yield "close", node


def _utf16_units(character: str) -> int:
    return len(character.encode("utf-16-le")) // 2


def _chunk_html(nodes: list[_Rendered], *, max_chars: int) -> list[str]:
    chunks: list[str] = []
    current: list[str] = []
    active: list[_Element] = []
    suppressed: set[int] = set()
    visible_units = 0
    raw_length = 0

    def closing_length() -> int:
        return sum(len(_closing_tag(element)) for element in active)

    def flush() -> None:
        nonlocal current, raw_length, visible_units
        for element in reversed(active):
            current.append(_closing_tag(element))
        chunk = "".join(current)
        if chunk:
            if len(chunk) > max_chars:
                raise ValueError("Rendered Telegram message chunk exceeds its limit")
            chunks.append(chunk)
        current = [_opening_tag(element) for element in active]
        raw_length = sum(map(len, current))
        visible_units = 0

    for operation, value in _operations(nodes):
        if operation == "open":
            element = value
            assert isinstance(element, _Element)
            opening = _opening_tag(element)
            required = len(opening) + len(_closing_tag(element)) + closing_length()
            if visible_units and raw_length + required > max_chars:
                flush()
            if raw_length + required > max_chars:
                # Formatting is optional when an enclosing tag or destination
                # would leave no room for text in this caller-sized chunk.
                suppressed.add(id(element))
                continue
            active.append(element)
            current.append(opening)
            raw_length += len(opening)
        elif operation == "close":
            element = value
            assert isinstance(element, _Element)
            if id(element) in suppressed:
                suppressed.remove(id(element))
                continue
            closing = _closing_tag(element)
            current.append(closing)
            raw_length += len(closing)
            if active and active[-1] is element:
                active.pop()
        else:
            character = value
            assert isinstance(character, str)
            escaped = html.escape(character, quote=False)
            units = _utf16_units(character)
            needs_break = visible_units and (
                raw_length + len(escaped) + closing_length() > max_chars
                or visible_units + units > max_chars
            )
            if needs_break:
                flush()
            if raw_length + len(escaped) + closing_length() > max_chars or visible_units + units > max_chars:
                raise ValueError("Telegram message formatting leaves no room for text")
            current.append(escaped)
            raw_length += len(escaped)
            visible_units += units

    for element in reversed(active):
        current.append(_closing_tag(element))
        raw_length += len(_closing_tag(element))
    if current:
        chunk = "".join(current)
        if len(chunk) > max_chars:
            raise ValueError("Rendered Telegram message chunk exceeds its limit")
        chunks.append(chunk)
    return chunks


def render_agent_message_chunks(text: str, *, max_chars: int = 4096) -> list[str]:
    """Render Markdown as safe, balanced Telegram HTML chunks.

    Chunks stay within both the serialized limit used by Eidolon's Telegram
    provider and Telegram's visible UTF-16 message limit. Raw HTML and unsafe
    links are displayed as text; images are represented only by their alt text.
    """

    if isinstance(max_chars, bool) or not isinstance(max_chars, int) or max_chars < 1:
        raise ValueError("max_chars must be a positive integer")
    nodes = _render_block(_parse_markdown(text), max_chars=max_chars)
    if not _flatten(nodes).strip():
        nodes = [text.strip()] if text.strip() else []
    return _chunk_html(nodes, max_chars=max_chars)
