"""A small table renderer, for the overlay's rows.

The overlay used to build each row by hand: two spaces, the key, two spaces,
the description, two spaces, the chord. The description was whatever length
the label happened to be, so the chord started in a different column on every
row and the table looked ragged.

This module is the fix, and it is deliberately a *table* renderer rather than
a pile of overlay-specific code: the widths come from the rows being
rendered, and a later view (a list of destinations, say) can hand it a
different set of columns without changing anything here.

Three things it has to get right:

**Widths are measured, in terminal columns.** The cells hold ANSI escape
sequences, which take no space, and East Asian wide characters, which take
two. `visible_width` is the one place that knows how to count.

**A line must never be as wide as the terminal.** A line of exactly `cols`
columns puts the cursor in the last column, and the next character wraps it
to column 0 of the next row, which shifts everything after it. The overlay
therefore caps its lines one column short of the terminal; this module
respects whatever budget it is given and never exceeds it.

**A narrow terminal loses the optional columns first.** The caller decides
which goes first, by priority: the overlay drops the note before the chord,
because a note that says "press this in herdr" means nothing without the
chord to press.

**Nothing a user wrote can upset the terminal.** The cells hold text from
`layers.toml`, which a user can hand-edit, and a stray escape or a control
character in it would move the cursor or swallow the rest of the line.
`one_line` removes them.
"""

import re
import unicodedata
from dataclasses import dataclass

# The gap between two columns.
GAP = 2

# What a truncated cell ends with.
ELLIPSIS = "..."

# Written when a cut leaves a colour run open.
ANSI_RESET = "\x1b[0m"

# The escape character. On its own it starts nothing: a terminal that sees
# one waits for a final byte and swallows the rest of the line.
ESC = "\x1b"

# Every control character, except the escape. The line breaks are turned
# into spaces by `one_line`, and the escape is handled on its own because
# a sequence that starts with one is legitimate -- it is the colour.
_CONTROLS = frozenset(
    [chr(code) for code in range(0x20) if code != 0x1B]
    + [chr(code) for code in range(0x7F, 0xA0)]
)

# The escape sequences a cell may hold: CSI (colours, cursor moves), OSC
# (window titles) and the two-byte ones. They take no space, but they are
# part of the text, so a truncation has to keep them.
_ANSI = re.compile(
    r"\x1b\[[0-?]*[ -/]*[@-~]"
    r"|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)"
    r"|\x1b[@-Z\\-_]"
)


def strip_ansi(text):
    """`text` without its escape sequences."""
    return _ANSI.sub("", text)


def char_width(char):
    """The number of terminal columns one character occupies."""
    if unicodedata.combining(char):
        # A combining mark rides on the character before it.
        return 0
    if char in _CONTROLS or char == ESC:
        # A control character occupies no column: it moves the cursor, or
        # does nothing at all. Counting it as one would make the width
        # budget wrong for every line that holds one.
        return 0
    if 0xE000 <= ord(char) <= 0xF8FF:
        # The private use area, which is where the Nerd Font icons live: one
        # column, even when the font draws them double width. A terminal
        # that cannot draw the glyph shows a box, which is one column too.
        return 1
    if unicodedata.east_asian_width(char) in ("W", "F"):
        # East Asian wide and fullwidth forms take two columns.
        return 2
    # Narrow, halfwidth and ambiguous take one. Ambiguous includes the
    # private use area, which is where the Nerd Font icons live: an icon
    # that a font draws as double width still counts as one column here,
    # because a terminal that cannot draw it shows a box anyway.
    return 1


def visible_width(text):
    """The number of terminal columns `text` occupies."""
    width = 0
    for char in strip_ansi(text):
        width += char_width(char)
    return width


def _visible_prefix(text, limit):
    """The first `limit` columns of `text`, keeping the escape sequences."""
    parts = []
    width = 0
    index = 0
    length = len(text)
    while index < length:
        match = _ANSI.match(text, index)
        if match is not None:
            parts.append(match.group(0))
            index = match.end()
            continue
        char = text[index]
        index += 1
        step = char_width(char)
        if width + step > limit:
            break
        parts.append(char)
        width += step
    return "".join(parts)


def _close(text):
    """Close a colour run that a cut left open."""
    if "\x1b" in text and not text.endswith(ANSI_RESET):
        return text + ANSI_RESET
    return text


def _clean(text):
    """`text` without the bytes that would leave the terminal in a state.

    The escape sequences are kept -- they are the overlay's colour -- and
    everything else a terminal would act on is removed: a stray escape,
    and the control characters that move the cursor.
    """
    out = []
    index = 0
    length = len(text)
    while index < length:
        match = _ANSI.match(text, index)
        if match is not None:
            out.append(match.group(0))
            index = match.end()
            continue
        char = text[index]
        index += 1
        if char == ESC:
            # A stray escape: the terminal would wait for a final byte and
            # swallow the rest of the line.
            continue
        if char in _CONTROLS:
            # A control character. A space keeps the words apart without
            # moving the cursor.
            out.append(" ")
            continue
        out.append(char)
    return "".join(out)


def one_line(text):
    """`text` as one line, with the bytes that would upset a terminal removed.

    A row of a table is one line, and so is every line the overlay writes:
    raw mode clears ONLCR, so a bare "\\n" in the middle of one would move
    the cursor down without returning it to column 0. A label with a line
    break in it would do exactly that.

    The other control characters go as well, and so does a stray escape.
    The cells hold text a user wrote -- `layers.toml` can be hand-edited --
    and a vertical tab or a form feed in a label moves the cursor, while
    a bare escape makes the terminal wait for a final byte and swallow the
    rest of the line. The escape sequences the overlay writes are matched
    and kept: they are the colour.
    """
    text = text.replace("\r\n", " ").replace("\r", " ").replace("\n", " ")
    return _clean(text)


def truncate(text, width, ellipsis=ELLIPSIS, close=True):
    """`text` cut to at most `width` columns, marked with an ellipsis.

    The escape sequences are kept, so the cut does not break the colour of
    what is left, and the text is flattened to one line: see `one_line`.

    `close` says whether a cut that leaves a colour run open is closed
    here. A row on the selection band is ONE run -- the band is a
    *background*, and the reset that closes a colour closes the
    background too -- so the caller asks for the run to stay open, and
    the band is closed at the end of the row. A reset in the middle of a
    band row ends the band there, and the rest of the row is drawn off
    it: invisible.
    """
    text = one_line(text)
    if width <= 0:
        return ""
    if visible_width(text) <= width:
        return text
    if width <= len(ellipsis):
        # No room for the marker and for anything else.
        cut = _visible_prefix(text, width)
        return _close(cut) if close else cut
    cut = _visible_prefix(text, width - len(ellipsis))
    return (_close(cut) if close else cut) + ellipsis


@dataclass
class Column:
    """One column of a table.

    `optional` columns are dropped when the table does not fit. Among the
    optional ones the lowest `priority` goes first, so the caller decides
    what is worth keeping when the terminal is narrow.
    """

    name: str
    optional: bool = False
    priority: int = 0
    align: str = "left"
    # How narrow the column may get before it is left alone.
    min_width: int = 1


def _column_width(grid, position):
    """The natural width of one column: its widest cell."""
    width = 0
    for row in grid:
        if position < len(row):
            width = max(width, visible_width(row[position]))
    return width


def _widths(grid, kept):
    return [_column_width(grid, position) for position in kept]


def _total(widths, gap):
    """The width of a table with columns of these widths."""
    if not widths:
        return 0
    return sum(widths) + gap * (len(widths) - 1)


def _shrink(widths, specs, kept, gap, budget):
    """Cut the widest column until the table fits.

    The widest column goes first, so the column that has the most to spare
    gives up the space, and every row keeps the same shape: a table that is
    cut in the middle of a line is not a table.
    """
    widths = list(widths)
    while _total(widths, gap) > budget:
        candidates = [
            index
            for index, width in enumerate(widths)
            if width > specs[kept[index]].min_width
        ]
        if not candidates:
            # Everything is at its minimum and it still does not fit. The
            # caller cuts the line itself rather than let it wrap.
            break
        # The widest column, and the leftmost one when they tie.
        index = max(candidates, key=lambda i: (widths[i], -i))
        excess = _total(widths, gap) - budget
        room = widths[index] - specs[kept[index]].min_width
        widths[index] -= min(excess, room)
    return widths


def _pad(text, width, align="left"):
    """Pad `text` to `width` columns."""
    pad = width - visible_width(text)
    if pad <= 0:
        return text
    if align == "right":
        return " " * pad + text
    return text + " " * pad


def render(rows, columns, max_width, indent="", gap=GAP):
    """Render `rows` as aligned lines, at most `max_width` columns wide.

    `rows` is a list of rows, each a list of cells in the order of
    `columns`. The result is a list of lines with no line ending: the caller
    decides how to end them, because the overlay has to end them with CRLF
    (raw mode clears ONLCR, so a bare newline does not return the cursor to
    column 0).
    """
    specs = list(columns)
    grid = [
        [one_line("" if cell is None else str(cell)) for cell in row]
        for row in rows
    ]

    budget = max_width - visible_width(indent)
    if budget < 0:
        budget = 0

    # A column that is empty in every row takes no space at all.
    kept = [
        position
        for position in range(len(specs))
        if _column_width(grid, position) > 0
    ]

    # Drop the optional columns, lowest priority first, for as long as the
    # table does not fit.
    for position in sorted(
        (i for i, spec in enumerate(specs) if spec.optional),
        key=lambda i: (specs[i].priority, i),
    ):
        if _total(_widths(grid, kept), gap) <= budget:
            break
        if position in kept:
            kept.remove(position)

    widths = _shrink(_widths(grid, kept), specs, kept, gap, budget)

    lines = []
    for row in grid:
        cells = []
        for index, position in enumerate(kept):
            cell = row[position] if position < len(row) else ""
            cell = truncate(cell, widths[index])
            cells.append(_pad(cell, widths[index], specs[position].align))
        lines.append(indent + (" " * gap).join(cells))

    # The trailing padding of the last column is not worth keeping, and a
    # line that is still too wide is cut rather than allowed to wrap.
    return [_close_line(line, max_width) for line in lines]


def _close_line(line, max_width):
    line = line.rstrip()
    if visible_width(line) > max_width:
        return truncate(line, max_width)
    return line
