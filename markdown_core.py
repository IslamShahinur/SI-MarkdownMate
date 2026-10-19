"""Markdown helpers for SI MarkdownMate.

Everything here works on plain strings. There is no terminal code, no file
access and no third-party import, so the whole module is fast and easy to test.

Contents
    * suggestions / autocomplete templates   (get_suggestions)
    * toggle bold / italic                   (toggle_wrap)
    * HTML colour and font-size spans        (apply_style_span, color_span, ...)
    * horizontal dividers                    (insert_divider)
    * structural validation                  (validate_markdown)
    * text search                            (search_text, find_offsets)
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

# --------------------------------------------------------------------------
# Data types and constants
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Suggestion:
    """One entry in the suggestion menu.

    ``replace_len`` characters directly before the cursor (the text the user
    already typed to trigger the menu) are replaced by ``insert``. The cursor
    is then placed ``cursor_offset`` characters after the start of ``insert``.
    """

    label: str
    insert: str
    cursor_offset: int
    description: str = ""
    replace_len: int = 0


@dataclass(frozen=True)
class Issue:
    """A Markdown validation finding. ``line`` is 1-based."""

    line: int
    message: str
    severity: str = "warning"  # "warning" or "error"

    def __str__(self) -> str:
        return f"Line {self.line}: {self.message}"


COLORS = ("red", "orange", "green", "blue", "purple", "gray", "black", "white")
FONT_SIZES = (12, 14, 16, 18, 20, 24, 28, 32)

MIN_FONT_SIZE = 8
MAX_FONT_SIZE = 96

_HEX_COLOR_RE = re.compile(r"^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6})$")

# --------------------------------------------------------------------------
# Fenced code block detection (shared by suggestions, validation, the editor)
# --------------------------------------------------------------------------

_FENCE_RE = re.compile(r"^ {0,3}(`{3,}|~{3,})(.*)$")


def code_line_mask(lines: list[str]) -> tuple[list[bool], Optional[tuple[int, str]]]:
    """Return ``(mask, unclosed)`` for a list of lines.

    ``mask[i]`` is True when line ``i`` is a fence line or sits inside a fenced
    code block. ``unclosed`` is ``(line_number, fence)`` when a fence is never
    closed, otherwise ``None``.
    """
    mask = [False] * len(lines)
    open_fence: Optional[tuple[str, int, int]] = None  # (char, length, line_no)

    for index, line in enumerate(lines):
        match = _FENCE_RE.match(line)
        if open_fence is None:
            if not match:
                continue
            fence, info = match.group(1), match.group(2)
            if fence[0] == "`" and "`" in info:
                continue  # e.g. ```inline code``` - not a fence
            open_fence = (fence[0], len(fence), index + 1)
            mask[index] = True
        else:
            mask[index] = True
            if (
                match
                and match.group(1)[0] == open_fence[0]
                and len(match.group(1)) >= open_fence[1]
                and not match.group(2).strip()
            ):
                open_fence = None

    unclosed = None
    if open_fence is not None:
        unclosed = (open_fence[2], open_fence[0] * open_fence[1])
    return mask, unclosed


def in_code_fence(text: str) -> bool:
    """True when ``text`` ends while a fenced code block is still open."""
    _, unclosed = code_line_mask(text.split("\n"))
    return unclosed is not None


# --------------------------------------------------------------------------
# Suggestions
# --------------------------------------------------------------------------

_TABLE_2 = "| Column 1 | Column 2 |\n| --- | --- |\n|  |  |"
_TABLE_3 = "| Column 1 | Column 2 | Column 3 |\n| --- | --- | --- |\n|  |  |  |"


def _divider_text(previous_line_has_text: bool) -> str:
    # A "---" directly under a text line turns that line into a heading
    # (Setext heading), so a blank line must come first.
    return ("\n" if previous_line_has_text else "") + "---\n\n"


def _heading(level: int) -> Suggestion:
    hashes = "#" * level
    return Suggestion(
        f"{hashes} Heading {level}", f"{hashes} ", level + 1, f"Level {level} heading"
    )


def _slash_catalog(previous_line_has_text: bool) -> list[tuple[tuple[str, ...], Suggestion]]:
    divider = _divider_text(previous_line_has_text)
    return [
        (("h1", "heading", "title"), _heading(1)),
        (("h2", "heading", "subtitle"), _heading(2)),
        (("h3", "heading"), _heading(3)),
        (("bullet", "list", "unordered", "ul"), Suggestion("Bulleted list", "- ", 2, "List item")),
        (("numbered", "ordered", "list", "ol"), Suggestion("Numbered list", "1. ", 3, "Numbered item")),
        (("task", "todo", "checkbox"), Suggestion("Task", "- [ ] ", 6, "Unchecked task")),
        (("done", "completed", "checked"), Suggestion("Completed task", "- [x] ", 6, "Checked task")),
        (("quote", "blockquote"), Suggestion("Blockquote", "> ", 2, "Quoted text")),
        (("code", "fence", "block"), Suggestion("Code block", "```\n\n```", 4, "Fenced code block")),
        (("table",), Suggestion("Table", _TABLE_2, 2, "2-column table")),
        (("divider", "hr", "rule", "line"), Suggestion("Divider", divider, len(divider), "Horizontal rule")),
        (("link",), Suggestion("Link", "[](url)", 1, "[text](url)")),
        (("image", "img", "picture"), Suggestion("Image", "![](image-url)", 2, "![alt](image-url)")),
        (("bold", "strong"), Suggestion("Bold", "****", 2, "**text**")),
        (("italic", "emphasis"), Suggestion("Italic", "**", 1, "*text*")),
        (("inline", "code", "mono"), Suggestion("Inline code", "``", 1, "`code`")),
    ]


def get_suggestions(text_before_cursor: str) -> list[Suggestion]:
    """Return context-aware suggestions for the text before the cursor.

    Suggestions only appear for clear Markdown triggers (a lone ``#``, ``-``,
    ``>``, ``|``, a code fence, ``[``, ``![`` ...), never for ordinary prose, and
    never inside a fenced code block.
    """
    if "\n" in text_before_cursor:
        earlier, line = text_before_cursor.rsplit("\n", 1)
    else:
        earlier, line = "", text_before_cursor

    if not line:
        return []

    previous_line = earlier.rsplit("\n", 1)[-1] if "\n" in text_before_cursor else ""
    found = _match_triggers(line, previous_line.strip() != "")
    if not found:
        return []

    if earlier and in_code_fence(earlier):
        return []
    return found


def _match_triggers(line: str, previous_line_has_text: bool) -> list[Suggestion]:
    # Headings: "#", "##", ... typed on an otherwise empty line.
    match = re.fullmatch(r"( {0,3})(#{1,6})", line)
    if match:
        typed = len(match.group(2))
        order = [typed] + [n for n in range(1, 5) if n != typed]
        return [
            _with_replace(_heading(level), typed) for level in order[:4]
        ]

    # Dividers: "--", "---", "***", "___".
    match = re.fullmatch(r"(\s*)(-{2,}|\*{3,}|_{3,})", line)
    if match:
        text = _divider_text(previous_line_has_text)
        return [
            Suggestion("Divider", text, len(text), "Horizontal rule", len(match.group(2)))
        ]

    # Bullet / task list markers.
    match = re.fullmatch(r"(\s*)([-*+])", line)
    if match:
        marker = match.group(2)
        items = [
            Suggestion("Bulleted list", f"{marker} ", 2, "List item", 1),
            Suggestion("Task", f"{marker} [ ] ", 6, "Unchecked task", 1),
            Suggestion("Completed task", f"{marker} [x] ", 6, "Checked task", 1),
        ]
        if marker == "*":
            items += [
                Suggestion("Italic", "**", 1, "*text*", 1),
                Suggestion("Bold", "****", 2, "**text**", 1),
            ]
        return items

    # Ordered list: "1." at line start.
    match = re.fullmatch(r"(\s*)(\d{1,3})\.", line)
    if match:
        number = match.group(2)
        return [
            Suggestion("Numbered list", f"{number}. ", len(number) + 2, "Numbered item", len(number) + 1)
        ]

    # Blockquote.
    if re.fullmatch(r"\s*>", line):
        return [Suggestion("Blockquote", "> ", 2, "Quoted text", 1)]

    # Code fences.
    match = re.fullmatch(r"( {0,3})(`{3,}|~{3,})", line)
    if match:
        fence = match.group(2)
        items = []
        for language, description in (
            ("python", "Python code"),
            ("bash", "Shell commands"),
            ("json", "JSON data"),
            ("", "Plain code block"),
        ):
            opening = f"{fence}{language}\n"
            items.append(
                Suggestion(
                    f"{fence}{language}" if language else f"{fence} (plain)",
                    f"{opening}\n{fence}",
                    len(opening),
                    description,
                    len(fence),
                )
            )
        return items

    # Tables.
    if re.fullmatch(r"\s*\|", line):
        return [
            Suggestion("Table (2 columns)", _TABLE_2, 2, "Markdown table", 1),
            Suggestion("Table (3 columns)", _TABLE_3, 2, "Markdown table", 1),
        ]

    # Slash menu: "/" or "/ta" at line start lists every template.
    match = re.fullmatch(r"(\s*)/([A-Za-z0-9]*)", line)
    if match:
        typed = match.group(2).lower()
        replace_len = 1 + len(typed)
        results = []
        for keywords, suggestion in _slash_catalog(previous_line_has_text):
            if not typed or any(word.startswith(typed) for word in keywords):
                results.append(_with_replace(suggestion, replace_len))
        return results

    # Task-list checkbox after a bullet: "- [".
    match = re.fullmatch(r"(\s*[-*+] )\[", line)
    if match:
        return [
            Suggestion("[ ] Task", "[ ] ", 4, "Unchecked task", 1),
            Suggestion("[x] Completed", "[x] ", 4, "Checked task", 1),
        ]

    # Images and links (the "[" must start a new token).
    if line.endswith("!["):
        return [Suggestion("Image", "![](image-url)", 2, "![alt](image-url)", 2)]
    if re.search(r"(?:^|[\s(])\[$", line):
        return [Suggestion("Link", "[](url)", 1, "[text](url)", 1)]

    # Inline code: a single backtick that starts a new token.
    if re.search(r"(?:^|\s)`$", line):
        return [Suggestion("Inline code", "``", 1, "`code`", 1)]

    # Bold / italic markers that start a new token.
    if re.search(r"(?:^|\s)\*\*$", line):
        return [Suggestion("Bold", "****", 2, "**text**", 2)]
    if re.search(r"(?:^|\s)\*$", line):
        return [
            Suggestion("Italic", "**", 1, "*text*", 1),
            Suggestion("Bold", "****", 2, "**text**", 1),
        ]

    return []


def _with_replace(suggestion: Suggestion, replace_len: int) -> Suggestion:
    return Suggestion(
        suggestion.label,
        suggestion.insert,
        suggestion.cursor_offset,
        suggestion.description,
        replace_len,
    )


# --------------------------------------------------------------------------
# Selection helpers
# --------------------------------------------------------------------------


def _clamp_range(text: str, start: int, end: int) -> tuple[int, int]:
    start = max(0, min(start, len(text)))
    end = max(0, min(end, len(text)))
    if start > end:
        start, end = end, start
    return start, end


def _is_word_char(char: str) -> bool:
    return char.isalnum() or char in "_'’-"


def _word_bounds(text: str, position: int) -> tuple[int, int]:
    """Bounds of the word touching ``position``; ``(position, position)`` if none."""
    start = position
    while start > 0 and _is_word_char(text[start - 1]):
        start -= 1
    end = position
    while end < len(text) and _is_word_char(text[end]):
        end += 1
    if not any(char.isalnum() for char in text[start:end]):
        return position, position
    return start, end


def _trim_whitespace(text: str, start: int, end: int) -> tuple[int, int]:
    while start < end and text[start].isspace():
        start += 1
    while end > start and text[end - 1].isspace():
        end -= 1
    return start, end


# --------------------------------------------------------------------------
# Bold / italic
# --------------------------------------------------------------------------


def toggle_wrap(text: str, start: int, end: int, marker: str) -> tuple[str, int, int]:
    """Toggle a Markdown wrapper (``**``, ``*``, ``~~``, ...) around a range.

    ``start``/``end`` delimit the selection (``start == end`` means "no
    selection": the word at the cursor is used, or an empty pair is inserted).
    Returns ``(new_text, new_start, new_end)`` where the new range covers the
    original text, so formatting can be toggled repeatedly.
    """
    if not marker:
        raise ValueError("marker must not be empty")

    start, end = _clamp_range(text, start, end)
    if start == end:
        start, end = _word_bounds(text, start)
    start, end = _trim_whitespace(text, start, end)

    size = len(marker)
    if start == end:
        # Nothing to wrap: insert an empty pair and put the cursor inside it.
        pos = start
        return text[:pos] + marker * 2 + text[pos:], pos + size, pos + size

    if set(marker) == {"*"}:
        return _toggle_asterisks(text, start, end, size)

    # Generic markers.
    if start >= size and text[start - size:start] == marker and text[end:end + size] == marker:
        return text[:start - size] + text[start:end] + text[end + size:], start - size, end - size
    selected = text[start:end]
    if len(selected) >= size * 2 and selected.startswith(marker) and selected.endswith(marker):
        inner = selected[size:-size]
        return text[:start] + inner + text[end:], start, start + len(inner)
    return (
        text[:start] + marker + selected + marker + text[end:],
        start + size,
        end + size,
    )


def _toggle_asterisks(text: str, start: int, end: int, size: int) -> tuple[str, int, int]:
    """Bold (``**``) and italic (``*``) share a character, so count the run.

    Around a word, ``*x*`` is italic, ``**x**`` is bold and ``***x***`` is both.
    """
    original = (text, start, end)

    # If the selection itself includes the asterisks, look inside them.
    while start < end and text[start] == "*":
        start += 1
    while end > start and text[end - 1] == "*":
        end -= 1
    if start == end:
        return original

    left = 0
    while start - left - 1 >= 0 and text[start - left - 1] == "*":
        left += 1
    right = 0
    while end + right < len(text) and text[end + right] == "*":
        right += 1

    run = min(left, right, 3)
    present = run >= 2 if size == 2 else run % 2 == 1

    if present:
        new_text = text[:start - size] + text[start:end] + text[end + size:]
        return new_text, start - size, end - size

    marker = "*" * size
    new_text = text[:start] + marker + text[start:end] + marker + text[end:]
    return new_text, start + size, end + size


# --------------------------------------------------------------------------
# Dividers
# --------------------------------------------------------------------------


def insert_divider(text: str, cursor: int) -> tuple[str, int]:
    """Insert a portable ``---`` divider at the cursor.

    A blank line is kept before the rule (otherwise Markdown reads the line
    above as a heading) and the cursor ends up below it.
    """
    cursor = max(0, min(cursor, len(text)))
    before, after = text[:cursor], text[cursor:]

    if not before or before.endswith("\n\n"):
        lead = ""
    elif before.endswith("\n"):
        lead = "\n"
    else:
        lead = "\n\n"

    trail = "---\n" if after.startswith("\n") else "---\n\n"
    insertion = lead + trail
    return before + insertion + after, cursor + len(insertion)


# --------------------------------------------------------------------------
# HTML colour / font-size spans (NOT standard Markdown)
# --------------------------------------------------------------------------


def normalize_color(color: str) -> str:
    """Validate a colour: a palette name or a ``#rgb`` / ``#rrggbb`` value."""
    value = str(color).strip().lower()
    if value in COLORS or _HEX_COLOR_RE.match(value):
        return value
    raise ValueError(
        f"Unknown colour {color!r}. Use one of {', '.join(COLORS)} or a hex value like #ff8800."
    )


def normalize_font_size(size) -> int:
    """Validate a font size in pixels."""
    try:
        value = int(str(size).strip().lower().removesuffix("px"))
    except (TypeError, ValueError):
        raise ValueError(f"Font size must be a whole number, not {size!r}.") from None
    if not MIN_FONT_SIZE <= value <= MAX_FONT_SIZE:
        raise ValueError(f"Font size must be between {MIN_FONT_SIZE} and {MAX_FONT_SIZE} px.")
    return value


def color_span(text: str, color: str) -> str:
    """HTML colour span. Not portable standard Markdown."""
    return f'<span style="color: {normalize_color(color)};">{text}</span>'


def font_size_span(text: str, size) -> str:
    """HTML font-size span. Renderer support varies."""
    return f'<span style="font-size: {normalize_font_size(size)}px;">{text}</span>'


_SPAN_OPEN = r'<span style="(color|font-size):\s*([^;"<>]+);">'
_SPAN_FULL_RE = re.compile(r"\A" + _SPAN_OPEN + r"(.*)</span>\Z", re.DOTALL)
_SPAN_BEFORE_RE = re.compile(_SPAN_OPEN + r"\Z")


def _span(prop: str, css: str, inner: str) -> str:
    return f'<span style="{prop}: {css};">{inner}</span>'


def apply_style_span(
    text: str, start: int, end: int, prop: str, value
) -> tuple[str, int, int]:
    """Apply, change or remove an HTML ``color`` / ``font-size`` span.

    ``value`` is a colour / size, or ``None`` to remove the span. With no
    selection the word at the cursor is used, or a placeholder is inserted.
    Returns ``(new_text, new_start, new_end)``. When there is nothing to
    remove the text is returned unchanged.
    """
    if prop not in ("color", "font-size"):
        raise ValueError("prop must be 'color' or 'font-size'")

    css: Optional[str] = None
    if value is not None:
        css = normalize_color(value) if prop == "color" else f"{normalize_font_size(value)}px"

    start, end = _clamp_range(text, start, end)
    selected = text[start:end]

    # The selection is exactly one existing span.
    match = _SPAN_FULL_RE.match(selected) if selected else None
    if match and match.group(1) == prop:
        inner = match.group(3)
        new = inner if css is None else _span(prop, css, inner)
        return text[:start] + new + text[end:], start, start + len(new)

    # The selection is the inside of an existing span.
    before = _SPAN_BEFORE_RE.search(text[:start])
    if before and before.group(1) == prop and text.startswith("</span>", end):
        inner = text[start:end]
        new = inner if css is None else _span(prop, css, inner)
        open_start = before.start()
        new_text = text[:open_start] + new + text[end + len("</span>"):]
        return new_text, open_start, open_start + len(new)

    if css is None:
        return text, start, end  # nothing to remove

    if start == end:
        start, end = _word_bounds(text, start)
        selected = text[start:end]
        if start == end:
            placeholder = "text"
            opening = f'<span style="{prop}: {css};">'
            new = opening + placeholder + "</span>"
            inner_start = start + len(opening)
            return text[:start] + new + text[end:], inner_start, inner_start + len(placeholder)

    new = _span(prop, css, selected)
    return text[:start] + new + text[end:], start, start + len(new)


# --------------------------------------------------------------------------
# Validation
# --------------------------------------------------------------------------

_CODE_SPAN_RE = re.compile(r"(?<!`)(`+)(?!`)(.+?)(?<!`)\1(?!`)")
_LINK_EMPTY_RE = re.compile(r"(!?)\[[^\]]*\]\(\s*\)")
_LINK_OPEN_RE = re.compile(r"(!?)\[[^\]]*\]\([^)]*$")
_LINK_NO_TEXT_RE = re.compile(r"(?<!!)\[\]\(\s*[^)\s]")
_IMAGE_BARE_RE = re.compile(r"!\[([^\]]*)\](?![(\[])")
_REF_DEF_RE = re.compile(r"^ {0,3}\[([^\]]+)\]:")
_SEPARATOR_RE = re.compile(r"^\s*\|?\s*:?-+:?\s*(?:\|\s*:?-+:?\s*)*\|?\s*$")
_RULE_RE = re.compile(r"^ {0,3}([-*_])(?:[ \t]*\1){2,}[ \t]*$")
_HEADING_RE = re.compile(r"^ {0,3}#{1,6}(?:\s|$)")
_LIST_START_RE = re.compile(r"^\s*(?:[*+-]|\d+[.)])\s+")


def _blank_out(match: "re.Match[str]") -> str:
    return " " * len(match.group(0))


def _strip_code_spans(line: str) -> str:
    return _CODE_SPAN_RE.sub(_blank_out, line)


def _split_row(line: str) -> list[str]:
    body = line.strip()
    if body.startswith("|"):
        body = body[1:]
    if body.endswith("|") and not body.endswith("\\|"):
        body = body[:-1]
    return re.split(r"(?<!\\)\|", body)


def _is_separator_row(line: str) -> bool:
    return "|" in line and bool(_SEPARATOR_RE.match(line))


def validate_markdown(text: str) -> list[Issue]:
    """Report common, high-confidence structural problems.

    Unusual-but-valid Markdown is not flagged; most findings are warnings.
    """
    lines = text.replace("\r\n", "\n").split("\n")
    mask, unclosed = code_line_mask(lines)
    issues: list[Issue] = []

    if unclosed:
        issues.append(
            Issue(unclosed[0], f"Unclosed fenced code block (opened with {unclosed[1]}).", "error")
        )

    clean = ["" if mask[i] else _strip_code_spans(line) for i, line in enumerate(lines)]
    definitions = {
        match.group(1).strip().lower()
        for line, masked in zip(lines, mask)
        if not masked and (match := _REF_DEF_RE.match(line))
    }

    _check_links(clean, definitions, issues)
    table_lines = _check_tables(lines, clean, mask, issues)
    _check_emphasis(clean, mask, table_lines, issues)
    _check_html_spans(clean, issues)

    issues.sort(key=lambda issue: (issue.line, issue.message))
    return issues


def _check_links(clean: list[str], definitions: set[str], issues: list[Issue]) -> None:
    for number, line in enumerate(clean, start=1):
        if not line:
            continue
        for match in _LINK_EMPTY_RE.finditer(line):
            kind = "image" if match.group(1) else "link"
            issues.append(Issue(number, f"{kind.capitalize()} has an empty destination."))
        match = _LINK_OPEN_RE.search(line)
        if match and not _LINK_EMPTY_RE.search(line[match.start():]):
            kind = "image" if match.group(1) else "link"
            issues.append(Issue(number, f"Incomplete {kind}: missing closing ')'."))
        if _LINK_NO_TEXT_RE.search(line):
            issues.append(Issue(number, "Link has no text."))
        for match in _IMAGE_BARE_RE.finditer(line):
            alt = match.group(1).strip().lower()
            if alt not in definitions:
                issues.append(Issue(number, "Image has no destination: use ![alt](image-url)."))


def _check_tables(
    lines: list[str], clean: list[str], mask: list[bool], issues: list[Issue]
) -> set[int]:
    """Validate pipe tables. Returns the 0-based indexes of table lines."""
    in_table: set[int] = set()
    index = 0
    count = len(lines)

    while index < count:
        line = lines[index]
        if (
            not mask[index]
            and "|" in line
            and index + 1 < count
            and not mask[index + 1]
            and not _is_separator_row(line)
            and _is_separator_row(lines[index + 1])
        ):
            columns = len(_split_row(line))
            separator_columns = len(_split_row(lines[index + 1]))
            in_table.update((index, index + 1))
            if separator_columns != columns:
                issues.append(
                    Issue(
                        index + 2,
                        f"Table separator has {separator_columns} column(s) but the header has {columns}.",
                    )
                )
            row = index + 2
            while row < count and not mask[row] and lines[row].strip() and "|" in lines[row]:
                cells = len(_split_row(lines[row]))
                in_table.add(row)
                if cells != columns:
                    issues.append(
                        Issue(row + 1, f"Table row has {cells} cell(s) but the header has {columns}.")
                    )
                row += 1
            index = row
            continue
        index += 1

    previous_orphan = False
    for index, line in enumerate(lines):
        orphan = (
            not mask[index]
            and index not in in_table
            and line.lstrip().startswith("|")
        )
        if orphan and not previous_orphan:
            issues.append(
                Issue(index + 1, "Table row without a header separator row (| --- | --- |).")
            )
        previous_orphan = orphan
    return in_table


def _emphasis_text(line: str) -> str:
    """Blank out everything in a line that must not count as an emphasis marker."""
    text = re.sub(r"\\.", "  ", line)
    text = re.sub(r"^(\s*>)+", _blank_out, text)
    text = _LIST_START_RE.sub(_blank_out, text, count=1)
    text = re.sub(r"\]\([^)]*\)", lambda m: "]" + " " * (len(m.group(0)) - 1), text)
    text = re.sub(r"<[^>\n]*>", _blank_out, text)
    return text


def _check_emphasis(
    clean: list[str], mask: list[bool], table_lines: set[int], issues: list[Issue]
) -> None:
    paragraph: list[tuple[int, str]] = []

    def flush() -> None:
        if paragraph:
            issues.extend(_emphasis_issues(paragraph))
            paragraph.clear()

    for index, line in enumerate(clean):
        if mask[index] or not line.strip() or _RULE_RE.match(line) or index in table_lines:
            flush()
            continue
        starts_block = bool(_HEADING_RE.match(line) or _LIST_START_RE.match(line))
        if starts_block:
            flush()
        paragraph.append((index + 1, _emphasis_text(line)))
        if _HEADING_RE.match(line):
            flush()
    flush()


def _emphasis_issues(items: list[tuple[int, str]]) -> list[Issue]:
    joined = "\n".join(text for _, text in items)
    starts: list[tuple[int, int]] = []
    position = 0
    for number, text in items:
        starts.append((position, number))
        position += len(text) + 1

    def line_at(offset: int) -> int:
        number = items[0][0]
        for start, line_number in starts:
            if start <= offset:
                number = line_number
            else:
                break
        return number

    issues: list[Issue] = []
    stack: list[list[int]] = []  # [remaining asterisks, offset]

    for match in re.finditer(r"\*+", joined):
        before = joined[match.start() - 1] if match.start() > 0 else " "
        after = joined[match.end()] if match.end() < len(joined) else " "
        can_open = not after.isspace()
        can_close = not before.isspace()
        if not (can_open or can_close):
            continue  # e.g. "2 * 3": a literal asterisk

        remaining = len(match.group(0))
        if can_close:
            while remaining and stack:
                take = min(remaining, stack[-1][0])
                stack[-1][0] -= take
                remaining -= take
                if stack[-1][0] == 0:
                    stack.pop()
        if remaining:
            if can_open:
                stack.append([remaining, match.start()])
            else:
                issues.append(
                    Issue(line_at(match.start()), "Possible unmatched closing emphasis marker (*).")
                )

    for remaining, offset in stack:
        issues.append(
            Issue(
                line_at(offset),
                f"Possible unmatched emphasis marker ({'*' * remaining}): it is never closed.",
            )
        )
    return issues


def _check_html_spans(clean: list[str], issues: list[Issue]) -> None:
    opened: list[int] = []
    for number, line in enumerate(clean, start=1):
        for match in re.finditer(r"<span\b[^>]*>|</span\s*>", line, re.IGNORECASE):
            if match.group(0).startswith("</"):
                if opened:
                    opened.pop()
                else:
                    issues.append(Issue(number, "Closing </span> without an opening <span>."))
            else:
                opened.append(number)
    for number in opened:
        issues.append(Issue(number, "<span> is never closed."))


# --------------------------------------------------------------------------
# Search
# --------------------------------------------------------------------------


def _compile(query: str, case_sensitive: bool) -> "re.Pattern[str]":
    return re.compile(re.escape(query), 0 if case_sensitive else re.IGNORECASE)


def search_text(text: str, query: str, case_sensitive: bool = False) -> list[tuple[int, int]]:
    """Return ``(line_number, column)`` for every match (line 1-based, column 0-based)."""
    if not query:
        return []
    pattern = _compile(query, case_sensitive)
    matches: list[tuple[int, int]] = []
    for number, line in enumerate(text.split("\n"), start=1):
        for match in pattern.finditer(line):
            matches.append((number, match.start()))
    return matches


def find_offsets(text: str, query: str, case_sensitive: bool = False) -> list[tuple[int, int]]:
    """Return ``(start, end)`` character offsets of every match in ``text``."""
    if not query:
        return []
    return [
        (match.start(), match.end())
        for match in _compile(query, case_sensitive).finditer(text)
    ]
