"""Tests for the table renderer.

The renderer is the overlay's layout engine: it measures the columns from
the rows it is given, so the chords line up, and it never returns a line as
wide as the terminal, because a line of exactly `cols` columns wraps the
cursor and shifts everything after it.

Run: python3 tests/test_table.py
"""

import os
import re
import sys
import unicodedata
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

from managr import table as table_mod  # noqa: E402


# The escape sequences a cell may hold: CSI (colours, cursor moves), OSC
# (window titles) and the two-byte ones.
#
# Written here rather than imported from the module: the width of a line
# is measured by this test, because a test that asks the code under test
# how wide its own output is proves nothing.
ANSI = re.compile(
    r"\x1b\[[0-?]*[ -/]*[@-~]"
    r"|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)"
    r"|\x1b[@-Z\\-_]"
)


def display_width(text):
    """The number of terminal columns `text` occupies, measured here.

    The module has `visible_width` for this, and it is tested separately
    below, but the *budget* assertions must not depend on it: a broken
    `visible_width` would then hide itself.
    """
    plain = ANSI.sub("", text)
    width = 0
    for char in plain:
        if unicodedata.combining(char):
            # A combining mark rides on the character before it.
            continue
        width += 2 if unicodedata.east_asian_width(char) in ("W", "F") else 1
    return width


# The columns the overlay uses, and the cells of a layer with two items.
COLUMNS = [
    table_mod.Column(name="key"),
    table_mod.Column(name="desc"),
    table_mod.Column(name="chord", optional=True, priority=1),
    table_mod.Column(name="note", optional=True, priority=0),
]

ROWS = [
    ["h", "focus pane left", "C-a h", ""],
    ["k", "focus pane up", "C-a k", ""],
]


class TestVisibleWidth(unittest.TestCase):
    """How many columns a piece of text takes."""

    def test_plain_text(self):
        self.assertEqual(table_mod.visible_width("focus pane left"), 15)

    def test_escape_sequences_take_no_space(self):
        self.assertEqual(table_mod.visible_width("\x1b[38;5;81mh\x1b[0m"), 1)
        self.assertEqual(table_mod.visible_width("\x1b[1m\x1b[0m"), 0)

    def test_east_asian_wide_characters_take_two_columns(self):
        self.assertEqual(table_mod.visible_width("日本語"), 6)
        self.assertEqual(table_mod.visible_width("Ａ"), 2)

    def test_ambiguous_characters_take_one_column(self):
        # The private use area is ambiguous, and that is where the Nerd Font
        # icons live: one column, not two.
        self.assertEqual(table_mod.visible_width("\ue0b0"), 1)
        self.assertEqual(table_mod.visible_width("·"), 1)

    def test_a_nerd_font_glyph_takes_one_column(self):
        # The overlay draws Nerd Font icons (U+EA60..U+ECxx and the
        # font-awesome range). An icon that counted as two columns would push
        # every row one column to the right, and the right border of the
        # frame with it.
        for glyph in ("\uebd5", "\ueb7f", "\uf013", "\uec20"):
            self.assertEqual(table_mod.visible_width(glyph), 1, glyph)

    def test_a_row_with_a_glyph_is_not_wider_than_it_should_be(self):
        # The width is measured here, not with the module's own function.
        rows = [["n", "\uebd5", "navigation", "", ""]]
        lines = table_mod.render(rows, COLUMNS, 80, indent="  ")
        # 2 (indent) + 1 (key) + 2 (gap) + 1 (glyph) + 2 (gap) + 10 (desc)
        self.assertEqual(display_width(lines[0]), 18)

    def test_a_combining_mark_takes_no_space(self):
        self.assertEqual(table_mod.visible_width("é"), 1)


class TestTruncate(unittest.TestCase):
    def test_a_short_text_is_untouched(self):
        self.assertEqual(table_mod.truncate("hello", 10), "hello")

    def test_a_long_text_is_cut_and_marked(self):
        out = table_mod.truncate("focus pane left", 8)
        self.assertEqual(out, "focus...")
        self.assertEqual(display_width(out), 8)

    def test_escape_sequences_do_not_count_towards_the_width(self):
        out = table_mod.truncate("\x1b[1mfocus pane left\x1b[0m", 8)
        self.assertEqual(display_width(out), 8)
        self.assertTrue(out.endswith("..."))

    def test_a_cut_leaves_the_colour_closed(self):
        # The cut must not leave the colour running into the next cell.
        out = table_mod.truncate("\x1b[38;5;81mfocus pane left\x1b[0m", 8)
        self.assertIn("\x1b[0m", out)

    def test_a_width_of_zero_gives_nothing(self):
        self.assertEqual(table_mod.truncate("focus pane left", 0), "")

    def test_a_line_break_is_flattened(self):
        # Raw mode has ONLCR off, so a bare newline in the middle of a line
        # moves the cursor down without returning it to column 0.
        out = table_mod.truncate("focus\npane left", 20)
        self.assertNotIn("\n", out)
        self.assertEqual(out, "focus pane left")


class TestOneLine(unittest.TestCase):
    def test_a_line_break_becomes_a_space(self):
        self.assertEqual(table_mod.one_line("focus\npane"), "focus pane")
        self.assertEqual(table_mod.one_line("focus\r\npane"), "focus pane")
        self.assertEqual(table_mod.one_line("focus\rpane"), "focus pane")

    def test_a_cell_cannot_hold_a_line_break(self):
        rows = [["h", "focus\npane left", "C-a h", ""]]
        for line in table_mod.render(rows, COLUMNS, 80):
            self.assertNotIn("\n", line)

    def test_a_stray_escape_is_removed(self):
        # The cells hold text the user wrote, and `layers.toml` can be
        # hand-edited. An escape that starts nothing is emitted raw, and
        # the terminal then waits for a final byte and swallows the rest
        # of the line.
        self.assertEqual(table_mod.one_line("focus\x1b pane"), "focus pane")

    def test_the_control_characters_become_spaces(self):
        # A vertical tab or a form feed moves the cursor.
        self.assertEqual(table_mod.one_line("focus\x0bpane"), "focus pane")
        self.assertEqual(table_mod.one_line("focus\x0cpane"), "focus pane")
        self.assertEqual(table_mod.one_line("focus\x07pane"), "focus pane")
        self.assertEqual(table_mod.one_line("focus\tpane"), "focus pane")

    def test_the_colour_sequences_are_kept(self):
        # The escape sequences the overlay writes are colour, not rubbish.
        coloured = "\x1b[1mfocus\x1b[0m"
        self.assertEqual(table_mod.one_line(coloured), coloured)

    def test_a_control_character_takes_no_space(self):
        self.assertEqual(table_mod.visible_width("\x0b"), 0)
        self.assertEqual(table_mod.visible_width("\x1b"), 0)


class TestRender(unittest.TestCase):
    def test_the_columns_line_up(self):
        lines = table_mod.render(ROWS, COLUMNS, 80)
        self.assertEqual(lines[0].index("C-a h"), lines[1].index("C-a k"))

    def test_the_columns_are_measured_from_the_content(self):
        # The key column is as wide as the widest key (one column), the
        # description column as wide as the longest description (15), and
        # the chord column starts after them: 2 (indent) + 1 (key) + 2
        # (gap) + 15 (description) + 2 (gap).
        lines = table_mod.render(ROWS, COLUMNS, 80, indent="  ")
        self.assertIn("focus pane left", lines[0])
        self.assertIn("C-a h", lines[0])
        self.assertEqual(lines[0].index("focus pane left"), 5)
        self.assertEqual(lines[0].index("C-a h"), 22)
        self.assertEqual(lines[1].index("C-a k"), 22)

    def test_no_line_is_wider_than_the_budget(self):
        # The width is measured here, not with the module's own function:
        # asking the code under test how wide its output is proves nothing.
        for budget in (8, 12, 20, 40, 80):
            for line in table_mod.render(ROWS, COLUMNS, budget):
                self.assertLessEqual(
                    display_width(line),
                    budget,
                    "a line is wider than %d columns: %r" % (budget, line),
                )

    def test_the_optional_column_goes_before_the_description_is_cut(self):
        lines = table_mod.render(ROWS, COLUMNS, 20)
        joined = "\n".join(lines)
        self.assertNotIn("C-a h", joined)
        self.assertIn("focus pane left", joined)

    def test_a_narrow_table_still_shows_the_key_and_a_description(self):
        lines = table_mod.render(ROWS, COLUMNS, 12)
        self.assertTrue(lines[0].strip())
        self.assertTrue(lines[0].startswith("h"))
        self.assertIn("...", lines[0])

    def test_an_empty_column_takes_no_space(self):
        rows = [["", "focus pane left", "C-a h", ""]]
        lines = table_mod.render(rows, COLUMNS, 80)
        self.assertEqual(lines[0].index("focus pane left"), 0)

    def test_a_hint_row_keeps_its_note_in_a_column_of_its_own(self):
        rows = [
            ["h", "focus pane left", "C-a h", ""],
            ["?", "help", "C-a ?", "press this in herdr"],
        ]
        lines = table_mod.render(rows, COLUMNS, 80)
        self.assertEqual(lines[0].index("C-a h"), lines[1].index("C-a ?"))
        self.assertEqual(
            lines[1].index("press this in herdr"),
            lines[1].index("C-a ?") + len("C-a ?") + 2,
        )

    def test_a_missing_cell_is_an_empty_one(self):
        rows = [["h", "focus pane left"]]
        lines = table_mod.render(rows, COLUMNS, 80)
        self.assertIn("focus pane left", lines[0])

    def test_no_rows_give_no_lines(self):
        self.assertEqual(table_mod.render([], COLUMNS, 80), [])


if __name__ == "__main__":
    # Any other test file named on the command line is run too, so
    # `python3 tests/test_keymap.py tests/test_configure.py` runs both
    # rather than ignoring the second one.
    _loader = unittest.TestLoader()
    _suite = unittest.TestSuite()
    _suite.addTests(_loader.loadTestsFromModule(sys.modules["__main__"]))
    for _name in [
        os.path.splitext(os.path.basename(a))[0]
        for a in sys.argv[1:]
        if a.endswith(".py")
    ]:
        _suite.addTests(_loader.loadTestsFromNames([_name]))
    _result = unittest.TextTestRunner(verbosity=2).run(_suite)
    sys.exit(0 if _result.wasSuccessful() else 1)
