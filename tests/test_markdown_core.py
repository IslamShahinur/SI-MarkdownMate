import unittest

import markdown_core as mc


def labels(items):
    return [s.label for s in items]


class TestSuggestions(unittest.TestCase):
    def test_heading_trigger_offers_levels(self):
        found = mc.get_suggestions("#")
        self.assertEqual(found[0].insert, "# ")
        self.assertTrue(all(s.replace_len == 1 for s in found))

    def test_typed_level_comes_first(self):
        self.assertEqual(mc.get_suggestions("##")[0].insert, "## ")

    def test_list_trigger(self):
        self.assertIn("Task", labels(mc.get_suggestions("-")))

    def test_plain_text_gets_nothing(self):
        for text in ("Hello world", "# Real heading text", "**bold text**", "see #", "- item text", "5 * 3"):
            self.assertEqual(mc.get_suggestions(text), [], text)

    def test_no_suggestions_inside_code_fence(self):
        self.assertEqual(mc.get_suggestions("```python\n#"), [])
        self.assertNotEqual(mc.get_suggestions("```python\nx\n```\n#"), [])

    def test_code_fence_template_places_cursor_inside(self):
        s = mc.get_suggestions("```")[0]
        self.assertEqual(s.insert[s.cursor_offset:], "\n```")
        self.assertEqual(s.replace_len, 3)

    def test_link_and_image(self):
        self.assertEqual(mc.get_suggestions("see [")[0].insert, "[](url)")
        self.assertEqual(mc.get_suggestions("see ![")[0].insert, "![](image-url)")
        self.assertEqual(mc.get_suggestions("word[") , [])

    def test_task_checkbox_after_bullet(self):
        self.assertEqual(mc.get_suggestions("- [")[0].insert, "[ ] ")

    def test_table_and_blockquote_and_ordered(self):
        self.assertIn("| --- |", mc.get_suggestions("|")[0].insert)
        self.assertEqual(mc.get_suggestions(">")[0].insert, "> ")
        self.assertEqual(mc.get_suggestions("12.")[0].insert, "12. ")

    def test_divider_gets_blank_line_after_text(self):
        after_text = mc.get_suggestions("Some text\n---")[0].insert
        self.assertTrue(after_text.startswith("\n---"))
        self.assertTrue(mc.get_suggestions("\n---")[0].insert.startswith("---"))

    def test_slash_menu_filters(self):
        self.assertGreater(len(mc.get_suggestions("/")), 10)
        found = mc.get_suggestions("/ta")
        self.assertEqual(labels(found), ["Task", "Table"])
        self.assertTrue(all(s.replace_len == 3 for s in found))

    def test_inline_markers(self):
        self.assertEqual(mc.get_suggestions("text `")[0].label, "Inline code")
        self.assertEqual(mc.get_suggestions("text **")[0].label, "Bold")
        self.assertEqual(labels(mc.get_suggestions("text *"))[0], "Italic")
        self.assertEqual(mc.get_suggestions("text``"), [])


class TestToggleWrap(unittest.TestCase):
    def test_bold_on_off(self):
        t, s, e = mc.toggle_wrap("hello", 0, 5, "**")
        self.assertEqual((t, t[s:e]), ("**hello**", "hello"))
        t2, s2, e2 = mc.toggle_wrap(t, s, e, "**")
        self.assertEqual((t2, t2[s2:e2]), ("hello", "hello"))

    def test_italic_does_not_damage_bold(self):
        t, s, e = mc.toggle_wrap("**hi**", 2, 4, "*")
        self.assertEqual(t, "***hi***")
        t, s, e = mc.toggle_wrap(t, s, e, "*")
        self.assertEqual(t, "**hi**")

    def test_bold_off_inside_bold_italic(self):
        t, _, _ = mc.toggle_wrap("***hi***", 3, 5, "**")
        self.assertEqual(t, "*hi*")

    def test_selection_including_markers(self):
        t, s, e = mc.toggle_wrap("**hello**", 0, 9, "**")
        self.assertEqual((t, t[s:e]), ("hello", "hello"))

    def test_word_under_cursor_when_nothing_selected(self):
        t, s, e = mc.toggle_wrap("one two three", 5, 5, "**")
        self.assertEqual(t, "one **two** three")

    def test_empty_pair_with_cursor_inside(self):
        t, s, e = mc.toggle_wrap("a  b", 2, 2, "**")
        self.assertEqual((t, s, e), ("a **** b", 4, 4))

    def test_whitespace_is_trimmed(self):
        t, _, _ = mc.toggle_wrap("hi there ", 0, 9, "**")
        self.assertEqual(t, "**hi there** ")

    def test_generic_marker(self):
        t, s, e = mc.toggle_wrap("gone", 0, 4, "~~")
        self.assertEqual(t, "~~gone~~")
        self.assertEqual(mc.toggle_wrap(t, s, e, "~~")[0], "gone")

    def test_apostrophe_word(self):
        self.assertEqual(mc.toggle_wrap("don't stop", 2, 2, "**")[0], "**don't** stop")


class TestDivider(unittest.TestCase):
    def test_blank_line_before_rule(self):
        t, c = mc.insert_divider("Hello", 5)
        self.assertEqual(t, "Hello\n\n---\n\n")
        self.assertEqual(c, len(t))

    def test_empty_document(self):
        self.assertEqual(mc.insert_divider("", 0), ("---\n\n", 5))

    def test_after_blank_line(self):
        t, _ = mc.insert_divider("Hello\n\n", 7)
        self.assertEqual(t, "Hello\n\n---\n\n")

    def test_in_middle(self):
        t, c = mc.insert_divider("ab\ncd", 3)
        self.assertEqual(t, "ab\n\n---\n\ncd")
        self.assertEqual(t[c:], "cd")


class TestHtmlStyles(unittest.TestCase):
    def test_spans(self):
        self.assertEqual(mc.color_span("x", "RED"), '<span style="color: red;">x</span>')
        self.assertEqual(mc.color_span("x", "#F80"), '<span style="color: #f80;">x</span>')
        self.assertEqual(mc.font_size_span("x", 24), '<span style="font-size: 24px;">x</span>')

    def test_rejects_unsafe_values(self):
        for bad in ("red;}<script>", "", "javascript:x"):
            with self.assertRaises(ValueError):
                mc.color_span("x", bad)
        for bad in (7, 97, "big", None):
            with self.assertRaises(ValueError):
                mc.font_size_span("x", bad)

    def test_apply_change_remove(self):
        t, s, e = mc.apply_style_span("a word b", 2, 6, "color", "red")
        self.assertEqual(t, 'a <span style="color: red;">word</span> b')
        t2, s2, e2 = mc.apply_style_span(t, s, e, "color", "blue")
        self.assertEqual(t2, 'a <span style="color: blue;">word</span> b')
        t3, _, _ = mc.apply_style_span(t2, s2, e2, "color", None)
        self.assertEqual(t3, "a word b")

    def test_change_when_only_inner_text_selected(self):
        text = 'x <span style="color: red;">word</span> y'
        start = text.index("word")
        t, _, _ = mc.apply_style_span(text, start, start + 4, "color", "green")
        self.assertEqual(t, 'x <span style="color: green;">word</span> y')

    def test_remove_nothing(self):
        self.assertEqual(mc.apply_style_span("plain", 0, 5, "color", None)[0], "plain")

    def test_placeholder_without_word(self):
        t, s, e = mc.apply_style_span("a  b", 2, 2, "font-size", 20)
        self.assertEqual(t[s:e], "text")

    def test_word_under_cursor(self):
        t, _, _ = mc.apply_style_span("one two", 5, 5, "color", "red")
        self.assertEqual(t, 'one <span style="color: red;">two</span>')


class TestValidation(unittest.TestCase):
    def msgs(self, text):
        return [str(i) for i in mc.validate_markdown(text)]

    def has(self, text, fragment):
        return any(fragment in m for m in self.msgs(text))

    def test_clean_document(self):
        doc = "# Title\n\nSome **bold** and *italic* and `code`.\n\n- a\n- b\n\n| a | b |\n|---|---|\n| 1 | 2 |\n"
        self.assertEqual(self.msgs(doc), [])

    def test_unclosed_fence(self):
        issues = mc.validate_markdown("text\n```python\nprint('hi')\n")
        self.assertEqual((issues[0].line, issues[0].severity), (2, "error"))
        self.assertIn("Unclosed", issues[0].message)

    def test_closed_fence_and_tilde_and_longer_fence(self):
        self.assertEqual(self.msgs("```\nx\n```\n"), [])
        self.assertEqual(self.msgs("~~~\nx\n~~~\n"), [])
        self.assertEqual(self.msgs("````\n```\ninner\n```\n````\n"), [])

    def test_code_is_not_checked(self):
        self.assertEqual(self.msgs("```\nx = a**b**c**\n[a]()\n```\n"), [])
        self.assertEqual(self.msgs("Use `a**b` here"), [])

    def test_empty_destinations(self):
        self.assertTrue(self.has("[text]()", "empty destination"))
        self.assertTrue(self.has("![alt]()", "Image has an empty destination"))

    def test_incomplete_link(self):
        self.assertTrue(self.has("see [text](http://x", "missing closing"))
        self.assertEqual(self.msgs("see [text](http://x) and (aside"), [])

    def test_bare_image_and_references(self):
        self.assertTrue(self.has("![alt]", "Image has no destination"))
        self.assertEqual(self.msgs("![alt]\n\n[alt]: http://x/y.png"), [])
        self.assertEqual(self.msgs("![alt][ref]\n\n[ref]: http://x"), [])

    def test_link_without_text(self):
        self.assertTrue(self.has("[](http://x)", "no text"))

    def test_unmatched_bold(self):
        self.assertTrue(self.has("one **bold", "unmatched emphasis"))

    def test_literal_asterisks_not_flagged(self):
        for text in ("2 * 3 * 4", "* item\n* item two", "\\*escaped", "***", "a\n\n---\n\nb"):
            self.assertEqual(self.msgs(text), [], text)

    def test_nested_emphasis_ok(self):
        self.assertEqual(self.msgs("**bold *italic* bold** and ***both***"), [])

    def test_emphasis_spans_lines_in_paragraph(self):
        self.assertEqual(self.msgs("**bold\nacross lines**"), [])

    def test_table_problems(self):
        self.assertTrue(self.has("| a | b |\n|---|---|\n| 1 | 2 | 3 |\n", "Table row has 3"))
        self.assertTrue(self.has("| a | b |\n|---|\n| 1 | 2 |\n", "separator has 1"))
        self.assertTrue(self.has("| a | b |\n| 1 | 2 |\n", "without a header separator"))

    def test_html_spans(self):
        self.assertTrue(self.has('<span style="color: red;">x', "never closed"))
        self.assertTrue(self.has("x</span>", "without an opening"))
        self.assertEqual(self.msgs('<span style="color: red;">x</span>'), [])

    def test_line_numbers_and_order(self):
        issues = mc.validate_markdown("ok\n\n[a]()\n\n```\nopen\n")
        self.assertEqual([i.line for i in issues], [3, 5])

    def test_crlf_input(self):
        self.assertEqual(self.msgs("# T\r\n\r\ntext\r\n"), [])


class TestSearch(unittest.TestCase):
    def test_search_text(self):
        self.assertEqual(mc.search_text("one\ntwo\nthree", "TWO"), [(2, 0)])
        self.assertEqual(mc.search_text("aXa", "a", case_sensitive=True), [(1, 0), (1, 2)])
        self.assertEqual(mc.search_text("abc", ""), [])

    def test_find_offsets(self):
        self.assertEqual(mc.find_offsets("abcabc", "bc"), [(1, 3), (4, 6)])
        self.assertEqual(mc.find_offsets("a.b", "."), [(1, 2)])


if __name__ == "__main__":
    unittest.main()
