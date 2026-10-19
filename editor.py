"""Interactive terminal editor for SI MarkdownMate.

Layout (top to bottom): the text area, an optional information panel, an
optional one-line prompt, a two-line shortcut bar and a status bar. Suggestions
appear in a small popup next to the cursor.

Design notes
    * Suggestions use our own popup, not prompt_toolkit's completion state:
      browsing prompt_toolkit completions rewrites the buffer text, which would
      put unaccepted text on disk through autosave.
    * Autosave is a real trailing debounce: a timer is restarted on every edit
      and fires once the user pauses, so the last keystroke is never left
      unsaved.
    * Selection is prompt_toolkit's character selection, which includes the
      character under the cursor (like vim's visual mode); formatting acts on
      exactly what is highlighted.
"""

from __future__ import annotations

import asyncio
import os
import re
import time
from pathlib import Path
from typing import Callable, Optional

from prompt_toolkit.application import Application
from prompt_toolkit.buffer import Buffer
from prompt_toolkit.completion import DummyCompleter, PathCompleter
from prompt_toolkit.document import Document
from prompt_toolkit.filters import Condition
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import (
    BufferControl,
    ConditionalContainer,
    Float,
    FloatContainer,
    FormattedTextControl,
    HSplit,
    Layout,
    VSplit,
    Window,
)
from prompt_toolkit.layout.menus import CompletionsMenu
from prompt_toolkit.lexers import Lexer
from prompt_toolkit.selection import SelectionState, SelectionType
from prompt_toolkit.styles import Style

import markdown_core
from file_manager import ExternalChangeError, FileManager, FileManagerError

INDENT = "    "

HELP_LINES = [
    "SI MarkdownMate - keyboard shortcuts (Esc closes this panel)",
    "  Ctrl+S Save / Save As     Ctrl+N New     Ctrl+O Open     Alt+R Rename     Ctrl+Q Save and quit",
    "  Ctrl+F Find in document   F3 / F4 Next / previous match   Alt+F Search every .md file in this folder",
    "  Ctrl+B Bold   Alt+I Italic   Alt+C Text color (HTML)   Alt+S Font size (HTML)   Alt+V Check Markdown",
    "  Alt+Enter or Alt+D Insert a --- divider   Tab Accept suggestion / indent   Shift+Tab Outdent",
    "  Esc Dismiss suggestions   Up / Down Choose a suggestion   Ctrl+Z / Ctrl+Y Undo / redo",
    "  Shift+Arrows (or Ctrl+Space, then arrows) Select text; Ctrl+B / Alt+I then work on the selection",
    "  Type / at the start of a line for a menu of every template.",
    "  Colors and font sizes are HTML, not standard Markdown: some renderers ignore them.",
]


# --------------------------------------------------------------------------
# Small pure helpers (no prompt_toolkit): easy to test
# --------------------------------------------------------------------------


def line_col(text: str, position: int) -> tuple[int, int]:
    """0-based (row, column) of a character offset."""
    position = max(0, min(position, len(text)))
    row = text.count("\n", 0, position)
    column = position - (text.rfind("\n", 0, position) + 1)
    return row, column


def offset_of(text: str, row: int, column: int) -> int:
    """Character offset for (row, column), clamped to the line."""
    lines = text.split("\n")
    row = max(0, min(row, len(lines) - 1))
    start = sum(len(line) + 1 for line in lines[:row])
    return start + max(0, min(column, len(lines[row])))


def move_cursor(text: str, position: int, direction: str) -> int:
    """New offset after moving one step: left, right, up, down, home or end."""
    position = max(0, min(position, len(text)))
    row, column = line_col(text, position)
    if direction == "left":
        return max(0, position - 1)
    if direction == "right":
        return min(len(text), position + 1)
    if direction == "up":
        return offset_of(text, row - 1, column) if row > 0 else 0
    if direction == "down":
        last_row = text.count("\n")
        return offset_of(text, row + 1, column) if row < last_row else len(text)
    if direction == "home":
        return offset_of(text, row, 0)
    if direction == "end":
        return offset_of(text, row, 10**9)
    raise ValueError(f"unknown direction: {direction}")


def pretty_path(path: Path | str) -> str:
    text = str(path)
    home = str(Path.home())
    if text == home:
        return "~"
    if text.startswith(home + os.sep):
        return "~" + text[len(home):]
    return text


# --------------------------------------------------------------------------
# Syntax highlighting
# --------------------------------------------------------------------------

_HEADING_RE = re.compile(r"^ {0,3}#{1,6}(?:\s|$)")
_QUOTE_RE = re.compile(r"^ {0,3}>")
_RULE_RE = re.compile(r"^ {0,3}([-*_])(?:[ \t]*\1){2,}[ \t]*$")
_LIST_RE = re.compile(r"^(\s*(?:[-*+]|\d+[.)])\s+)")
_INLINE_RE = re.compile(
    r"(?P<code>(?<!`)`[^`\n]+`(?!`))"
    r"|(?P<bold>\*\*(?=\S)[^\n]*?(?<=\S)\*\*)"
    r"|(?P<italic>(?<!\*)\*(?=[^\s*])[^*\n]*?(?<=[^\s*])\*(?!\*))"
    r"|(?P<html></?span[^>\n]*>)"
)
_INLINE_STYLES = {
    "code": "class:md-code-inline",
    "bold": "class:md-bold",
    "italic": "class:md-italic",
    "html": "class:md-html",
}


def inline_fragments(text: str) -> list[tuple[str, str]]:
    """Split a line into (style, text) pieces; the pieces always rejoin to ``text``."""
    fragments: list[tuple[str, str]] = []
    position = 0
    for match in _INLINE_RE.finditer(text):
        if match.start() > position:
            fragments.append(("", text[position:match.start()]))
        fragments.append((_INLINE_STYLES[match.lastgroup], match.group(0)))
        position = match.end()
    if position < len(text) or not fragments:
        fragments.append(("", text[position:]))
    return fragments


class MarkdownLexer(Lexer):
    """Light Markdown highlighting: headings, quotes, code, bold, italic."""

    def lex_document(self, document: Document) -> Callable[[int], list[tuple[str, str]]]:
        lines = document.lines
        mask, _ = markdown_core.code_line_mask(lines)

        def get_line(number: int) -> list[tuple[str, str]]:
            if number >= len(lines):
                return []
            line = lines[number]
            if mask[number]:
                return [("class:md-fence", line)]
            if _HEADING_RE.match(line):
                return [("class:md-heading", line)]
            if _RULE_RE.match(line):
                return [("class:md-rule", line)]
            if _QUOTE_RE.match(line):
                return [("class:md-quote", line)]
            marker = _LIST_RE.match(line)
            if marker:
                return [("class:md-marker", marker.group(1))] + inline_fragments(
                    line[marker.end():]
                )
            return inline_fragments(line)

        return get_line


# --------------------------------------------------------------------------
# The editor
# --------------------------------------------------------------------------


class MarkdownEditor:
    """Full-screen terminal Markdown editor."""

    PANEL_MAX_LINES = 14
    MENU_MAX_ITEMS = 8
    STATUS_MESSAGE_MAX = 120

    def __init__(
        self,
        file_manager: FileManager,
        initial_path: Optional[str] = None,
        *,
        start_blank: bool = False,
        input=None,
        output=None,
    ) -> None:
        self.fm = file_manager

        # Document state.
        self.path: Optional[Path] = None
        self.saved_text = ""
        self.newline = "\n"
        self.disk_mtime_ns: Optional[int] = None
        self.conflict = False
        self.save_error: Optional[str] = None
        self._recent_registered: Optional[Path] = None

        # UI state.
        self.status_message = "Ready. Press F1 for help."
        self.exit_message = ""
        self.exited = False
        self.suggestions: list[markdown_core.Suggestion] = []
        self.suggestion_index = 0
        self._menu_top = 0
        self._dismissed_before: Optional[str] = None
        self.prompt_active = False
        self.prompt_label = ""
        self._prompt_callback: Optional[Callable[[str], None]] = None
        self.panel_lines: list[str] = []
        self.mark_mode = False
        self._extending = False
        self._programmatic = False
        self._loading = False
        self._autosave_handle: Optional[asyncio.TimerHandle] = None
        self._find_query = ""
        self._last_find_span: Optional[tuple[int, int]] = None
        self._issues_cache: tuple[Optional[str], list[markdown_core.Issue]] = (None, [])
        self._quit_error_once = False
        self._open_candidates: list[Path] = []
        self._search_hits: list[tuple[str, int, str]] = []

        self.buffer = Buffer(
            multiline=True,
            on_text_changed=self._on_text_changed,
            on_cursor_position_changed=self._on_cursor_moved,
        )
        self.prompt_buffer = Buffer(
            multiline=False,
            completer=DummyCompleter(),
            accept_handler=self._prompt_accept,
        )
        self._path_completer = PathCompleter(
            expanduser=True,
            file_filter=lambda name: os.path.isdir(name) or name.lower().endswith(".md"),
        )

        self._build_ui()
        self._build_key_bindings()

        self.app = Application(
            layout=self.layout,
            key_bindings=self.bindings,
            full_screen=True,
            style=self._build_style(),
            mouse_support=False,
            input=input,
            output=output,
        )
        # Make a lone Esc feel instant while Alt+<key> still works.
        self.app.timeoutlen = 0.3
        self.app.ttimeoutlen = 0.05

        self._load_initial_document(initial_path, start_blank)

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------

    def _build_ui(self) -> None:
        self.editor_window = Window(
            content=BufferControl(buffer=self.buffer, lexer=MarkdownLexer()),
            wrap_lines=True,
        )

        suggestion_popup = ConditionalContainer(
            Window(
                content=FormattedTextControl(self._menu_fragments),
                style="class:menu",
                dont_extend_width=True,
                dont_extend_height=True,
            ),
            filter=Condition(lambda: self.menu_visible),
        )

        panel = ConditionalContainer(
            Window(
                content=FormattedTextControl(self._panel_fragments),
                height=lambda: max(1, min(len(self.panel_lines), self.PANEL_MAX_LINES)),
                style="class:panel",
            ),
            filter=Condition(lambda: bool(self.panel_lines)),
        )

        self.prompt_window = Window(
            content=BufferControl(buffer=self.prompt_buffer),
            height=1,
            style="class:prompt",
        )
        prompt_row = ConditionalContainer(
            VSplit(
                [
                    Window(
                        content=FormattedTextControl(
                            lambda: [("class:prompt-label", self.prompt_label)]
                        ),
                        height=1,
                        dont_extend_width=True,
                        style="class:prompt",
                    ),
                    self.prompt_window,
                ]
            ),
            filter=Condition(lambda: self.prompt_active),
        )

        help_window = Window(
            content=FormattedTextControl(self._help_fragments),
            height=2,
            style="class:help",
        )
        status_window = Window(
            content=FormattedTextControl(self._status_fragments),
            height=1,
            style="class:status",
        )

        body = HSplit([self.editor_window, panel, prompt_row, help_window, status_window])
        self.root_container = FloatContainer(
            content=body,
            floats=[
                Float(xcursor=True, ycursor=True, content=suggestion_popup),
                Float(xcursor=True, ycursor=True, content=CompletionsMenu(max_height=8)),
            ],
        )
        self.layout = Layout(self.root_container, focused_element=self.editor_window)

    @staticmethod
    def _build_style() -> Style:
        return Style.from_dict(
            {
                "status": "bg:#005f5f #ffffff",
                "status-ok": "bg:#005f5f #87ff87 bold",
                "status-warn": "bg:#005f5f #ffd75f bold",
                "status-bad": "bg:#870000 #ffffff bold",
                "help": "bg:#1c1c1c #9e9e9e",
                "panel": "bg:#262626 #d0d0d0",
                "prompt": "bg:#303030 #ffffff",
                "prompt-label": "bg:#303030 #5fd7ff bold",
                "menu": "bg:#303030 #d0d0d0",
                "menu-selected": "bg:#005f87 #ffffff bold",
                "md-heading": "bold #5fd7ff",
                "md-fence": "#d7af5f",
                "md-quote": "italic #87af87",
                "md-rule": "#808080",
                "md-marker": "#af87d7",
                "md-bold": "bold",
                "md-italic": "italic",
                "md-code-inline": "#d7af5f",
                "md-html": "#808080",
            }
        )

    # ------------------------------------------------------------------
    # Key bindings
    # ------------------------------------------------------------------

    def _build_key_bindings(self) -> None:
        kb = KeyBindings()
        normal = Condition(lambda: not self.prompt_active)
        in_prompt = Condition(lambda: self.prompt_active)
        menu_open = Condition(lambda: self.menu_visible)
        has_selection = Condition(
            lambda: (not self.prompt_active) and self.buffer.selection_state is not None
        )

        def bind(*keys: str, filter=None):
            """Register a binding; skip keys this prompt_toolkit version does not know."""

            def decorator(func):
                try:
                    if filter is None:
                        kb.add(*keys)(func)
                    else:
                        kb.add(*keys, filter=filter)(func)
                except ValueError:
                    pass
                return func

            return decorator

        # --- file commands -------------------------------------------------
        @bind("c-s", filter=normal)
        def _(event):
            self.save(force=True)

        @bind("c-n", filter=normal)
        def _(event):
            self.new_document()

        @bind("c-o", filter=normal)
        def _(event):
            self.open_document()

        @bind("escape", "r", filter=normal)
        def _(event):
            self.rename_document()

        @bind("c-q", filter=normal)
        def _(event):
            self.request_quit()

        @bind("c-c", filter=normal)
        def _(event):
            self.flash("Use Ctrl+Q to quit (your work is saved first).")

        # --- search --------------------------------------------------------
        @bind("c-f", filter=normal)
        def _(event):
            self.search_in_document()

        @bind("f3", filter=normal)
        def _(event):
            self.find_next(1)

        @bind("f4", filter=normal)
        def _(event):
            self.find_next(-1)

        @bind("escape", "f", filter=normal)
        def _(event):
            self.search_folder()

        # --- formatting ----------------------------------------------------
        @bind("c-b", filter=normal)
        def _(event):
            self.toggle_format("**")

        @bind("escape", "i", filter=normal)
        def _(event):
            self.toggle_format("*")

        @bind("escape", "enter", filter=normal)
        def _(event):
            self.insert_divider()

        @bind("escape", "d", filter=normal)
        def _(event):
            self.insert_divider()

        @bind("escape", "c", filter=normal)
        def _(event):
            self.prompt_color()

        @bind("escape", "s", filter=normal)
        def _(event):
            self.prompt_font_size()

        @bind("escape", "v", filter=normal)
        def _(event):
            self.show_validation()

        @bind("f1", filter=normal)
        def _(event):
            self.show_help()

        # --- undo / redo ---------------------------------------------------
        @bind("c-z", filter=normal)
        def _(event):
            self.mark_mode = False
            self.buffer.undo()

        @bind("c-y", filter=normal)
        def _(event):
            self.mark_mode = False
            if hasattr(self.buffer, "redo"):
                self.buffer.redo()

        # --- suggestions ---------------------------------------------------
        @bind("up", filter=menu_open)
        def _(event):
            self.move_suggestion(-1)

        @bind("down", filter=menu_open)
        def _(event):
            self.move_suggestion(1)

        @bind("tab", filter=normal)
        def _(event):
            if self.menu_visible:
                self.accept_suggestion()
            else:
                self.indent()

        @bind("s-tab", filter=normal)
        def _(event):
            self.outdent()

        @bind("escape")
        def _(event):
            self.handle_escape()

        # --- prompt line ---------------------------------------------------
        @bind("tab", filter=in_prompt)
        def _(event):
            prompt = self.prompt_buffer
            if prompt.complete_state:
                prompt.complete_next()
            else:
                prompt.start_completion(select_first=False)

        # --- selection -----------------------------------------------------
        for key, direction in (
            ("s-left", "left"),
            ("s-right", "right"),
            ("s-up", "up"),
            ("s-down", "down"),
            ("s-home", "home"),
            ("s-end", "end"),
        ):
            bind(key, filter=normal)(self._make_extender(direction))

        @bind("c-@", filter=normal)
        def _(event):
            self.toggle_mark()

        @bind("backspace", filter=has_selection)
        def _(event):
            self.delete_selection()

        @bind("delete", filter=has_selection)
        def _(event):
            self.delete_selection()

        self.bindings = kb

    def _make_extender(self, direction: str) -> Callable:
        def handler(event) -> None:
            self.extend_selection(direction)

        return handler

    # ------------------------------------------------------------------
    # Status, help, panel and popup rendering
    # ------------------------------------------------------------------

    @property
    def dirty(self) -> bool:
        return self.buffer.text != self.saved_text

    @property
    def menu_visible(self) -> bool:
        return bool(self.suggestions) and not self.prompt_active

    def flash(self, message: str) -> None:
        self.status_message = message
        self.app.invalidate()

    def _issues(self) -> list[markdown_core.Issue]:
        text = self.buffer.text
        cached_text, cached = self._issues_cache
        if cached_text != text:
            cached = markdown_core.validate_markdown(text)
            self._issues_cache = (text, cached)
        return cached

    def _status_fragments(self):
        name = pretty_path(self.path) if self.path else "Untitled"
        if self.conflict:
            state, style = "CHANGED ON DISK", "class:status-bad"
        elif self.save_error and self.dirty:
            state, style = "SAVE FAILED", "class:status-bad"
        elif self.dirty:
            state, style = "Unsaved", "class:status-warn"
        else:
            state, style = "Saved", "class:status-ok"

        document = self.buffer.document
        position = f"Ln {document.cursor_position_row + 1}, Col {document.cursor_position_col + 1}"
        issues = len(self._issues())

        fragments = [
            ("class:status", f" {name} | "),
            (style, state),
            ("class:status", f" | {position} | Issues: {issues}"),
        ]
        if self.status_message:
            message = self.status_message.replace("\n", " ")
            fragments.append(("class:status", f" | {message[: self.STATUS_MESSAGE_MAX]} "))
        return fragments

    def _help_fragments(self):
        return [
            (
                "class:help",
                " Ctrl+S Save  Ctrl+N New  Ctrl+O Open  Alt+R Rename  Ctrl+F Find  "
                "Alt+F Folder search  Ctrl+Q Quit\n"
                " Ctrl+B Bold  Alt+I Italic  Alt+C Color  Alt+S Size  Alt+Enter Divider  "
                "Alt+V Check  Tab Accept  Esc Close  F1 Help",
            )
        ]

    def _panel_fragments(self):
        lines = self.panel_lines[: self.PANEL_MAX_LINES]
        return [("class:panel", "\n".join(f" {line}" for line in lines))]

    def _sync_menu_scroll(self) -> None:
        if self.suggestion_index < self._menu_top:
            self._menu_top = self.suggestion_index
        elif self.suggestion_index >= self._menu_top + self.MENU_MAX_ITEMS:
            self._menu_top = self.suggestion_index - self.MENU_MAX_ITEMS + 1

    def _menu_fragments(self):
        self._sync_menu_scroll()
        visible = self.suggestions[self._menu_top : self._menu_top + self.MENU_MAX_ITEMS]
        if not visible:
            return []
        label_width = max(len(s.label) for s in visible)
        meta_width = max(len(s.description) for s in visible)

        fragments: list[tuple[str, str]] = []
        for offset, suggestion in enumerate(visible):
            selected = self._menu_top + offset == self.suggestion_index
            style = "class:menu-selected" if selected else "class:menu"
            line = f" {suggestion.label.ljust(label_width)}  {suggestion.description.ljust(meta_width)} "
            fragments.append((style, line))
            if offset < len(visible) - 1:
                fragments.append(("class:menu", "\n"))
        return fragments

    # ------------------------------------------------------------------
    # Buffer events
    # ------------------------------------------------------------------

    def _on_text_changed(self, buffer: Buffer) -> None:
        if self._loading:
            return
        self._last_find_span = None
        self._quit_error_once = False
        self._schedule_autosave()
        self._refresh_suggestions()

    def _on_cursor_moved(self, buffer: Buffer) -> None:
        if self._loading:
            return
        keep = self._extending or self._programmatic or self.mark_mode
        if buffer.selection_state is not None and not keep:
            buffer.selection_state = None
        self._refresh_suggestions()

    # ------------------------------------------------------------------
    # Programmatic edits
    # ------------------------------------------------------------------

    def _apply_edit(
        self, new_text: str, selection_start: int, selection_end: Optional[int] = None
    ) -> None:
        """Replace the document text.

        With ``selection_end`` greater than ``selection_start`` that range is
        left selected; otherwise the cursor is placed at ``selection_start``.
        """
        buffer = self.buffer
        buffer.save_to_undo_stack()
        selecting = selection_end is not None and selection_end > selection_start
        cursor = selection_end - 1 if selecting else selection_start

        self._programmatic = True
        try:
            buffer.document = Document(new_text, max(0, min(cursor, len(new_text))))
            if selecting:
                buffer.selection_state = SelectionState(
                    original_cursor_position=selection_start,
                    type=SelectionType.CHARACTERS,
                )
            else:
                buffer.selection_state = None
        finally:
            self._programmatic = False
        self.mark_mode = False
        self._refresh_suggestions()

    def selection_span(self) -> Optional[tuple[int, int]]:
        """The selected range as ``(start, end)`` with ``end`` exclusive, or None."""
        state = self.buffer.selection_state
        if state is None:
            return None
        text = self.buffer.text
        low = min(state.original_cursor_position, self.buffer.cursor_position)
        high = max(state.original_cursor_position, self.buffer.cursor_position) + 1
        high = min(high, len(text))
        if high <= low:
            return None
        return low, high

    def _range_for_edit(self) -> tuple[int, int]:
        span = self.selection_span()
        if span:
            return span
        cursor = self.buffer.cursor_position
        return cursor, cursor

    # ------------------------------------------------------------------
    # Selection commands
    # ------------------------------------------------------------------

    def extend_selection(self, direction: str) -> None:
        buffer = self.buffer
        if buffer.selection_state is None:
            buffer.selection_state = SelectionState(
                original_cursor_position=buffer.cursor_position,
                type=SelectionType.CHARACTERS,
            )
        self._extending = True
        try:
            buffer.cursor_position = move_cursor(buffer.text, buffer.cursor_position, direction)
        finally:
            self._extending = False
        self._refresh_suggestions()

    def toggle_mark(self) -> None:
        buffer = self.buffer
        if self.mark_mode:
            self.mark_mode = False
            buffer.selection_state = None
            self.flash("Selection off")
            return
        buffer.selection_state = SelectionState(
            original_cursor_position=buffer.cursor_position,
            type=SelectionType.CHARACTERS,
        )
        self.mark_mode = True
        self.flash("Selecting: move with the arrow keys, Esc to cancel")

    def delete_selection(self) -> None:
        span = self.selection_span()
        if span is None:
            return
        text = self.buffer.text
        self._apply_edit(text[: span[0]] + text[span[1] :], span[0])

    def handle_escape(self) -> None:
        if self.prompt_active:
            self._cancel_prompt()
        elif self.menu_visible:
            self._dismiss_suggestions()
        elif self.panel_lines:
            self.panel_lines = []
        elif self.buffer.selection_state is not None or self.mark_mode:
            self.mark_mode = False
            self.buffer.selection_state = None
            self.flash("Selection cleared")

    # ------------------------------------------------------------------
    # Suggestions
    # ------------------------------------------------------------------

    def _set_suggestions(self, suggestions: list[markdown_core.Suggestion]) -> None:
        if suggestions != self.suggestions:
            self.suggestion_index = 0
            self._menu_top = 0
        self.suggestions = suggestions

    def _refresh_suggestions(self) -> None:
        if self._loading or self.prompt_active or self.buffer.selection_state is not None:
            self._set_suggestions([])
            return

        before = self.buffer.document.text_before_cursor
        if self._dismissed_before is not None and before != self._dismissed_before:
            self._dismissed_before = None
        if self._dismissed_before is not None:
            self._set_suggestions([])
            return
        self._set_suggestions(markdown_core.get_suggestions(before))

    def _dismiss_suggestions(self) -> None:
        self._dismissed_before = self.buffer.document.text_before_cursor
        self._set_suggestions([])

    def move_suggestion(self, step: int) -> None:
        if self.suggestions:
            self.suggestion_index = (self.suggestion_index + step) % len(self.suggestions)

    def accept_suggestion(self) -> None:
        if not self.menu_visible:
            return
        suggestion = self.suggestions[self.suggestion_index]
        text = self.buffer.text
        cursor = self.buffer.cursor_position
        start = max(0, cursor - suggestion.replace_len)
        self._set_suggestions([])
        self._apply_edit(
            text[:start] + suggestion.insert + text[cursor:],
            start + suggestion.cursor_offset,
        )

    # ------------------------------------------------------------------
    # Formatting
    # ------------------------------------------------------------------

    def indent(self) -> None:
        self.buffer.insert_text(INDENT)

    def outdent(self) -> None:
        text = self.buffer.text
        cursor = self.buffer.cursor_position
        row, column = line_col(text, cursor)
        line_start = cursor - column
        spaces = len(text[line_start:]) - len(text[line_start:].lstrip(" "))
        remove = min(len(INDENT), spaces)
        if remove:
            self._apply_edit(
                text[:line_start] + text[line_start + remove :], max(line_start, cursor - remove)
            )

    def toggle_format(self, marker: str) -> None:
        start, end = self._range_for_edit()
        text = self.buffer.text
        new_text, new_start, new_end = markdown_core.toggle_wrap(text, start, end, marker)
        if new_text == text:
            self.flash("Nothing to format here.")
            return
        self._apply_edit(new_text, new_start, new_end)
        name = "Bold" if marker == "**" else "Italic" if marker == "*" else "Formatting"
        self.flash(f"{name} toggled")

    def insert_divider(self) -> None:
        text, cursor = markdown_core.insert_divider(self.buffer.text, self.buffer.cursor_position)
        self._apply_edit(text, cursor)
        self.flash("Section divider inserted")

    def insert_color(self, color: Optional[str]) -> None:
        self._apply_style("color", color, "Color")

    def insert_font_size(self, size) -> None:
        self._apply_style("font-size", size, "Font size")

    def _apply_style(self, prop: str, value, label: str) -> None:
        start, end = self._range_for_edit()
        text = self.buffer.text
        try:
            new_text, new_start, new_end = markdown_core.apply_style_span(
                text, start, end, prop, value
            )
        except ValueError as error:
            self.flash(str(error))
            return
        if new_text == text:
            self.flash(f"No {label.lower()} to remove here.")
            return
        self._apply_edit(new_text, new_start, new_end)
        if value is None:
            self.flash(f"{label} removed")
        else:
            self.flash(
                f"{label} added as HTML: it is not standard Markdown and some renderers ignore it."
            )

    def prompt_color(self) -> None:
        names = "  ".join(f"{i}={name}" for i, name in enumerate(markdown_core.COLORS, start=1))
        panel = [
            "Text color (HTML <span>, not standard Markdown; GitHub and others may ignore it).",
            names,
            "Type a number, a color name, or #rrggbb.  0 removes a color.",
        ]

        def done(text: str) -> None:
            choice = text.strip().lower()
            if not choice:
                self.flash("Cancelled")
            elif choice in ("0", "none", "remove"):
                self.insert_color(None)
            elif choice.isdigit() and 1 <= int(choice) <= len(markdown_core.COLORS):
                self.insert_color(markdown_core.COLORS[int(choice) - 1])
            else:
                self.insert_color(choice)

        self._open_prompt("Color: ", done, panel=panel)

    def prompt_font_size(self) -> None:
        sizes = "  ".join(f"{size}" for size in markdown_core.FONT_SIZES)
        panel = [
            "Font size in px (HTML <span>, not standard Markdown). For portable sizes use # headings.",
            f"Common sizes: {sizes}",
            f"Type a size from {markdown_core.MIN_FONT_SIZE} to {markdown_core.MAX_FONT_SIZE}.  0 removes a size.",
        ]

        def done(text: str) -> None:
            choice = text.strip()
            if not choice:
                self.flash("Cancelled")
            elif choice in ("0", "none", "remove"):
                self.insert_font_size(None)
            else:
                self.insert_font_size(choice)

        self._open_prompt("Size (px): ", done, panel=panel)

    def show_validation(self) -> None:
        issues = self._issues()
        if not issues:
            self.panel_lines = ["No Markdown problems found. (Esc closes this panel)"]
            return
        lines = [f"{len(issues)} possible problem(s) (Esc closes this panel):"]
        for issue in issues[: self.PANEL_MAX_LINES - 2]:
            marker = "ERROR" if issue.severity == "error" else "warn "
            lines.append(f"  {marker}  {issue}")
        if len(issues) > self.PANEL_MAX_LINES - 2:
            lines.append(f"  ... and {len(issues) - (self.PANEL_MAX_LINES - 2)} more")
        self.panel_lines = lines

    def show_help(self) -> None:
        self.panel_lines = list(HELP_LINES)

    # ------------------------------------------------------------------
    # Prompt line and confirmations
    # ------------------------------------------------------------------

    def _open_prompt(
        self,
        label: str,
        callback: Callable[[str], None],
        *,
        initial: str = "",
        path_completion: bool = False,
        panel: Optional[list[str]] = None,
    ) -> None:
        self.prompt_label = label
        self._prompt_callback = callback
        self.prompt_buffer.completer = (
            self._path_completer if path_completion else DummyCompleter()
        )
        self.prompt_buffer.reset(Document(initial, len(initial)))
        self.panel_lines = list(panel or [])
        self.prompt_active = True
        self._set_suggestions([])
        self.app.layout.focus(self.prompt_window)
        self.app.invalidate()

    def _close_prompt(self) -> None:
        self.prompt_active = False
        self._prompt_callback = None
        self.panel_lines = []
        self.app.layout.focus(self.editor_window)
        self._refresh_suggestions()

    def _prompt_accept(self, buffer: Buffer) -> bool:
        callback = self._prompt_callback
        text = buffer.text
        self._close_prompt()
        if callback:
            callback(text)
        return True  # keep the text so a follow-up prompt is not wiped

    def _cancel_prompt(self) -> None:
        self._close_prompt()
        self.flash("Cancelled")

    def _confirm(
        self,
        message: str,
        on_yes: Callable[[], None],
        on_no: Optional[Callable[[], None]] = None,
    ) -> None:
        def done(text: str) -> None:
            if text.strip().lower() in ("y", "yes"):
                on_yes()
            elif on_no is not None:
                on_no()
            else:
                self.flash("Cancelled")

        self._open_prompt("Confirm (y/n): ", done, panel=message.split("\n"))

    # ------------------------------------------------------------------
    # Loading documents
    # ------------------------------------------------------------------

    def _set_document(
        self,
        text: str,
        path: Optional[Path],
        newline: str,
        mtime_ns: Optional[int],
        cursor: int = 0,
        saved_text: Optional[str] = None,
    ) -> None:
        self._cancel_autosave_timer()
        self._loading = True
        try:
            self.buffer.reset(Document(text, max(0, min(cursor, len(text)))))
        finally:
            self._loading = False

        self.path = path
        self.saved_text = text if saved_text is None else saved_text
        self.newline = newline
        self.disk_mtime_ns = mtime_ns
        self.conflict = False
        self.save_error = None
        self.mark_mode = False
        self._quit_error_once = False
        self._last_find_span = None
        self._dismissed_before = None
        self._set_suggestions([])

    def _load_path(self, path: Path, cursor: int = 0, line: Optional[int] = None) -> None:
        loaded = self.fm.read_document(path)
        if line is not None:
            cursor = offset_of(loaded.text, max(0, line - 1), 0)
        self._set_document(loaded.text, loaded.path, loaded.newline, loaded.mtime_ns, cursor)
        self._register_recent(loaded.path)

    def _register_recent(self, path: Path) -> None:
        self.fm.add_recent_file(path)
        self._recent_registered = path

    def _load_initial_document(self, initial_path: Optional[str], start_blank: bool) -> None:
        if initial_path:
            path = Path(initial_path).expanduser().resolve()
            if path.exists():
                self._load_path(path)  # errors propagate so main() can report them
                self.flash(f"Opened {path.name}")
            else:
                self._set_document("", path, "\n", None)
                self.flash(f"New file {path.name}: it is created when you start typing.")
            return

        if not start_blank:
            last, cursor = self.fm.last_session()
            if last is not None:
                try:
                    self._load_path(last, cursor)
                    self.flash(f"Reopened {last.name}")
                    return
                except (FileManagerError, OSError):
                    pass

        self._set_document("", None, "\n", None)
        self.flash("New document. Ctrl+S names and saves it; your text is kept safe meanwhile.")

    # ------------------------------------------------------------------
    # Saving
    # ------------------------------------------------------------------

    def _cancel_autosave_timer(self) -> None:
        if self._autosave_handle is not None:
            self._autosave_handle.cancel()
            self._autosave_handle = None

    def _schedule_autosave(self) -> None:
        """Restart the debounce timer: save once the user pauses typing."""
        self._cancel_autosave_timer()
        if self.conflict:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return  # not inside the running application (e.g. unit tests)
        delay = float(self.fm.config.get("autosave_debounce_seconds", 0.75))
        self._autosave_handle = loop.call_later(delay, self._autosave_fire)

    def _autosave_fire(self) -> None:
        self._autosave_handle = None
        try:
            if self.dirty:
                self.save(auto=True)
        except Exception as error:  # never let a timer callback crash the editor
            self.flash(f"Autosave problem: {error}")
        self.app.invalidate()

    def autosave_now(self) -> bool:
        """Run the autosave logic immediately (used by shutdown paths and tests)."""
        self._cancel_autosave_timer()
        return self.save(auto=True)

    def _snapshot(self) -> bool:
        try:
            self.fm.save_recovery_snapshot(
                self.buffer.text,
                str(self.path) if self.path else None,
                cursor=self.buffer.cursor_position,
                base_mtime_ns=self.disk_mtime_ns,
                newline=self.newline,
            )
            return True
        except OSError as error:
            self.flash(f"Could not write recovery copy: {error}")
            return False

    def _write_current(self, ignore_conflict: bool = False) -> str:
        """Write the open document. Returns "ok", "conflict" or "error"."""
        assert self.path is not None
        text = self.buffer.text
        expected = None if (ignore_conflict or self.disk_mtime_ns is None) else self.disk_mtime_ns
        try:
            mtime = self.fm.write_file(
                self.path, text, newline=self.newline, expected_mtime_ns=expected
            )
        except ExternalChangeError:
            self.conflict = True
            self.save_error = None
            self._snapshot()
            return "conflict"
        except OSError as error:
            self.save_error = str(error)
            self._snapshot()
            self.flash(f"Save failed: {error}. A recovery copy was kept.")
            return "error"

        self.saved_text = text
        self.disk_mtime_ns = mtime
        self.conflict = False
        self.save_error = None
        self.fm.clear_recovery_snapshot()
        if self._recent_registered != self.path:
            self._register_recent(self.path)
        return "ok"

    def save(
        self,
        *,
        force: bool = False,
        auto: bool = False,
        then: Optional[Callable[[], None]] = None,
    ) -> bool:
        """Save the document. ``then`` runs after a successful save."""
        if self.path is None:
            if auto:
                if self.dirty and self._snapshot():
                    self.flash("Draft kept safe. Press Ctrl+S to name and save it.")
                return False
            self._prompt_save_as(then)
            return False

        if auto and (self.conflict or not self.dirty):
            return False

        result = self._write_current()
        if result == "ok":
            self.flash("Auto-saved" if auto else "Saved")
            if then:
                then()
            return True

        if result == "conflict":
            if auto:
                self.flash(
                    "File changed on disk: autosave paused. Ctrl+S to overwrite, or Ctrl+O to reload."
                )
            else:
                self._confirm_overwrite_changed_file(then)
        return False

    def _confirm_overwrite_changed_file(self, then: Optional[Callable[[], None]]) -> None:
        def overwrite() -> None:
            if self._write_current(ignore_conflict=True) == "ok":
                self.flash("Saved (overwrote the changed file)")
                if then:
                    then()

        name = self.path.name if self.path else "this file"
        self._confirm(
            f"{name} was changed on disk after you opened it.\n"
            "Overwrite it with the text in the editor? (a recovery copy is kept either way)",
            overwrite,
            lambda: self.flash("Not saved. Your text is safe in the recovery copy."),
        )

    def _default_save_path(self) -> Path:
        folder = self.fm.default_folder()
        for number in range(1000):
            name = "untitled.md" if number == 0 else f"untitled-{number}.md"
            if not (folder / name).exists():
                return folder / name
        return folder / "untitled.md"

    def _prompt_save_as(self, then: Optional[Callable[[], None]] = None) -> None:
        def done(text: str) -> None:
            if not text.strip():
                self.flash("Save cancelled")
                return
            self._save_as_path(text, then)

        self._open_prompt(
            "Save as: ",
            done,
            initial=str(self._default_save_path()),
            path_completion=True,
            panel=["Type a file name or path (.md). Tab completes paths."],
        )

    def _save_as_path(self, raw: str, then: Optional[Callable[[], None]] = None) -> None:
        try:
            target = self.fm.normalize_markdown_path(raw)
        except ValueError as error:
            self.flash(str(error))
            return

        is_current = self.path is not None and target == self.path
        if target.exists() and not is_current:
            self._confirm(
                f"{pretty_path(target)} already exists.\nReplace it with this document?",
                lambda: self._finish_save_as(target, then),
            )
        else:
            self._finish_save_as(target, then)

    def _finish_save_as(self, target: Path, then: Optional[Callable[[], None]]) -> None:
        text = self.buffer.text
        try:
            mtime = self.fm.write_file(target, text, newline=self.newline if self.path else "\n")
        except OSError as error:
            self.flash(f"Save failed: {error}")
            return

        if self.path is None:
            self.newline = "\n"
        self.path = target
        self.saved_text = text
        self.disk_mtime_ns = mtime
        self.conflict = False
        self.save_error = None
        self.fm.clear_recovery_snapshot()
        self._register_recent(target)
        self.flash(f"Saved as {pretty_path(target)}")
        if then:
            then()

    def _ensure_saved(self, then: Callable[[], None]) -> None:
        """Run ``then`` once the current document is safely stored (or discarded on request)."""
        if not self.dirty:
            then()
            return
        if self.path is not None:
            self.save(force=True, then=then)
            return

        def done(text: str) -> None:
            choice = text.strip()
            if choice.lower() == "discard":
                self.fm.clear_recovery_snapshot()
                self.saved_text = self.buffer.text
                then()
            elif not choice:
                self.flash("Cancelled: the document is still open.")
            else:
                self._save_as_path(choice, then)

        self._open_prompt(
            "Save untitled document as: ",
            done,
            initial=str(self._default_save_path()),
            path_completion=True,
            panel=[
                "This document has not been saved yet.",
                "Enter a file name to save it, type 'discard' to drop it, or press Esc to keep editing.",
            ],
        )

    # ------------------------------------------------------------------
    # New / open / rename
    # ------------------------------------------------------------------

    def new_document(self) -> None:
        self._ensure_saved(self._new_document_now)

    def _new_document_now(self) -> None:
        self._set_document("", None, "\n", None)
        self.flash("New document. Ctrl+S names and saves it.")

    def open_document(self) -> None:
        self._ensure_saved(self._show_open_dialog)

    def _show_open_dialog(self) -> None:
        self._open_candidates = [Path(p) for p in self.fm.get_recent_files()]
        panel = ["Recent files:"]
        if self._open_candidates:
            panel += [f"  {i}. {pretty_path(p)}" for i, p in enumerate(self._open_candidates, 1)]
        else:
            panel.append("  (none yet)")
        panel.append("Type a number, a file path, or a folder to list its Markdown files.")
        self._open_prompt("Open: ", self._open_choice, path_completion=True, panel=panel)

    def _open_choice(self, text: str) -> None:
        choice = text.strip().strip("\"'")
        if not choice:
            self.flash("Cancelled")
            return

        if choice.isdigit():
            index = int(choice) - 1
            if 0 <= index < len(self._open_candidates):
                self._open_path(self._open_candidates[index])
            else:
                self.flash(f"There is no entry number {choice}.")
            return

        path = Path(choice).expanduser()
        if path.is_dir():
            files = self.fm.find_markdown_files(path)[:30]
            if not files:
                self.flash(f"No Markdown files in {pretty_path(path.resolve())}")
                return
            self._open_candidates = files
            root = path.resolve()
            panel = [f"Markdown files in {pretty_path(root)}:"]
            for number, file in enumerate(files, 1):
                try:
                    shown = str(file.relative_to(root))
                except ValueError:
                    shown = str(file)
                panel.append(f"  {number}. {shown}")
            self._open_prompt("Open: ", self._open_choice, path_completion=True, panel=panel)
        elif path.is_file():
            self._open_path(path)
        else:
            try:
                target = self.fm.normalize_markdown_path(choice)
            except ValueError as error:
                self.flash(str(error))
                return
            if target.exists():
                self._open_path(target)
                return
            self._confirm(
                f"{pretty_path(target)} does not exist.\nCreate it as a new document?",
                lambda: self._start_new_file(target),
            )

    def _start_new_file(self, target: Path) -> None:
        self._set_document("", target, "\n", None)
        self.flash(f"New file {target.name}: it is created when you start typing.")

    def _open_path(self, path: Path, line: Optional[int] = None) -> None:
        try:
            self._load_path(path, line=line)
        except (FileManagerError, OSError) as error:
            self.flash(f"Could not open {path.name}: {error}")
            return
        self.flash(f"Opened {path.name}")

    def rename_document(self) -> None:
        if self.path is None:
            self.flash("Name the document first.")
            self._prompt_save_as()
            return

        def after_save() -> None:
            def done(text: str) -> None:
                if not text.strip():
                    self.flash("Rename cancelled")
                    return
                try:
                    target = self.fm.normalize_markdown_path(text)
                except ValueError as error:
                    self.flash(str(error))
                    return
                if target == self.path:
                    self.flash("Same name: nothing to do.")
                    return
                try:
                    assert self.path is not None
                    self.path = self.fm.rename_file(self.path, target)
                except (OSError, FileManagerError) as error:
                    self.flash(f"Rename failed: {error}")
                    return
                self.disk_mtime_ns = self.fm.get_mtime_ns(self.path)
                self._recent_registered = self.path
                self.flash(f"Renamed to {pretty_path(self.path)}")

            self._open_prompt(
                "Rename to: ",
                done,
                initial=str(self.path),
                path_completion=True,
                panel=["Enter the new file name or path (.md)."],
            )

        self._ensure_saved(after_save)

    # ------------------------------------------------------------------
    # Search
    # ------------------------------------------------------------------

    def search_in_document(self) -> None:
        def done(text: str) -> None:
            if not text:
                self.flash("Search cancelled")
                return
            self._find_query = text
            self._last_find_span = None
            self.find_next(1)

        self._open_prompt("Find: ", done, initial=self._find_query)

    def _select_span(self, start: int, end: int) -> None:
        text = self.buffer.text
        self._programmatic = True
        try:
            self.buffer.cursor_position = max(0, min(end - 1, len(text)))
            self.buffer.selection_state = SelectionState(
                original_cursor_position=start, type=SelectionType.CHARACTERS
            )
        finally:
            self._programmatic = False
        self.mark_mode = False
        self._refresh_suggestions()

    def find_next(self, direction: int = 1) -> None:
        if not self._find_query:
            self.flash("Nothing to find yet: press Ctrl+F.")
            return
        matches = markdown_core.find_offsets(self.buffer.text, self._find_query)
        if not matches:
            self.flash(f"No matches for '{self._find_query}'")
            return

        current = self.selection_span()
        if current is not None and current == self._last_find_span and current in matches:
            index = (matches.index(current) + direction) % len(matches)
        else:
            cursor = self.buffer.cursor_position
            if direction >= 0:
                index = next((i for i, m in enumerate(matches) if m[0] >= cursor), 0)
            else:
                index = next(
                    (i for i in range(len(matches) - 1, -1, -1) if matches[i][1] <= cursor),
                    len(matches) - 1,
                )

        start, end = matches[index]
        self._select_span(start, end)
        self._last_find_span = (start, end)
        self.flash(f"Match {index + 1} of {len(matches)}: '{self._find_query}'")

    def search_folder(self) -> None:
        folder = self.path.parent if self.path else Path.cwd()

        def done(text: str) -> None:
            query = text.strip()
            if not query:
                self.flash("Search cancelled")
                return
            results = self.fm.search_folder(folder, query)
            hits = [
                (path, number, line)
                for path in sorted(results)
                for number, line in results[path]
            ]
            if not hits:
                self.flash(f"No matches for '{query}' in {pretty_path(folder)}")
                return
            self._search_hits = hits
            shown = hits[: self.PANEL_MAX_LINES - 3]
            panel = [f"{len(hits)} match(es) for '{query}' under {pretty_path(folder)}:"]
            for number, (path, line_no, line) in enumerate(shown, 1):
                name = os.path.relpath(path, folder)
                panel.append(f"  {number}. {name}:{line_no}  {line.strip()[:60]}")
            if len(hits) > len(shown):
                panel.append(f"  (showing the first {len(shown)}; refine the search to see others)")
            self._open_prompt("Open result #: ", self._open_search_hit, panel=panel)

        self._open_prompt(
            "Search folder: ",
            done,
            panel=[f"Searches every .md file under {pretty_path(folder)}"],
        )

    def _open_search_hit(self, text: str) -> None:
        choice = text.strip()
        if not choice:
            self.flash("Cancelled")
            return
        if not choice.isdigit() or not 1 <= int(choice) <= len(self._search_hits):
            self.flash(f"Enter a result number from 1 to {len(self._search_hits)}.")
            return
        path, line, _ = self._search_hits[int(choice) - 1]
        self._ensure_saved(lambda: self._open_path(Path(path), line))

    # ------------------------------------------------------------------
    # Quit and recovery
    # ------------------------------------------------------------------

    def _exit(self) -> None:
        self._cancel_autosave_timer()
        self.exited = True
        try:
            self.fm.remember_session(self.path, self.buffer.cursor_position)
        except OSError:
            pass
        try:
            self.app.exit()
        except Exception:
            pass  # the application is not running (unit tests)

    def request_quit(self) -> None:
        """Save, then exit. Content is never dropped silently."""
        self._cancel_autosave_timer()

        if not self.dirty:
            if self.path is None:
                self.fm.clear_recovery_snapshot()
            self._exit()
            return

        if self.path is None:
            self._quit_untitled()
            return

        result = self._write_current()
        if result == "ok":
            self._exit()
        elif result == "conflict":
            def overwrite() -> None:
                if self._write_current(ignore_conflict=True) == "ok":
                    self._exit()

            def keep_copy() -> None:
                self._snapshot()
                self.exit_message = (
                    f"{self.path.name} changed on disk, so it was left untouched. "
                    f"Your text is in {self.fm.recovery_path}"
                )
                self._exit()

            self._confirm(
                "The file changed on disk after you opened it.\n"
                "y = overwrite it with your text and quit;  n = leave it, keep a recovery copy and quit.",
                overwrite,
                keep_copy,
            )
        else:
            if self._quit_error_once:
                self.exit_message = (
                    f"Could not save {self.path.name}. Your text is in {self.fm.recovery_path}"
                )
                self._exit()
            else:
                self._quit_error_once = True
                self.flash("Could not save. Fix the problem, or press Ctrl+Q again to quit with a recovery copy.")

    def _quit_untitled(self) -> None:
        if not self.buffer.text.strip():
            self.fm.clear_recovery_snapshot()
            self._exit()
            return

        def done(text: str) -> None:
            choice = text.strip()
            if choice:
                self._save_as_path(choice, self._exit)
            else:
                if self._snapshot():
                    self.exit_message = (
                        f"Unsaved text kept in {self.fm.recovery_path}; "
                        "SI MarkdownMate will offer to restore it next time."
                    )
                    self._exit()

        self._open_prompt(
            "Save as (Enter = keep a recovery copy): ",
            done,
            initial=str(self._default_save_path()),
            path_completion=True,
            panel=["This document has no name yet. Enter a file name to save it before quitting."],
        )

    def check_recovery(self) -> None:
        """Offer to restore text left behind by a crash or an unnamed document."""
        snapshot = self.fm.load_recovery_snapshot()
        if snapshot is None:
            return
        if snapshot["text"] == self.buffer.text and snapshot.get("original_path") == (
            str(self.path) if self.path else None
        ):
            self.fm.clear_recovery_snapshot()
            return

        origin = snapshot.get("original_path") or "an unnamed document"
        when = time.strftime("%Y-%m-%d %H:%M", time.localtime(snapshot.get("saved_at") or 0))
        self._confirm(
            f"Unsaved text from an earlier session was found ({origin}, {when}).\n"
            "Restore it into the editor?  (n keeps a copy as recovery.previous.json)",
            lambda: self._restore_snapshot(snapshot),
            self._archive_recovery,
        )

    def _archive_recovery(self) -> None:
        self.fm.archive_recovery_snapshot()
        self.flash("Recovery copy set aside as recovery.previous.json")

    def _restore_snapshot(self, snapshot: dict) -> None:
        text = snapshot["text"]
        newline = snapshot.get("newline") if snapshot.get("newline") in ("\n", "\r\n") else "\n"
        original = snapshot.get("original_path")
        path = Path(original) if original else None

        disk_text = ""
        mtime: Optional[int] = None
        if path is not None and path.is_file():
            try:
                loaded = self.fm.read_document(path)
                disk_text, mtime = loaded.text, loaded.mtime_ns
            except (FileManagerError, OSError):
                disk_text, mtime = "", None

        base = snapshot.get("base_mtime_ns")
        self._set_document(
            text, path, newline, base if base is not None else mtime,
            cursor=snapshot.get("cursor", 0), saved_text=disk_text,
        )
        if path is not None and base is not None and mtime is not None and base != mtime:
            self.conflict = True
            self.flash("Restored. The file changed on disk meanwhile: Ctrl+S asks before overwriting.")
        else:
            self.flash("Restored your unsaved text.")
            self._schedule_autosave()

    def _emergency_save(self) -> None:
        """Last-chance save when the application stops for any reason."""
        try:
            if self.dirty:
                if self.path is not None and not self.conflict:
                    if self._write_current() == "ok":
                        return
                self._snapshot()
                self.exit_message = self.exit_message or (
                    f"Unsaved text kept in {self.fm.recovery_path}"
                )
        except Exception:
            pass

    def run(self) -> None:
        try:
            self.check_recovery()
            self.app.run()
        finally:
            self._cancel_autosave_timer()
            self._emergency_save()
            try:
                self.fm.remember_session(self.path, self.buffer.cursor_position)
            except Exception:
                pass
        if self.exit_message:
            print(self.exit_message)
