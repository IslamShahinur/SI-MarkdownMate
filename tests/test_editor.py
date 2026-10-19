"""Editor behaviour tests.

They drive the editor's methods directly (no real terminal needed) and are
skipped automatically when prompt_toolkit is not installed.
"""

import asyncio
import contextlib
import os
import tempfile
import unittest
from pathlib import Path

try:
    from prompt_toolkit.document import Document
    from prompt_toolkit.input import create_pipe_input
    from prompt_toolkit.output import DummyOutput

    HAVE_PTK = True
except ImportError:  # pragma: no cover
    HAVE_PTK = False

if HAVE_PTK:
    import editor as editor_module
    from editor import MarkdownEditor, inline_fragments, line_col, move_cursor, offset_of
    from file_manager import FileManager


@unittest.skipUnless(HAVE_PTK, "prompt_toolkit is not installed")
class EditorTestCase(unittest.TestCase):
    def setUp(self):
        self._stack = contextlib.ExitStack()
        self.addCleanup(self._stack.close)
        tmp = self._stack.enter_context(tempfile.TemporaryDirectory())
        self.root = Path(tmp).resolve()
        self.docs = self.root / "docs"
        self.docs.mkdir()
        self.fm = FileManager(self.root / "config")
        self.pipe = self._stack.enter_context(create_pipe_input())

    def make(self, path=None, start_blank=True):
        return MarkdownEditor(
            self.fm,
            str(path) if path else None,
            start_blank=start_blank,
            input=self.pipe,
            output=DummyOutput(),
        )

    def type(self, editor, text):
        editor.buffer.insert_text(text)

    def answer(self, editor, text):
        self.assertTrue(editor.prompt_active, "expected a prompt to be open")
        editor.prompt_buffer.text = text
        editor._prompt_accept(editor.prompt_buffer)

    def write(self, name, text="", raw=None):
        path = self.docs / name
        if raw is not None:
            path.write_bytes(raw)
        else:
            path.write_text(text, encoding="utf-8")
        return path

    def touch_ahead(self, path):
        stat = path.stat()
        bump = stat.st_mtime_ns + 5_000_000_000
        os.utime(path, ns=(bump, bump))


class TestHelpers(EditorTestCase):
    def test_line_col_and_offset_roundtrip(self):
        text = "ab\n\ncdef"
        for pos in range(len(text) + 1):
            row, col = line_col(text, pos)
            self.assertEqual(offset_of(text, row, col), pos)

    def test_move_cursor(self):
        text = "abc\nd\nefgh"
        self.assertEqual(move_cursor(text, 2, "down"), 5)      # clamped to short line
        self.assertEqual(move_cursor(text, 5, "down"), 7)
        self.assertEqual(move_cursor(text, 7, "up"), 5)
        self.assertEqual(move_cursor(text, 0, "up"), 0)
        self.assertEqual(move_cursor(text, 9, "down"), len(text))
        self.assertEqual(move_cursor(text, 2, "home"), 0)
        self.assertEqual(move_cursor(text, 1, "end"), 3)
        self.assertEqual(move_cursor(text, 0, "left"), 0)
        self.assertEqual(move_cursor(text, len(text), "right"), len(text))

    def test_inline_fragments_always_rejoin(self):
        for line in ("", "plain", "**b** and *i* and `c`", "a * b * c", '<span style="color: red;">x</span>',
                     "**unclosed", "`", "***both***", "- [ ] task"):
            self.assertEqual("".join(t for _, t in inline_fragments(line)), line)

    def test_lexer_lines(self):
        lexer = editor_module.MarkdownLexer()
        doc = Document("# Title\n- item **x**\n```\ncode\n```\n> q\n---\nplain", 0)
        get_line = lexer.lex_document(doc)
        for number, line in enumerate(doc.lines):
            self.assertEqual("".join(t for _, t in get_line(number)), line)
        self.assertEqual(get_line(0)[0][0], "class:md-heading")
        self.assertEqual(get_line(3)[0][0], "class:md-fence")
        self.assertEqual(get_line(99), [])


class TestDocumentsAndSaving(EditorTestCase):
    def test_blank_start_is_clean(self):
        e = self.make()
        self.assertFalse(e.dirty)
        self.assertIsNone(e.path)

    def test_open_existing_and_save_roundtrip(self):
        path = self.write("a.md", "hello\n")
        e = self.make(path)
        self.assertEqual((e.buffer.text, e.path, e.dirty), ("hello\n", path, False))
        e.buffer.cursor_position = 5
        self.type(e, " world")
        self.assertTrue(e.dirty)
        self.assertTrue(e.autosave_now())
        self.assertEqual(path.read_text(), "hello world\n")
        self.assertFalse(e.dirty)

    def test_new_path_is_created_on_first_save_only(self):
        path = self.docs / "fresh.md"
        e = self.make(path)
        self.assertFalse(path.exists())
        self.type(e, "# Hi")
        e.autosave_now()
        self.assertEqual(path.read_text(), "# Hi")

    def test_crlf_file_keeps_its_line_endings(self):
        path = self.write("w.md", raw=b"a\r\nb\r\n")
        e = self.make(path)
        self.assertEqual(e.buffer.text, "a\nb\n")
        e.buffer.cursor_position = len(e.buffer.text)
        self.type(e, "c")
        e.autosave_now()
        self.assertEqual(path.read_bytes(), b"a\r\nb\r\nc")

    def test_undo_returns_to_clean_state(self):
        e = self.make(self.write("a.md", "x"))
        e.buffer.cursor_position = 1
        self.type(e, "y")
        self.assertTrue(e.dirty)
        e.buffer.undo()
        self.assertFalse(e.dirty)

    def test_untitled_autosave_keeps_recovery_copy_not_a_file(self):
        e = self.make()
        self.type(e, "draft")
        e.autosave_now()
        self.assertEqual(self.fm.load_recovery_snapshot()["text"], "draft")
        self.assertEqual(list(self.docs.iterdir()), [])

    def test_debounce_saves_the_last_keystroke_once(self):
        path = self.write("a.md", "")
        self.fm.config["autosave_debounce_seconds"] = 0.15
        e = self.make(path)
        writes = []
        original = self.fm.write_file

        def counting(*args, **kwargs):
            writes.append(args[1])
            return original(*args, **kwargs)

        self.fm.write_file = counting

        async def scenario():
            for ch in "abc":
                self.type(e, ch)
                await asyncio.sleep(0.05)       # keeps restarting the timer
            self.assertEqual(path.read_text(), "")   # nothing written mid-burst
            await asyncio.sleep(0.4)

        asyncio.run(scenario())
        self.assertEqual(path.read_text(), "abc")
        self.assertEqual(writes, ["abc"])
        self.assertFalse(e.dirty)

    def test_external_change_pauses_autosave_and_protects_file(self):
        path = self.write("a.md", "mine")
        e = self.make(path)
        path.write_text("theirs")
        self.touch_ahead(path)
        e.buffer.cursor_position = 4
        self.type(e, "!")
        self.assertFalse(e.autosave_now())
        self.assertTrue(e.conflict)
        self.assertEqual(path.read_text(), "theirs")
        self.assertEqual(self.fm.load_recovery_snapshot()["text"], "mine!")

        e.save(force=True)                      # Ctrl+S asks first
        self.assertTrue(e.prompt_active)
        self.answer(e, "n")
        self.assertEqual(path.read_text(), "theirs")
        e.save(force=True)
        self.answer(e, "y")
        self.assertEqual(path.read_text(), "mine!")
        self.assertFalse(e.conflict)
        self.assertIsNone(self.fm.load_recovery_snapshot())

    def test_save_failure_keeps_text_and_recovery_copy(self):
        path = self.write("a.md", "x")
        e = self.make(path)
        self.type(e, "y")

        def boom(*a, **k):
            raise OSError("disk full")

        original = self.fm.write_file
        self.fm.write_file = boom
        self.assertFalse(e.autosave_now())
        self.assertEqual(e.save_error, "disk full")
        self.assertTrue(e.dirty)
        self.assertEqual(self.fm.load_recovery_snapshot()["text"], "yx")
        self.fm.write_file = original
        self.assertTrue(e.autosave_now())
        self.assertIsNone(e.save_error)

    def test_save_as_for_untitled(self):
        e = self.make()
        self.type(e, "text")
        e.save(force=True)
        self.assertTrue(e.prompt_active)
        target = self.docs / "named"
        self.answer(e, str(target))
        self.assertEqual((self.docs / "named.md").read_text(), "text")
        self.assertEqual(e.path, self.docs / "named.md")
        self.assertFalse(e.dirty)
        self.assertIsNone(self.fm.load_recovery_snapshot())
        self.assertEqual(self.fm.get_recent_files(), [str(self.docs / "named.md")])

    def test_save_as_rejects_bad_extension_and_asks_before_replacing(self):
        existing = self.write("keep.md", "precious")
        e = self.make()
        self.type(e, "new")
        e.save(force=True)
        self.answer(e, str(self.docs / "x.txt"))
        self.assertIsNone(e.path)
        e.save(force=True)
        self.answer(e, str(existing))
        self.assertTrue(e.prompt_active)         # confirmation
        self.answer(e, "n")
        self.assertEqual(existing.read_text(), "precious")
        e.save(force=True)
        self.answer(e, str(existing))
        self.answer(e, "y")
        self.assertEqual(existing.read_text(), "new")

    def test_escape_cancels_prompt(self):
        e = self.make()
        self.type(e, "t")
        e.save(force=True)
        e.handle_escape()
        self.assertFalse(e.prompt_active)
        self.assertEqual(list(self.docs.iterdir()), [])


class TestNewOpenRename(EditorTestCase):
    def test_new_saves_current_first(self):
        path = self.write("a.md", "one")
        e = self.make(path)
        self.type(e, "!")
        e.new_document()
        self.assertEqual(path.read_text(), "!one")
        self.assertEqual((e.buffer.text, e.path), ("", None))

    def test_new_with_unsaved_untitled_requires_decision(self):
        e = self.make()
        self.type(e, "keep me")
        e.new_document()
        self.assertTrue(e.prompt_active)
        e.handle_escape()
        self.assertEqual(e.buffer.text, "keep me")
        e.new_document()
        self.answer(e, "discard")
        self.assertEqual(e.buffer.text, "")

    def test_open_by_number_path_and_folder(self):
        a = self.write("a.md", "AAA")
        b = self.write("b.md", "BBB")
        self.fm.add_recent_file(a)
        self.fm.add_recent_file(b)
        e = self.make()
        e.open_document()
        self.assertTrue(any("1." in line for line in e.panel_lines))
        self.answer(e, "2")                       # recent #2 is a.md
        self.assertEqual(e.buffer.text, "AAA")
        e.open_document()
        self.answer(e, str(b))
        self.assertEqual(e.buffer.text, "BBB")
        e.open_document()
        self.answer(e, str(self.docs))            # folder listing
        self.assertTrue(e.prompt_active)
        self.answer(e, "1")
        self.assertEqual(e.buffer.text, "AAA")

    def test_open_missing_offers_to_create(self):
        e = self.make()
        e.open_document()
        self.answer(e, str(self.docs / "later"))
        self.answer(e, "y")
        self.assertEqual(e.path, self.docs / "later.md")
        self.assertFalse((self.docs / "later.md").exists())

    def test_open_bad_files_report_instead_of_crashing(self):
        bad = self.write("bad.md", raw=b"\xff\xfe\x00")
        e = self.make()
        e.open_document()
        self.answer(e, str(bad))
        self.assertIn("Could not open", e.status_message)
        self.assertIsNone(e.path)

    def test_rename(self):
        path = self.write("a.md", "x")
        e = self.make(path)
        e.rename_document()
        self.answer(e, str(self.docs / "b"))
        self.assertEqual(e.path, self.docs / "b.md")
        self.assertTrue((self.docs / "b.md").exists() and not path.exists())
        self.type(e, "y")
        self.assertTrue(e.autosave_now())          # no false conflict after rename
        self.assertEqual((self.docs / "b.md").read_text(), "yx")

    def test_rename_flushes_unsaved_text_first(self):
        path = self.write("a.md", "x")
        e = self.make(path)
        self.type(e, "y")
        e.rename_document()
        self.answer(e, str(self.docs / "z.md"))
        self.assertEqual((self.docs / "z.md").read_text(), "yx")

    def test_session_reopens_last_file(self):
        path = self.write("a.md", "hello")
        e = self.make(path)
        e.buffer.cursor_position = 3
        e.request_quit()
        again = self.make(start_blank=False)
        self.assertEqual((again.path, again.buffer.cursor_position), (path, 3))
        self.assertIsNone(self.make(start_blank=True).path)


class TestQuitAndRecovery(EditorTestCase):
    def test_quit_saves_dirty_file(self):
        path = self.write("a.md", "x")
        e = self.make(path)
        self.type(e, "y")
        e.request_quit()
        self.assertTrue(e.exited)
        self.assertEqual(path.read_text(), "yx")

    def test_quit_clean_and_blank_untitled(self):
        e = self.make()
        e.request_quit()
        self.assertTrue(e.exited)

    def test_quit_untitled_saves_when_named(self):
        e = self.make()
        self.type(e, "words")
        e.request_quit()
        self.assertFalse(e.exited)
        self.answer(e, str(self.docs / "out.md"))
        self.assertTrue(e.exited)
        self.assertEqual((self.docs / "out.md").read_text(), "words")

    def test_quit_untitled_blank_answer_keeps_recovery_copy(self):
        e = self.make()
        self.type(e, "words")
        e.request_quit()
        self.answer(e, "")
        self.assertTrue(e.exited)
        self.assertIn("recovery", e.exit_message)
        self.assertEqual(self.fm.load_recovery_snapshot()["text"], "words")

    def test_quit_with_conflict_choices(self):
        path = self.write("a.md", "mine")
        e = self.make(path)
        path.write_text("theirs"); self.touch_ahead(path)
        self.type(e, "!")
        e.request_quit()
        self.assertTrue(e.prompt_active and not e.exited)
        self.answer(e, "n")
        self.assertTrue(e.exited)
        self.assertEqual(path.read_text(), "theirs")
        self.assertEqual(self.fm.load_recovery_snapshot()["text"], "!mine")

        e2 = self.make(path)
        path.write_text("theirs2"); self.touch_ahead(path)
        self.type(e2, "?")
        e2.request_quit()
        self.answer(e2, "y")
        self.assertTrue(e2.exited)
        self.assertEqual(path.read_text(), "?theirs")

    def test_quit_survives_one_failed_save(self):
        path = self.write("a.md", "x")
        e = self.make(path)
        self.type(e, "y")
        original = self.fm.write_file
        self.fm.write_file = lambda *a, **k: (_ for _ in ()).throw(OSError("nope"))
        e.request_quit()
        self.assertFalse(e.exited)               # stays open, text still safe
        e.request_quit()
        self.assertTrue(e.exited)
        self.assertEqual(self.fm.load_recovery_snapshot()["text"], "yx")
        self.fm.write_file = original

    def test_recovery_restore_untitled(self):
        self.fm.save_recovery_snapshot("lost text", None, cursor=4)
        e = self.make()
        e.check_recovery()
        self.assertTrue(e.prompt_active)
        self.answer(e, "y")
        self.assertEqual((e.buffer.text, e.path, e.dirty), ("lost text", None, True))
        self.assertEqual(e.buffer.cursor_position, 4)

    def test_recovery_restore_into_named_file_marks_dirty(self):
        path = self.write("a.md", "old")
        mtime = path.stat().st_mtime_ns
        self.fm.save_recovery_snapshot("newer", str(path), base_mtime_ns=mtime)
        e = self.make()
        e.check_recovery()
        self.answer(e, "yes")
        self.assertEqual((e.buffer.text, e.path, e.dirty, e.conflict), ("newer", path, True, False))
        e.autosave_now()
        self.assertEqual(path.read_text(), "newer")

    def test_recovery_restore_detects_changed_file(self):
        path = self.write("a.md", "old")
        self.fm.save_recovery_snapshot("newer", str(path), base_mtime_ns=path.stat().st_mtime_ns - 1)
        e = self.make()
        e.check_recovery()
        self.answer(e, "y")
        self.assertTrue(e.conflict)
        self.assertFalse(e.autosave_now())
        self.assertEqual(path.read_text(), "old")

    def test_recovery_decline_archives(self):
        self.fm.save_recovery_snapshot("lost", None)
        e = self.make()
        e.check_recovery()
        self.answer(e, "n")
        self.assertIsNone(self.fm.load_recovery_snapshot())
        self.assertTrue(self.fm.previous_recovery_path.exists())
        self.assertEqual(e.buffer.text, "")

    def test_identical_snapshot_is_dropped_silently(self):
        path = self.write("a.md", "same")
        e = self.make(path)
        self.fm.save_recovery_snapshot("same", str(path))
        e.check_recovery()
        self.assertFalse(e.prompt_active)
        self.assertIsNone(self.fm.load_recovery_snapshot())

    def test_emergency_save(self):
        path = self.write("a.md", "x")
        e = self.make(path)
        self.type(e, "y")
        e._emergency_save()
        self.assertEqual(path.read_text(), "yx")
        e2 = self.make()
        self.type(e2, "orphan")
        e2._emergency_save()
        self.assertEqual(self.fm.load_recovery_snapshot()["text"], "orphan")


class TestSuggestionsInEditor(EditorTestCase):
    def test_accept_heading_template(self):
        e = self.make()
        self.type(e, "#")
        self.assertTrue(e.menu_visible)
        e.accept_suggestion()
        self.assertEqual((e.buffer.text, e.buffer.cursor_position), ("# ", 2))
        self.assertFalse(e.menu_visible)

    def test_accept_replaces_only_the_trigger(self):
        e = self.make()
        self.type(e, "Intro line\n##")
        e.move_suggestion(1)
        e.accept_suggestion()
        self.assertTrue(e.buffer.text.startswith("Intro line\n#"))

    def test_code_fence_cursor_lands_inside(self):
        e = self.make()
        self.type(e, "```")
        e.accept_suggestion()
        text, cursor = e.buffer.text, e.buffer.cursor_position
        self.assertEqual(text[cursor:], "\n```")
        self.assertTrue(text.startswith("```python\n"))

    def test_link_and_bold_cursor_positions(self):
        e = self.make()
        self.type(e, "see [")
        e.accept_suggestion()
        self.assertEqual((e.buffer.text, e.buffer.cursor_position), ("see [](url)", 5))
        e2 = self.make()
        self.type(e2, "a **")
        e2.accept_suggestion()
        self.assertEqual((e2.buffer.text, e2.buffer.cursor_position), ("a ****", 4))

    def test_dismiss_then_reappear_after_typing(self):
        e = self.make()
        self.type(e, "-")
        self.assertTrue(e.menu_visible)
        e.handle_escape()
        self.assertFalse(e.menu_visible)
        self.type(e, "-")                           # "--" -> divider
        self.assertTrue(e.menu_visible)

    def test_navigation_wraps_and_choice_is_used(self):
        e = self.make()
        self.type(e, "-")
        e.move_suggestion(1)
        e.accept_suggestion()
        self.assertEqual(e.buffer.text, "- [ ] ")
        e2 = self.make()
        self.type(e2, "-")
        e2.move_suggestion(-1)
        self.assertEqual(e2.suggestion_index, len(e2.suggestions) - 1)

    def test_menu_never_appears_for_prose_or_modifies_text(self):
        e = self.make()
        for chunk in ("Hello ", "world, # not a heading", "\n**bold**"):
            self.type(e, chunk)
            self.assertFalse(e.menu_visible)
        e.accept_suggestion()                       # no-op without a menu
        self.assertEqual(e.buffer.text, "Hello world, # not a heading\n**bold**")

    def test_dismissing_does_not_touch_text_or_disk(self):
        path = self.write("a.md", "")
        e = self.make(path)
        self.type(e, "#")
        e.handle_escape()
        self.assertEqual(e.buffer.text, "#")
        e.move_suggestion(1)
        self.assertEqual(e.buffer.text, "#")

    def test_slash_menu(self):
        e = self.make()
        self.type(e, "/div")
        self.assertEqual([s.label for s in e.suggestions], ["Divider"])
        e.accept_suggestion()
        self.assertTrue(e.buffer.text.startswith("---"))

    def test_no_menu_inside_code_block(self):
        e = self.make()
        self.type(e, "```\n#")
        self.assertFalse(e.menu_visible)

    def test_menu_hidden_while_prompt_open(self):
        e = self.make()
        self.type(e, "#")
        e.search_in_document()
        self.assertFalse(e.menu_visible)
        e.handle_escape()
        self.assertTrue(e.menu_visible)


class TestFormatting(EditorTestCase):
    def test_bold_selection_and_repeat_toggle(self):
        e = self.make()
        self.type(e, "hello world")
        e._select_span(0, 5)
        e.toggle_format("**")
        self.assertEqual(e.buffer.text, "**hello** world")
        self.assertEqual(e.selection_span(), (2, 7))   # selection preserved
        e.toggle_format("*")
        self.assertEqual(e.buffer.text, "***hello*** world")
        e.toggle_format("**")
        e.toggle_format("*")
        self.assertEqual(e.buffer.text, "hello world")

    def test_bold_without_selection_uses_word(self):
        e = self.make()
        self.type(e, "one two")
        e.buffer.cursor_position = 5
        e.toggle_format("**")
        self.assertEqual(e.buffer.text, "one **two**")

    def test_failed_format_keeps_content(self):
        e = self.make()
        self.type(e, "**")
        e._select_span(0, 2)
        e.toggle_format("*")
        self.assertEqual(e.buffer.text, "**")

    def test_formatting_is_one_undo_step(self):
        e = self.make()
        self.type(e, "abc")
        e._select_span(0, 3)
        e.toggle_format("**")
        e.buffer.undo()
        self.assertEqual(e.buffer.text, "abc")

    def test_divider(self):
        e = self.make()
        self.type(e, "Title")
        e.insert_divider()
        self.assertEqual(e.buffer.text, "Title\n\n---\n\n")
        self.assertEqual(e.buffer.cursor_position, len(e.buffer.text))

    def test_color_and_size_flow_with_prompts(self):
        e = self.make()
        self.type(e, "word")
        e._select_span(0, 4)
        e.prompt_color()
        self.assertIn("not standard Markdown", e.panel_lines[0])
        self.answer(e, "1")
        self.assertEqual(e.buffer.text, '<span style="color: red;">word</span>')
        self.assertIn("HTML", e.status_message)
        e.prompt_color()
        self.answer(e, "blue")
        self.assertIn("color: blue", e.buffer.text)
        e.prompt_color()
        self.answer(e, "0")
        self.assertEqual(e.buffer.text, "word")

        e._select_span(0, 4)
        e.prompt_font_size()
        self.answer(e, "24")
        self.assertEqual(e.buffer.text, '<span style="font-size: 24px;">word</span>')

    def test_invalid_color_and_size_leave_text_alone(self):
        e = self.make()
        self.type(e, "word")
        e._select_span(0, 4)
        e.insert_color("not-a-color;}")
        self.assertEqual(e.buffer.text, "word")
        self.assertIn("Unknown colour", e.status_message)
        e.insert_font_size("999")
        self.assertEqual(e.buffer.text, "word")
        e.insert_color(None)
        self.assertIn("No color to remove", e.status_message)

    def test_indent_and_outdent(self):
        e = self.make()
        e.indent()
        self.type(e, "x")
        self.assertEqual(e.buffer.text, "    x")
        e.outdent()
        self.assertEqual(e.buffer.text, "x")
        e.outdent()
        self.assertEqual(e.buffer.text, "x")


class TestSelectionAndSearch(EditorTestCase):
    def test_shift_selection_and_clear_on_plain_move(self):
        e = self.make()
        self.type(e, "abcdef")
        e.buffer.cursor_position = 1
        e.extend_selection("right")
        e.extend_selection("right")
        self.assertEqual(e.selection_span(), (1, 4))    # inclusive of char under cursor
        e.buffer.cursor_position = 0                    # a plain move
        self.assertIsNone(e.buffer.selection_state)

    def test_mark_mode_keeps_selection_while_moving(self):
        e = self.make()
        self.type(e, "abcdef")
        e.buffer.cursor_position = 1
        e.toggle_mark()
        e.buffer.cursor_position = 3
        self.assertIsNotNone(e.buffer.selection_state)
        e.handle_escape()
        self.assertIsNone(e.buffer.selection_state)

    def test_delete_selection(self):
        e = self.make()
        self.type(e, "abcdef")
        e._select_span(1, 4)
        e.delete_selection()
        self.assertEqual((e.buffer.text, e.buffer.cursor_position), ("aef", 1))

    def test_find_next_and_previous_wrap(self):
        e = self.make()
        self.type(e, "cat dog cat bird CAT")
        e.buffer.cursor_position = 0
        e._find_query = "cat"
        e.find_next(1)
        self.assertEqual(e.selection_span(), (0, 3))
        e.find_next(1)
        self.assertEqual(e.selection_span(), (8, 11))
        e.find_next(1)
        e.find_next(1)
        self.assertEqual(e.selection_span(), (0, 3))     # wrapped
        e.find_next(-1)
        self.assertEqual(e.selection_span(), (17, 20))
        self.assertIn("3 of 3", e.status_message)

    def test_find_prompt_and_no_match(self):
        e = self.make()
        self.type(e, "alpha beta")
        e.buffer.cursor_position = 0
        e.search_in_document()
        self.answer(e, "beta")
        self.assertEqual(e.selection_span(), (6, 10))
        e.search_in_document()
        self.answer(e, "zzz")
        self.assertIn("No matches", e.status_message)

    def test_bold_a_search_result(self):
        e = self.make()
        self.type(e, "say hello now")
        e._find_query = "hello"
        e.buffer.cursor_position = 0
        e.find_next(1)
        e.toggle_format("**")
        self.assertEqual(e.buffer.text, "say **hello** now")

    def test_folder_search_opens_hit_at_line(self):
        self.write("a.md", "one\ntwo needle\nthree")
        self.write("b.md", "nothing")
        e = self.make(self.docs / "b.md")
        e.search_folder()
        self.answer(e, "needle")
        self.assertTrue(e.prompt_active)
        self.assertTrue(any("a.md:2" in line for line in e.panel_lines))
        self.answer(e, "1")
        self.assertEqual(e.path, self.docs / "a.md")
        self.assertEqual(line_col(e.buffer.text, e.buffer.cursor_position)[0], 1)

    def test_folder_search_without_results(self):
        e = self.make(self.write("b.md", "x"))
        e.search_folder()
        self.answer(e, "absent")
        self.assertFalse(e.prompt_active)
        self.assertIn("No matches", e.status_message)


class TestPanelsAndStatus(EditorTestCase):
    def test_validation_panel(self):
        e = self.make()
        e.show_validation()
        self.assertIn("No Markdown problems", e.panel_lines[0])
        self.type(e, "[x]()\n```\nopen")
        e.show_validation()
        self.assertTrue(any("ERROR" in line for line in e.panel_lines))
        e.handle_escape()
        self.assertEqual(e.panel_lines, [])

    def test_help_panel(self):
        e = self.make()
        e.show_help()
        self.assertTrue(any("Alt+Enter" in line for line in e.panel_lines))

    def test_status_reflects_state(self):
        path = self.write("a.md", "x")
        e = self.make(path)
        text = lambda: "".join(t for _, t in e._status_fragments())
        self.assertIn("Saved", text())
        self.type(e, "y")
        self.assertIn("Unsaved", text())
        self.assertIn("Ln 1, Col 2", text())
        self.assertIn("a.md", text())

    def test_key_bindings_table(self):
        e = self.make()
        self.assertIsNotNone(e.bindings)
        self.assertAlmostEqual(e.app.timeoutlen, 0.3)


if __name__ == "__main__":
    unittest.main()
