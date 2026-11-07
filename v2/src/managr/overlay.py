"""The modal overlay.

This is the whole point of the plugin, so it is worth being precise about how
it works.

The overlay runs inside a Herdr popup, which is a real terminal. It puts stdin
into raw mode and reads keystrokes itself. Because the popup is session-modal,
**every** keystroke goes to the overlay and none of them reach Herdr's prefix
handler. That is what gives the overlay its own keyspace: `p` and `v` inside it
are the plugin's, and they cannot shadow a Herdr binding.

Actions are not performed by faking keypresses -- `pane.send_keys` writes into
a pane's PTY and Herdr's key handling sits upstream of that. They are performed
by calling the socket API directly, which is why an action can be a real
operation rather than a "press this yourself" note.

Three states: browsing a layer, typing a value (for renames), and typing a
filter. Escape goes up a level, and closes from the root. Backspace also goes
up a level -- except while a filter is being typed, where it deletes a
character.

A fourth state is the *navigatr*: the spaces, tabs and panes of the running
session, drawn as a tree with a cursor on it. It is not a layer -- a layer
maps keys to actions, and the navigatr is a list of places to go -- so it
has its own keys, its own footer and its own way of scrolling. It lives
inside this overlay rather than in another plugin's popup because a popup is
a singleton in Herdr: only one can be open, and ours already is.

The navigatr comes in two kinds, and the state says which one is open: the
tree of places, and a flat list of every agent in the session with the ones
that want attention first. The same keys, the same footer and the same
filter serve both, and the rows are composed by two different composers:
a tree and a flat list are not the same shape.

A long layer does not fit on the screen, so the rows scroll: the arrows, the
page keys and ctrl+d / ctrl+u move the window. `j` and `k` are *not* scroll
keys -- they are actions in the panes and navigation layers, and taking them
would make those actions unreachable.
"""

import codecs
import os
import signal
import sys

try:
    import select
except ImportError:  # pragma: no cover - windows
    select = None

try:
    import termios
    import tty
except ImportError:  # pragma: no cover - windows
    termios = None
    tty = None

from . import table
from . import visuals as visuals_mod
from . import navigatr as navigatr_mod
from . import custom as custom_mod
from . import tree as tree_mod
from .client import HerdrError, HerdrUnavailable
from .table import truncate

ESC = "\x1b"
CSI = "\x1b["
# The same, as bytes: what a read from the terminal returns.
ESC_BYTES = b"\x1b"
CSI_BYTES = b"\x1b["

# Bytes that are a key of their own rather than text. A UTF-8 multi-byte
# character never starts with one of these, so they can be recognised before
# the character decoder sees them.
CONTROL_KEYS = {
    b"\x03": "ctrl-c",
    b"\x04": "ctrl-d",
    b"\x0d": "enter",
    b"\x0a": "enter",
    b"\x7f": "backspace",
    b"\x08": "backspace",
    b"\t": "tab",
}

# The final byte of a CSI sequence, and the key it stands for.
#
# ECMA-48 lets any byte in 0x40-0x7E end a CSI sequence, but a terminal only
# sends the ones below. Accepting any of them would swallow the character
# typed right after an Escape: ESC "[" "x" would be read as one unknown
# sequence, and the "x" would be lost.
CSI_FINALS = {
    b"A": "up",
    b"B": "down",
    b"C": "right",
    b"D": "left",
    b"H": "home",
    b"F": "end",
    b"Z": "backtab",
}

# The "~" family names its key with the parameter in front of it: ESC[3~ is
# Delete, ESC[5~ is PageUp.
CSI_TILDE = {
    "1": "home",
    "2": "insert",
    "3": "delete",
    "4": "end",
    "5": "pageup",
    "6": "pagedown",
    "7": "home",
    "8": "end",
}

# Raw mode turns off output post-processing (termios's cfmakeraw clears
# OPOST), which means ONLCR is off and a bare "\n" moves down a line *without*
# returning the cursor to column 0. Every line would start where the previous
# one ended, and the overlay would render as a diagonal staircase.
#
# The fix is to end every line with "\r\n" explicitly rather than relying on
# the terminal to translate it. Doing it here, rather than switching to
# tty.setcbreak, keeps the guarantee even if Herdr's popup pty happens to
# have OPOST off to begin with.
NL = "\r\n"

# The escape sequences. The *colours* are not here: they come from the
# user's own Herdr theme, so they live in the palette (`visuals.Palette`).
RESET = visuals_mod.RESET
BOLD = visuals_mod.BOLD
DIM = visuals_mod.DIM

# The frame's box-drawing characters. Light, so the frame reads as a frame
# and not as content.
FRAME_TOP_LEFT = "╭"
FRAME_TOP_RIGHT = "╮"
FRAME_BOTTOM_LEFT = "╰"
FRAME_BOTTOM_RIGHT = "╯"
FRAME_HORIZONTAL = "─"
FRAME_VERTICAL = "│"

# The narrowest overlay that is worth framing. Below this the frame costs
# more columns than it says, and the plain layout is kinder.
MIN_FRAME_WIDTH = 12

# The rows the frame costs, besides the item rows.
#
# The render writes, in order: the top border (with the breadcrumb in it),
# the chrome lines, the item rows, and the bottom border -- or the plain
# footer when there is no frame.
#
# The cursor's own row is counted too, and that is the row the frame used
# to forget. Every line the overlay writes ends with a newline, so the last
# newline leaves the cursor on the row *after* the last line: a frame that
# filled the screen exactly scrolled by one row, and the top border -- the
# breadcrumb -- went off the top of the screen.
FRAME_ROWS = 3
PLAIN_ROWS = 2

# The columns of an item row: the overlay's own key, the layer's glyph, the
# description, the Herdr chord, and the note that says a chord has to be
# pressed in Herdr.
#
# The widths are measured from the rows being rendered, so the columns line
# up whatever the labels happen to be.
#
# The note goes before the chord does. A note says "press this in herdr",
# so it means nothing without the chord to press: dropping the chord and
# keeping the note leaves the user with an instruction and no key. The
# order of the columns is the order they are drawn in; the priority is the
# order they are given up in. The glyph goes last of all: it is decoration,
# and a key is worth more than an icon.
ITEM_COLUMNS = [
    table.Column(name="key", min_width=1),
    table.Column(name="icon", optional=True, priority=3, min_width=1),
    table.Column(name="desc", min_width=4),
    table.Column(name="chord", optional=True, priority=1, min_width=1),
    table.Column(name="note", optional=True, priority=0, min_width=1),
]

# Every row starts here, so the rows are set in from the edge.
ROW_INDENT = "  "

# The navigatr: the spaces, tabs and panes of the running session.
#
# It is not a layer. A layer maps keys to actions; the navigatr is a list
# of destinations with a cursor on it, and the key that opens it is the
# overlay's own.
#
# The key is the one from the definition file (`navigatr_key` in
# layers.toml), like every other entry in the root: a user who wants it
# somewhere else moves it there. A layer that binds the key itself keeps
# it, exactly like the filter key.
# The detail region: the two lines under a navigatr's list, above the
# keys. Herdr's own navigation surface has the same -- the path of the
# selection and its directory -- and the row cannot carry them: the path
# is cut to fit and the directory is not in the row at all.
DETAIL_ROWS = 2

# The width of the status word column in the agents list. The vocabulary
# is fixed -- blocked, working, done, idle, unknown -- so the width is
# too, and "unknown" is the longest of them.
STATUS_WORD_WIDTH = 7

# The panel has to be tall enough for the chrome, the detail and a few
# rows of list, or the detail is not worth the list it would cost.
MIN_DETAIL_HEIGHT = 12

NAVIGATR_TITLE = "navigatr"
NAVIGATR_HINT = "the spaces, tabs and panes of this session"

# The agents navigatr: a flat list of every agent in the session, the ones
# that want attention first.
#
# It is a second way of browsing the same session, not a second layer: the
# same keys, the same footer, the same filter, and the cursor on a list
# rather than on a tree.
AGENTS_TITLE = "agents"
AGENTS_HINT = "every agent in this session, the urgent ones first"

# The tree's own glyphs, when the user has not replaced them.
#
# These are box-drawing characters, not Nerd Font icons: they are the
# structure of the tree, and the structure is not decoration. The layer and
# group icons are the font's, and `icons = false` still turns those off.
DEFAULT_BRANCH = "▾"
DEFAULT_TEE = "├─"
DEFAULT_ELBOW = "└─"

# The marker of a space that is folded: its rows are hidden, so the space
# says so rather than looking open.
DEFAULT_COLLAPSED = "▸"

# The marker that says which row is the current one.
#
# It is a marker, not decoration: a current row that is not marked is a
# row the user cannot tell apart from the others. So it has a fallback for
# a terminal with the glyphs turned off, and the fallback is the same
# plain character the icon is.
DEFAULT_ARROW = "→"

# The key that opens the filter.
#
# The layers bind letters and "?" (the reference key), so "/" is free. A
# layer that binds "/" itself keeps it: the filter only takes the key when
# the layer does not want it.
FILTER_KEY = "/"

# Whether the terminal's cursor is shown.
#
# The overlay hides it while it draws a list: every line it writes ends
# with a newline, so the cursor would sit on the row after the frame,
# outside the panel, blinking as a block under the window. The input
# state shows it again, because the user is typing into a line and the
# cursor says where they are.
HIDE_CURSOR = "\x1b[?25l"
SHOW_CURSOR = "\x1b[?25h"

# ctrl+u is not one of the control keys the decoder knows, so it arrives as
# a control character inside a "char" key. The decoding is left alone and
# the key is recognised here, next to the other scroll keys.
CTRL_U = "\x15"


class Key:
    """A decoded keystroke."""

    def __init__(self, name, char=""):
        self.name = name        # "char", "esc", "enter", "backspace", "up", ...
        self.char = char


def decode(data):
    """Turn the bytes of one complete key into a Key.

    `KeyReader` is what the overlay actually uses: a read from the terminal
    can hold several keys, or half of one, and this function cannot tell the
    difference. It stays for callers that already hold a whole key.
    """
    if not data:
        return Key("eof")
    name = CONTROL_KEYS.get(data)
    if name is not None:
        return Key(name)
    if data == b"\x1b":
        return Key("esc")
    if data.startswith(CSI_BYTES):
        return decode_sequence(data)
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return Key("unknown")
    if len(text) == 1:
        return Key("char", text)
    return Key("unknown")


def decode_sequence(sequence):
    """Decode a complete escape sequence, e.g. b"\\x1b[A"."""
    tail = sequence[2:]
    if not tail:
        return Key("unknown")
    final = tail[-1:]
    name = CSI_FINALS.get(final)
    if name is not None:
        return Key(name)
    if final == b"~":
        # ESC[3~ is Delete: the "~" family names the key with the parameter
        # in front of it.
        return Key(CSI_TILDE.get(tail[:-1].decode("ascii", "replace"), "unknown"))
    return Key("unknown")


def csi_final(data):
    """The index of the byte that ends a CSI sequence, or None.

    The final byte has to be one a terminal actually sends. ECMA-48 allows
    any byte in 0x40-0x7E, but accepting any of them would swallow the
    character typed right after an Escape: ESC "[" "x" would be read as one
    unknown sequence, and the "x" would be lost.
    """
    for index in range(2, len(data)):
        byte = data[index : index + 1]
        if not 0x40 <= byte[0] <= 0x7E:
            # A parameter or intermediate byte: the final byte is still to
            # come.
            continue
        if byte in CSI_FINALS or byte == b"~":
            return index
        # A byte that cannot end a sequence: this is not a CSI sequence at
        # all, so the "[" was a literal "[" the user typed.
        return None
    return None


# A lone ESC is ambiguous: it is either the Escape key or the first byte of
# an escape sequence such as "\x1b[A". Wait this long for the rest of the
# sequence before deciding that it is Escape.
ESC_TIMEOUT = 0.05


class KeyReader:
    """Turn a byte stream into one Key at a time.

    A single read from the terminal can return several keystrokes ("pv"), the
    first half of a multi-byte character, or the beginning of an escape
    sequence. Decoding the whole read as one key loses all of it -- "pv"
    becomes one unknown key and both keystrokes vanish. So the reader keeps a
    pending buffer and hands out one key at a time.
    """

    def __init__(self, fd, escape_timeout=ESC_TIMEOUT):
        self.fd = fd
        self.escape_timeout = escape_timeout
        self._buffer = b""
        # An incremental decoder holds the bytes of a character that arrived
        # split across two reads, so a multi-byte character is not lost.
        self._decoder = codecs.getincrementaldecoder("utf-8")()
        self._partial = False

    def feed(self, data):
        """Add bytes read from the terminal to the pending buffer."""
        self._buffer += data

    def keys(self):
        """Every key that can be decoded from the buffer right now.

        Stops when the buffer holds only a partial key: the caller should
        read more bytes and feed them.
        """
        while True:
            key = self.next_key()
            if key is None:
                return
            yield key

    def next_key(self):
        """The next key, or None when the buffer holds an incomplete one."""
        if not self._buffer:
            return None
        if self._buffer.startswith(b"\x1b"):
            return self._escape_key()
        return self._utf8_key()

    # -- the two kinds of key ------------------------------------------------

    def _escape_key(self):
        """A key that starts with ESC.

        The escape branch never goes through the UTF-8 decoder: an escape
        sequence is not text. Only a complete sequence is decoded; a partial
        one waits for the rest, and a lone ESC becomes the Escape key once
        we know no more bytes are coming.
        """
        data = self._buffer
        if len(data) == 1:
            if self._more_coming():
                return None
            self._buffer = b""
            self._reset_partial()
            return Key("esc")
        if data.startswith(CSI_BYTES):
            final = csi_final(data)
            if final is not None:
                sequence = data[: final + 1]
                self._buffer = data[final + 1 :]
                self._reset_partial()
                return decode_sequence(sequence)
            # Not a sequence we know. Either the rest is still on its way,
            # or the "[" was a literal "[" the user typed.
            if self._more_coming():
                return None
            # Give the Escape key back and leave the "[" and whatever
            # follows as ordinary keys: consuming them would lose the
            # character the user typed.
            self._buffer = data[1:]
            self._reset_partial()
            return Key("esc")
        # ESC followed by something that is not a CSI: the ESC is Escape and
        # whatever follows is the next key.
        self._buffer = data[1:]
        self._reset_partial()
        return Key("esc")

    def _utf8_key(self):
        """One character, decoded incrementally.

        The incremental decoder keeps the bytes of a character that has not
        arrived in full yet, so a character split across two reads survives.
        """
        while self._buffer:
            byte = self._buffer[:1]
            name = CONTROL_KEYS.get(byte)
            if name is not None:
                self._buffer = self._buffer[1:]
                # A control key interrupts a half-decoded character. The
                # decoder still holds the bytes of it, and the *next*
                # keystroke would decode to garbage and be dropped.
                self._reset_partial()
                return Key(name)
            if byte == ESC_BYTES:
                # An escape sequence part-way through the buffer, behind a
                # half-decoded character. The character is garbage now; drop
                # it and let the escape branch decode the sequence.
                self._reset_partial()
                return self._escape_key()
            try:
                text = self._decoder.decode(byte)
            except UnicodeDecodeError:
                # Not valid UTF-8. Drop that byte only: the bytes after it
                # are still keys.
                self._decoder.reset()
                self._partial = False
                self._buffer = self._buffer[1:]
                return Key("unknown")
            if text:
                self._partial = False
                self._buffer = self._buffer[1:]
                return Key("char", text)
            # The character is incomplete: the decoder holds the bytes it has
            # seen and finishes them when the rest arrives.
            self._partial = True
            self._buffer = self._buffer[1:]
            if not self._buffer:
                return None
        return None

    def _more_coming(self):
        """True when more bytes are already waiting on the terminal."""
        if select is None:
            return False
        try:
            ready, _, _ = select.select([self.fd], [], [], self.escape_timeout)
        except (OSError, ValueError):
            return False
        return bool(ready)

    def _reset_partial(self):
        """Drop a half-decoded character, e.g. when an escape key interrupts."""
        if self._partial:
            self._decoder.reset()
            self._partial = False


class Overlay:
    def __init__(self, tree, context, out=None, visuals=None):
        self.tree = tree
        self.context = context
        self.out = out or sys.stdout
        # The look comes from the user's own Herdr theme, so it is loaded
        # once, here, and not per keystroke.
        self.visuals = visuals if visuals is not None else visuals_mod.load()
        self.palette = self.visuals.palette
        self.layer_id = "root"
        self.stack = []
        self.message = ""
        self.message_kind = "ok"
        self.input_prompt = None
        self.input_buffer = ""
        self.input_action = None
        self.input_item = None
        # The first row on screen. The window moves over the rows of the
        # layer; it never moves past either end.
        self.scroll = 0
        # The filter in force, and the one being typed. The draft is what
        # the user is typing; the query is what has been accepted.
        self.filter_query = ""
        self.filter_draft = ""
        self.filtering = False
        # The navigatr, when it is open. It holds the tree of the session
        # and the cursor on it; the layers are untouched while it is.
        self.navigatr = None
        # Which spaces are folded in the navigatr, by workspace id.
        #
        # The state lives here and not in the rows, because the rows are
        # rebuilt every time the navigatr is opened: a fold that lived in a
        # row would be forgotten the moment the user closed the navigatr and
        # opened it again. The key is the workspace id, not the row's
        # place, so a fold survives the tree being rebuilt.
        #
        # A space that is not in here is folded by default -- except the
        # space the session is in, which is open. So the navigatr opens as
        # a list of spaces and the user opens the one they want.
        self._folds = {}
        # The spaces that are folded, for this render. It is computed once
        # per render and read by the row composer.
        self._folded = set()
        # The rearranging: the mode, what is being carried, and which way a
        # pane would be split into the tab it is dropped into.
        self._rearranging = False
        self._carried = None
        self._split = navigatr_mod.SPLIT_DIRECTIONS[0]
        # The custom layer: the user's own commands and the plugins'
        # actions. The rows are fetched when the layer is opened and kept
        # until it is opened again, so a render never costs a round trip.
        self.custom_entries = {}
        self.custom_error = ""
        self.custom_skipped = []
        # How many of the entries the layer ran out of keys for. They are
        # listed and findable, but no key runs them, so the layer says so.
        self.custom_unkeyed = 0
        # The items the tree had for the custom layer, kept: the rows that
        # are fetched are added to them, not put in their place.
        self.custom_base_items = None
        # What the last render showed, so the frame can say where we are.
        self._view_total = 0
        self._view_budget = 0
        self._view_first = 0
        self.running = True
        self._stdin = sys.stdin

    # -- rendering ----------------------------------------------------------

    def _size(self):
        """Terminal size, with a sane fallback.

        A pty that has never had a window size set reports zero rows, and
        `os.get_terminal_size` may raise. Either way the overlay must still
        render its items, so a non-positive size becomes the default rather
        than an empty list.
        """
        columns, rows = 80, 24
        try:
            size = os.get_terminal_size(self.out.fileno())
            columns, rows = size.columns, size.lines
        except (OSError, ValueError):
            pass
        if columns <= 0:
            columns = 80
        if rows <= 0:
            rows = 24
        return columns, rows

    def render(self):
        rows, inner, frame = self._layout()
        layer = self.tree.layer(self.layer_id)
        if layer is None:
            return

        self.out.write("\x1b[H\x1b[2J")  # home, clear
        # The cursor is hidden while the overlay draws a list. Every line
        # it writes ends with a newline, so the cursor is parked on the
        # row *after* the frame -- outside the panel, where it blinks as
        # a block under the window and reads as a piece of the overlay
        # that was left behind.
        #
        # It is shown again when the user is typing -- in the input
        # state or in the filter -- and it is put where the typing is:
        # see `_cursor_home`.
        typing = self._typing_line(inner, layer)
        self.out.write(SHOW_CURSOR if typing else HIDE_CURSOR)

        if frame:
            self.out.write(self._top_border(inner) + NL)

        body = self._body(layer, inner, rows, frame)
        detail = self._detail_region(inner)
        if frame:
            # The frame fills the popup. The bottom border is on the last
            # row of the window, and the space between the borders is the
            # panel's body -- which is where the keys at the bottom of
            # Herdr's own navigation surface live, and where ours do too.
            body = self._fill_body(body, rows - FRAME_ROWS - len(detail), inner)
            body = body + detail
        else:
            # Without a frame the hints are a line of their own, at the
            # bottom of the window, where they were before.
            body = self._fill_body(body, rows - PLAIN_ROWS, inner)
            body.append(self._plain_footer(inner))
        for line in body:
            self.out.write(self._frame_row(line, inner, frame) + NL)

        if frame:
            self.out.write(self._bottom_border(inner) + NL)

        if typing:
            # The cursor belongs on the line the user is typing on, not
            # under the frame.
            self.out.write(self._cursor_home(inner, layer, frame))

        self.out.flush()
        if self.input_prompt is None:
            # The message is a reply to the key that was pressed, not a
            # permanent line: it is shown once.
            self.message = ""

    def _typing_line(self, inner, layer):
        """The line the user is typing on, or "" when they are not typing.

        There are two: the input state (a rename, a new name) and the
        filter (the query that narrows the rows). Both are a line of
        text with the cursor at the end of it.
        """
        if self.input_prompt is not None:
            prompt, buffer, separator = self._input_line(inner)
            return ROW_INDENT + prompt + separator + buffer
        if self.filtering:
            room = inner - table.visible_width(FILTER_KEY) - 1
            buffer = truncate(self.filter_draft, max(0, room))
            separator = " " if room > 0 else ""
            return FILTER_KEY + separator + buffer
        return ""

    def _cursor_home(self, inner, layer, frame):
        """The escape sequence that puts the cursor on the typing line.

        Every line the overlay writes ends with a newline, so the cursor
        would be parked under the frame -- outside the panel, where it
        blinks as a block. When the user is typing, the cursor says
        where they are: at the end of what they have typed.
        """
        text = self._typing_line(inner, layer)
        if not text:
            return ""
        # The first line of the body is the hint, and the typing line is
        # the one under it.
        hint = self._hint_text(layer)
        row = (2 if frame else 1) + (1 if hint else 0)
        column = (2 if frame else 1) + table.visible_width(text)
        return "\x1b[%d;%dH" % (row, column)

    def _hint_text(self, layer):
        """The hint line above the rows, whatever is on the screen."""
        if self.navigatr is not None:
            return AGENTS_HINT if self._is_agents() else NAVIGATR_HINT
        return layer.hint if layer is not None else ""

    def _detail_rows(self, rows=None):
        """How many rows the detail region takes.

        Only the navigatrs have one. A layer is a menu, and a menu has
        nothing to say about a row under a cursor it does not have.
        """
        if self.navigatr is None:
            return 0
        if rows is None:
            rows = self._size()[1]
        if rows < MIN_DETAIL_HEIGHT:
            return 0
        return DETAIL_ROWS

    def _plain_text(self, text):
        """Text from outside the plugin, without the escape sequences in it.

        A pane's title is set by whatever program runs in the pane, and
        Herdr reports it as it is -- so the label is not the only place
        the outside text reaches the terminal: the description of a row,
        the path in the detail region, the directory, the error the
        server sent and the message it answered with are all the same
        kind of thing. An escape sequence in any of them can move the
        cursor, clear the screen, or close the colour run of the band
        under the row and leave the row unreadable.

        Only the *outside* text is stripped. The overlay's own colours
        are applied after this, so they survive.
        """
        return table.strip_ansi(str(text))

    def _detail_region(self, inner):
        """The lines under the list: where the cursor is, and where it works.

        The path of the row under the cursor -- the space, the tab and the
        row's own name, in full -- and the directory it is in. The row
        cannot carry them: its own name is cut to fit and the directory is
        not in it at all.
        """
        if self.navigatr is None:
            return []
        rows = self._navigatr_visible()
        if not rows:
            return []
        row = rows[min(self.navigatr.cursor, len(rows) - 1)]
        lines = []
        # The path and the directory are the session's own data -- the
        # names of the space, the tab and the pane, and the directory the
        # pane is in -- so the escape sequences are stripped out of them
        # as well. A pane's title can hold one, and a cursor move in the
        # middle of this line pushes the frame's border out of the panel.
        if row.path:
            lines.append(
                "%s%s%s"
                % (
                    self.palette.hint,
                    truncate(self._plain_text(row.path), inner),
                    self.palette.reset,
                )
            )
        if row.cwd:
            lines.append(
                "%s%s%s"
                % (
                    self.palette.chord,
                    truncate(self._plain_text(row.cwd), inner),
                    self.palette.reset,
                )
            )
        while len(lines) < DETAIL_ROWS:
            lines.append("")
        return lines[:DETAIL_ROWS]

    def _fill_body(self, body, height, inner):
        """The body, padded or cut to exactly `height` lines.

        The frame is the window, so the body has to be exactly as tall as
        the space between the two borders: a body that is shorter is padded
        with blank lines, and one that is longer is cut. A panel whose
        bottom border is half way down the popup leaves the keys in the
        middle of the window, which is where the user does not look for
        them.
        """
        if height <= 0:
            return []
        if len(body) > height:
            return body[:height]
        return body + [""] * (height - len(body))

    def _layout(self):
        """(rows, inner, frame) for the terminal as it is right now.

        A line exactly as wide as the terminal leaves the cursor in the last
        column, and the next character wraps it to column 0 of a new row:
        everything after it lands a column to the left. Every line is
        therefore capped one column short of the terminal's width.

        The frame is drawn *inside* that budget: its two borders are part
        of the line, so the content gets two columns less.
        """
        columns, rows = self._size()
        width = max(1, columns - 1)
        frame = self.visuals.frame and width >= MIN_FRAME_WIDTH
        inner = width - 2 if frame else width
        return rows, inner, frame

    def _body(self, layer, inner, rows, frame):
        """The lines inside the frame: the hint, the messages and the rows."""
        if self.navigatr is not None:
            return self._navigatr_body(inner, rows, frame)

        if self.input_prompt is not None:
            # The input state shows no list, so the frame has no position to
            # report: the badge of the layer we came from would be a lie
            # about a list that is not on the screen.
            self._view_total = 0
            self._view_budget = 0
            self._view_first = 0
            return self._input_body(layer, inner)

        items = self._visible_items(layer)
        lines = self._layer_chrome(layer, inner, items)

        # The rows that fit. The chrome is counted, not guessed: the frame's
        # two borders (or the plain footer) and the lines above are rows on
        # the screen too, and a row drawn over the bottom border would push
        # that border off the screen.
        budget = self._budget(rows, len(lines), frame)
        start, end = self._window(items, budget)
        self._view_total = len(items)
        self._view_budget = budget
        self._view_first = start + 1

        # Every row is measured, not only the visible ones: the columns then
        # stay where they are while the list scrolls, instead of jumping on
        # every step.
        table_rows = [self._item_row(item) for item in items]
        lines.extend(
            table.render(table_rows, ITEM_COLUMNS, inner, indent=ROW_INDENT)[
                start:end
            ]
        )
        return lines

    def _navigatr_body(self, inner, rows, frame):
        """The lines of the navigatr: the tree of the session, in the frame.

        The chrome is counted against the screen exactly as it is for a
        layer, so the frame's bottom border is never pushed off it.
        """
        items = self._navigatr_visible()
        lines = self._navigatr_chrome(inner, items)

        budget = self._budget(rows, len(lines), frame)
        start, end = self._navigatr_window(items, budget)
        self._view_total = len(items)
        self._view_budget = budget
        self._view_first = start + 1

        # The rows are composed one by one, not measured into columns.
        #
        # The aligned table renderer is for the layers, where the columns
        # SHOULD line up. A tree is the opposite: the indentation IS the
        # alignment, so every row has a different width and a table would
        # only flatten the tree back into a wall of labels.
        #
        # The agents list is flat, so it is composed by another composer:
        # no tree markers, no kind glyph, and the status is spelled out.
        numbers = self._navigatr_numbers(items)
        compose = self._agent_line if self._is_agents() else self._navigatr_line
        lines.extend(
            compose(row, index, inner, numbers.get(index, ""))
            for index, row in list(enumerate(items))[start:end]
        )
        return lines

    def _is_agents(self):
        """True when the open navigatr is the agents list."""
        return (
            self.navigatr is not None
            and self.navigatr.kind == navigatr_mod.AGENTS
        )

    def _navigatr_numbers(self, rows):
        """The rows that are numbered 1-9, in the order they are shown.

        In the places tree only the spaces are numbered: the point is to
        jump between them, and a number on every row would be noise. In the
        agents list every row is an agent, so every row is numbered.

        The numbers follow the FILTER, so a number always names a row the
        user can actually see - and the number is drawn on the row, so
        there is nothing to memorise.
        """
        numbers = {}
        count = 0
        agents = self._is_agents()
        for index, row in enumerate(rows):
            if not agents and row.kind != navigatr_mod.WORKSPACE:
                continue
            count += 1
            if count > 9:
                break
            numbers[index] = "%d" % count
        return numbers

    def _navigatr_chrome(self, inner, items):
        """The lines around the tree: the hint, the filter and the errors.

        The navigatr has the same chrome as a layer -- the same hint line,
        the same filter line, the same messages -- so it reads as the same
        program rather than as something else that happens to be drawn in
        the same frame.
        """
        hint = AGENTS_HINT if self._is_agents() else NAVIGATR_HINT
        lines = self._chrome_lines(hint, inner, items)

        error = self.navigatr.error if self.navigatr is not None else ""
        if error:
            lines.append(
                "%s%s%s"
                % (
                    self.palette.err,
                    truncate(self._plain_text(error), inner),
                    self.palette.reset,
                )
            )
        elif not items and not self._query():
            # Nothing at all: say so plainly, rather than showing an empty
            # box that looks like a broken overlay.
            empty = (
                "no agents in this session"
                if self._is_agents()
                else "no workspaces in this session"
            )
            lines.append(
                "%s%s%s"
                % (
                    self.palette.err,
                    truncate(empty, inner),
                    self.palette.reset,
                )
            )
        return lines

    def _folded_spaces(self, rows):
        """The spaces whose rows are hidden, by workspace id.

        A space is folded when the user said so, or when the user has not
        said anything about it and it is not the space the session is in.
        So the navigatr opens as a list of spaces, with the current one
        open, and the user opens the others as they want them.

        **A filter suspends the folds.** A filter is an explicit request to
        see something, and a place the filter cannot reach is a place the
        user cannot go to. So while a filter is in force every space is
        open, and the folds come back when the filter is cleared.
        """
        if self._query():
            return set()
        focused = ""
        for row in rows:
            if row.kind == navigatr_mod.WORKSPACE and row.current:
                focused = row.params.get("workspace_id", "")
                break
        folded = set()
        for row in rows:
            if row.kind != navigatr_mod.WORKSPACE:
                continue
            space_id = row.params.get("workspace_id", "")
            if self._folds.get(space_id, space_id != focused):
                folded.add(space_id)
        return folded

    def _folded_rows(self, rows):
        """The rows of the tree with the folded spaces' children hidden.

        The space's own row stays: it is the row that says the space is
        there, and it is what the numbers aim at. Only the rows under it go.

        A row at the top level -- a tab whose space is gone, a loose pane --
        is not under any space, so it is never hidden. That is what the
        depth is for: the children of a space are the rows under it.
        """
        folded = self._folded_spaces(rows)
        self._folded = folded
        if not folded:
            return rows
        out = []
        hiding = False
        for row in rows:
            if row.depth == 0:
                hiding = (
                    row.kind == navigatr_mod.WORKSPACE
                    and row.params.get("workspace_id", "") in folded
                )
                out.append(row)
                continue
            if hiding:
                continue
            out.append(row)
        return out

    def _navigatr_visible(self):
        """The navigatr's rows, narrowed by the filter, cursor clamped.

        **The filter keeps the structure.** A space or a tab row is a
        heading, and it is shown when one of the rows under it survived
        the filter: a tree without its headings is a list, and the list
        is what the agents navigatr is for.

        The cursor is clamped here, every time the rows are asked for:
        the filter can shrink the list under it, and a cursor past the
        end of the list points at nothing. It is also moved off a
        heading: the cursor never stops on one.
        """
        if self.navigatr is None:
            return []
        query = self._query().lower()
        rows = self._folded_rows(self.navigatr.rows)
        keep = [query in row.search.lower() for row in rows]
        if not all(keep):
            # A heading survives when one of the rows under it does. The
            # rows are a flat list in tree order, so a row's parent is
            # the nearest row above it with a smaller depth.
            for index in range(len(rows) - 1, -1, -1):
                parent = self._navigatr_parent(rows, index)
                if parent >= 0 and keep[index]:
                    keep[parent] = True
        visible = [row for row, shown in zip(rows, keep) if shown]
        if not visible:
            self.navigatr.cursor = 0
        else:
            self.navigatr.cursor = self._selectable_row(
                visible, max(0, min(self.navigatr.cursor, len(visible) - 1))
            )
        return visible

    def _navigatr_parent(self, rows, index):
        """The index of the row's parent, or -1 for a top-level row.

        The tree is a flat list of rows in tree order, so a row's parent
        is the nearest row above it with a smaller depth.
        """
        depth = rows[index].depth
        for candidate in range(index - 1, -1, -1):
            if rows[candidate].depth < depth:
                return candidate
        return -1

    def _navigatr_selectable(self, row):
        """True when the cursor can stop on this row.

        A tab is a heading, not a destination, so the cursor never stops on
        one. While rearranging it is the other way round: a tab is exactly
        what the user wants to aim at -- the tab a pane is dropped into, or
        the tab another tab takes the place of. So the tab rows become
        targets for as long as the rearranging is on.
        """
        if row.selectable:
            return True
        return bool(self._rearranging) and row.kind == navigatr_mod.TAB

    def _selectable_row(self, items, index):
        """The index of the row the cursor can stop on, nearest `index`.

        The tab rows are headings, not destinations, so the cursor never
        stops on one. If the row it was on is a heading -- the filter can
        leave it there -- it moves to the nearest row it can stop on: the
        row below it first, because a heading is followed by the rows it
        groups.
        """
        if 0 <= index < len(items) and self._navigatr_selectable(items[index]):
            return index
        for candidate in range(index + 1, len(items)):
            if self._navigatr_selectable(items[candidate]):
                return candidate
        for candidate in range(index - 1, -1, -1):
            if self._navigatr_selectable(items[candidate]):
                return candidate
        return index

    def _cursor_to_the_first_match(self):
        """Put the cursor on the first row the filter matched.

        The filter keeps the structure, so the list under it is longer
        than the list of matches: the space and the tab above a pane that
        matched are shown as well, so the user can see where the pane
        is. The cursor goes to what matched.

        A match that is a heading -- a tab -- is not a place, so the
        cursor goes to the first place under it: the panes it groups. A
        tab with nothing under it has nowhere to go, and the overlay
        says so instead of quietly going somewhere else.
        """
        if self.navigatr is None:
            return
        query = self.filter_query.lower()
        if not query:
            return
        rows = self._navigatr_visible()
        for index, row in enumerate(rows):
            if query not in row.search.lower():
                continue
            if self._navigatr_selectable(row):
                self.navigatr.cursor = index
                return
            # A heading: the first place under it, if there is one.
            for candidate in range(index + 1, len(rows)):
                if self._navigatr_selectable(rows[candidate]):
                    self.navigatr.cursor = candidate
                    return
            # Nothing under it: the cursor cannot go there, and the user
            # is told why instead of being sent somewhere else.
            self.navigatr.cursor = self._selectable_row(rows, index)
            self.message = "that row is a heading, not a place"
            self.message_kind = "err"
            return

    def _navigatr_window(self, items, budget):
        """The slice of the tree on screen, with the cursor in the window.

        The cursor is the position here, so the window follows it: it moves
        when the cursor would otherwise be off the screen, and it never
        moves past either end.
        """
        limit = max(0, len(items) - budget)
        start = max(0, min(self.scroll, limit))
        cursor = self.navigatr.cursor if self.navigatr is not None else 0
        if cursor < start:
            start = cursor
        elif cursor >= start + budget:
            start = cursor - budget + 1
        start = max(0, min(start, limit))
        self.scroll = start
        return start, start + budget

    def _move_cursor(self, delta):
        """Move the cursor by `delta` rows, and stop at either end.

        The headings are skipped: they are not destinations, and a tab
        always has at least one pane, so there is nothing to go to by
        stopping on one. A page of rows is a page of *destinations*, not
        a page of lines.
        """
        if self.navigatr is None:
            return
        items = self._navigatr_visible()
        if not items:
            return
        index = self.navigatr.cursor
        if not 0 <= index < len(items) or not self._navigatr_selectable(items[index]):
            index = self._selectable_row(items, index)
        step = 1 if delta > 0 else -1
        left = abs(delta)
        while left > 0:
            candidate = index + step
            while (
                0 <= candidate < len(items)
                and not self._navigatr_selectable(items[candidate])
            ):
                candidate += step
            if not 0 <= candidate < len(items):
                # There is nothing further in that direction.
                break
            index = candidate
            left -= 1
        self.navigatr.cursor = index

    def _navigatr_line(self, row, index, width, number=""):
        """One row of the tree, as ONE composed line.

        The row is not a set of table columns. The marker and the content
        it marks move together: the kind of the row, the marker of the
        current one and the name all start one column after the end of
        that row's own branch marker -- the glyph's column is the
        marker's end column plus one.

        That is what the depth of a row used to be missing. The markers
        were a narrow column on the left and the content was a flat wall
        of labels on the right, so the eye had to join the two up, and the
        tree converged instead of opening outward. Here the indentation
        carries the depth, which is what a tree is.
        """
        on_band = self.navigatr is not None and index == self.navigatr.cursor
        # A row on the band is ONE run: the reset that closes a colour
        # closes the background too, so the parts of a band row are not
        # closed between them. `close` is what says whether a part closes
        # its own colour, and it is False on the band -- the band is
        # closed once, at the end of the row, by `_band`.
        #
        # It matters for the parts that are painted *inside* the row:
        # the kind glyph and the marker of the current row. A reset after
        # either of them ended the band there, and the rest of the row
        # was drawn in the panel's background colour on the terminal's own
        # background: invisible. The band was a short stripe behind the
        # first two columns and the row under the cursor was gone.
        close = not on_band
        cursor = self._cursor_marker(index, on_band)
        # The kind of the row: a space, a tab or a pane. The agent is not a
        # row of its own any more -- it is folded into the pane that hosts
        # it. The glyph is the font's; the word is the fallback for a
        # terminal without it, because the tree's shape does not say what a
        # row is.
        #
        # The colour says how deep the row is: the deeper, the brighter,
        # so the leaves -- the things you can jump to -- stand out and
        # the structure recedes.
        kind = self.visuals.group_icon(row.kind) or self.visuals.kind_label(row.kind)
        kind = self._paint(
            kind,
            self._row_colour(self.visuals.kind_colour(row.kind), on_band),
            close,
        )
        current = " "
        if row.current:
            # The arrow that marks the current space, tab and pane. It is a
            # plain character, like the tree's markers, so it survives
            # `icons = false`: the tree is information, not decoration.
            #
            # The space is kept when there is no arrow, so the names of the
            # rows at one depth start in the same column.
            current = self._paint(
                self.visuals.marker("arrow") or DEFAULT_ARROW,
                self._row_colour(self.palette.accent, on_band),
                close,
            )
        status = ""
        status_colour = ""
        if row.status:
            # The status belongs to its row, so it belongs next to the
            # name: a status at the far right of the frame is forty
            # columns away from the thing it describes.
            #
            # The separator is only there when there is a glyph: a status
            # with no glyph of its own would leave two spaces at the end
            # of the row, and they would be counted in the width budget.
            category = navigatr_mod.status_category(row.status)
            glyph = self.visuals.status_icon(category)
            if glyph:
                status = "  %s" % glyph
                status_colour = self.visuals.status_colour(category)
        # The label is the session's own data: a pane title can hold an
        # escape sequence, and a program running in a pane could colour
        # the overlay. The trust boundary moved when the tree started
        # drawing socket data instead of only the user's own layers.toml,
        # so the colour runs are stripped out of the label -- and only out
        # of the label, because the overlay's own colours have to survive.
        label = self._plain_text(row.label)
        # The number has a slot of its own, two columns wide, and the slot
        # is there whether the row has a number in it or not. It used to
        # sit at the front of the label, and that pushed the name of a
        # space two columns to the right of the name of the tab under it:
        # the tree's names no longer marched with its depth. A number in
        # a column of its own is a column, and the names march.
        number_slot = self._paint(
            "%s " % number if number else "  ",
            self._row_colour(self.palette.chord, on_band),
            close,
        )
        prefix = "%s%s %s%s %s %s " % (
            ROW_INDENT,
            cursor,
            number_slot,
            self._tree_cell(row),
            kind,
            current,
        )
        # What the row holds: the counts of a space or a tab, the agent of
        # a pane. It is context, not content, so it is drawn in the muted
        # colour: the name is what the row is for.
        #
        # It is the session's own data too -- the agent's name and the
        # names of the space and the tab -- so it is stripped like the
        # label is.
        desc = "  %s" % self._plain_text(row.desc) if row.desc else ""

        # The status and the description are *dependent* elements: they
        # are dropped before the label is cut, so the row ends cleanly
        # instead of in a bare ellipsis. "A note that outlives its chord
        # is worse than no note", and a status cut off at the edge of the
        # frame is a status the user cannot read.
        #
        # The status goes first and the description next: the description
        # is what the row holds, the status is how it is doing. The number
        # gives up its room after them and before the label is cut.
        #
        # The parts are listed in the order they are drawn -- the
        # description, then the status -- and carry the order they are
        # given up as their priority.
        parts = []
        if desc:
            parts.append(
                (desc, 0, False, self._row_colour(self.palette.hint, on_band), False)
            )
        if status:
            # The status is drawn at the right-hand end of the row, so the
            # glyphs of a whole list line up: that is what makes them a
            # column the eye can scan instead of a colour it has to find
            # in every line.
            parts.append(
                (status, 1, False, self._row_colour(status_colour, on_band), True)
            )
        # The number is in the prefix, in a slot of its own, so there is
        # no number to give up here.
        label, kept = self._fit_row(prefix, label, parts, width)
        used = (
            table.visible_width(prefix)
            + table.visible_width(label)
        )
        line = "%s%s%s" % (
            prefix,
            self._paint(
                label,
                self._row_colour(self._name_colour(row, index), on_band),
                close,
            ),
            self._draw_parts(kept, used, width, close),
        )
        # A row is one line, and one line is at most as wide as the frame's
        # inside: a line as wide as the terminal wraps the cursor, and
        # every line after it lands a column to the left.
        #
        # The cut does not close the colour run on a band row: the band is
        # closed once, at the end of the row, and a reset in the middle of
        # it ends the band and leaves the rest of the row off it.
        return self._band(
            table.truncate(line, width, close=not on_band), width, on_band
        )

    def _cursor_colour(self, index):
        """The colour of the row the cursor is on, or "".

        The name of the row under the cursor is drawn in the accent, so
        the row you are about to act on is the one that stands out. It is
        a deliberate run, and it is closed again by `_paint` -- the
        cursor's colour used to run on into the rest of the row.
        """
        if self.navigatr is not None and index == self.navigatr.cursor:
            return self.palette.accent + self.palette.bold
        return ""

    def _name_colour(self, row, index):
        """The colour a row's name is drawn in.

        **The three tiers of the tree are told apart by their colour, not
        only by their markers.** The space is the section title and is in
        the accent; the tab is the group heading and is muted; the pane is
        the item the user jumps to and is in the plain colour.

        Without it the three of them are a wall of labels in the same
        colour, and the eye has to work out the structure of the tree row
        by row -- which is the one thing a tree is for.

        A tab that is a destination -- one whose space is gone, and which
        is therefore the only row that names that place -- is not a
        heading, and it is drawn like the pane it is: it is a place.
        """
        colour = self._cursor_colour(index)
        if colour:
            return colour
        if row.kind == navigatr_mod.WORKSPACE:
            # A space is a section: the loudest thing on its own branch.
            return self.palette.accent + self.palette.bold
        if not row.selectable:
            # A heading, not a place: it recedes.
            return self.palette.hint
        return ""

    def _agent_line(self, row, index, width, number=""):
        """One row of the agents list, as ONE composed line.

        The list is flat: no tree markers, no branch, no indentation. Every
        row is an agent, so a kind glyph on every row would be noise.

        The status is a glyph *and* a word. The glyph alone needs the
        vocabulary to be read, and the word alone needs the eye to read
        seven characters on every row. The word is the category itself:
        blocked, working, done, idle or unknown.

        The row is marked when it is the agent that is focused, the same
        way the tree marks the space, tab and pane that are current: the
        marker is not decoration, it says which one you are on.
        """
        on_band = self.navigatr is not None and index == self.navigatr.cursor
        # A row on the band is ONE run: the reset that closes a colour
        # closes the background too. The marker of the focused agent is
        # painted inside the row, so it must not close its own colour
        # there -- a reset after it ended the band, and the rest of the
        # row was drawn in the panel's background colour on the
        # terminal's own background: invisible.
        close = not on_band
        cursor = self._cursor_marker(index, on_band)
        current = " "
        if row.current:
            # The arrow that marks the focused agent. It is a plain
            # character, like the tree's markers, so it survives
            # `icons = false`.
            current = self._paint(
                self.visuals.marker("arrow") or DEFAULT_ARROW,
                self._row_colour(self.palette.accent, on_band),
                close,
            )
        category = navigatr_mod.status_category(row.status)
        glyph = self.visuals.status_icon(category)
        # The status is the thing the eye should go to: an agent that is
        # blocked wants the user now. So the status is the one thing in
        # the list that is not a shade of the accent.
        status_colour = self.visuals.status_colour(category)
        # The number has a slot of its own, two columns wide, and the slot
        # is there whether the row has a number in it or not: a row with
        # no number must not shift the rest of the row. The status is a
        # column -- that is what makes it scannable -- and a column that
        # moves two columns to the left after the ninth agent is not a
        # column.
        #
        # The number is also the identity of the row, and the footer
        # promises the keys that name it, so it is not given up when the
        # row is narrow: the name of the agent is cut instead.
        number_slot = self._paint(
            "%s " % number if number else "  ",
            self._row_colour(self.palette.chord, on_band),
            close,
        )
        # The slot carries its own space, so the head starts in the same
        # column whether the row has a number in it or not.
        prefix = "%s%s %s %s" % (ROW_INDENT, cursor, current, number_slot)
        # The label is the session's own data, so the colour runs are
        # stripped out of it: only out of the label, because the overlay's
        # own colours have to survive.
        label = self._plain_text(row.label)
        # The description is where the agent is: a dependent element, and
        # the first to go. It is drawn in the dimmed accent, a step
        # brighter than the counts in the tree: the place is worth more
        # than a count of things.
        #
        # It is the session's own data too -- the names of the space and
        # the tab -- so it is stripped like the label is.
        desc = "  %s" % self._plain_text(row.desc) if row.desc else ""

        # The parts are listed in the order they are drawn: the glyph and
        # the word before the label, the description after it. Their
        # priorities are the order they are given up in: the location
        # first, then the word -- which is eight columns and the glyph is
        # one -- and then the glyph itself. The name of the agent is the
        # last thing to lose anything: it is the row.
        parts = []
        if glyph:
            parts.append(
                ("%s " % glyph, 0, True, self._row_colour(status_colour, on_band), False)
            )
        if category:
            # The word is padded to the width of the longest one, so the
            # words line up and the colours form a column. The vocabulary
            # is fixed -- blocked, working, done, idle, unknown -- so the
            # width is too.
            parts.append(
                (
                    "%s " % category.ljust(STATUS_WORD_WIDTH),
                    1,
                    True,
                    self._row_colour(status_colour, on_band),
                    False,
                )
            )
        if desc:
            parts.append(
                (
                    desc,
                    2,
                    False,
                    self._row_colour(self.palette.chord, on_band),
                    True,
                )
            )
        # The number is in the prefix, in a slot of its own, so there is
        # no number to give up here.
        label, kept = self._fit_row(prefix, label, parts, width)
        head = "".join(
            self._paint(part[0], part[3], close) for part in kept if part[2]
        )
        line = "%s%s%s" % (
            prefix,
            head,
            self._paint(
                label,
                self._row_colour(self._cursor_colour(index), on_band),
                close,
            ),
        )
        # The location goes at the right-hand end of the row, so the
        # places line up and the eye can compare them.
        used = (
            table.visible_width(prefix)
            + table.visible_width(head)
            + table.visible_width(label)
        )
        tail = self._draw_parts(
            [part for part in kept if not part[2]], used, width, close
        )
        return self._band(
            table.truncate(line + tail, width, close=not on_band), width, on_band
        )

    def _cursor_marker(self, index, on_band=False):
        """The marker that says which row the cursor is on.

        The cursor is a marker, not an index: the user has to be able to
        see where they are without counting rows.

        The colour is closed again here. Without the reset the accent ran
        on into the rest of the row, and the tree markers, the glyph and
        the name of the row under the cursor were all drawn in it.
        """
        if self.navigatr is not None and index == self.navigatr.cursor:
            if on_band:
                # The band is the highlight, so the marker does not need
                # the bold -- and a bold that was not closed would run on
                # into the rest of the row.
                return "%s>" % self._row_colour(self.palette.accent, on_band)
            return "%s%s%s%s" % (
                self._row_colour(self.palette.accent, on_band),
                self.palette.bold,
                ">",
                self.palette.reset,
            )
        return " "

    def _fit_row(self, before, label, parts, width):
        """The parts of a row that fit in `width` columns.

        `before` is the fixed text at the left of the row. `parts` is a
        list of (text, priority, head, colour, right): the text is drawn
        in the order the list is in, `priority` says what is given up
        first -- the highest number goes first -- `head` says whether the
        part is drawn before the label or after it, `colour` is the
        escape sequence the part is drawn in, and `right` says whether
        the part is drawn at the right-hand end of the row instead of
        after the label.

        A part at the right-hand end is the one that makes a column: the
        status glyphs of a whole list line up, so the eye can scan them
        instead of reading every row. It is drawn last, in the space
        that is left over, and it is given up before anything else when
        the row does not fit.

        The number is not here: it is in a column of its own, in the
        prefix, so it is never given up and the names of the rows march
        with the depth of the tree.

        Returns (label, kept): the label that fits, and the parts that
        survived, in the order they are drawn.
        """
        kept = [part for part in parts if part[0]]

        def measure():
            return (
                table.visible_width(before)
                + table.visible_width(label)
                + sum(
                    table.visible_width(part[0])
                    for part in kept
                    if part[2]
                )
                + sum(
                    table.visible_width(part[0])
                    for part in kept
                    if not part[2]
                )
            )

        while kept and measure() > width:
            # The part with the highest priority is the one that goes.
            worst = max(kept, key=lambda part: part[1])
            kept.remove(worst)

        room = width - table.visible_width(before)
        room -= sum(table.visible_width(part[0]) for part in kept)
        return table.truncate(label, max(1, room)), kept

    def _draw_parts(self, parts, used, width, close=True):
        """The parts of a row, drawn in the right places.

        A part marked `right` goes at the right-hand end of the row, in
        the space that is left over: that is what makes a column of
        them. The parts that are not marked are drawn after the label,
        in the order they are given.

        `used` is the width of what has already been drawn -- the
        marker, the number and the label -- so the gap is the space that
        is really left.
        """
        middle = [part for part in parts if not part[4]]
        right = [part for part in parts if part[4]]
        text = "".join(self._paint(part[0], part[3], close) for part in middle)
        if not right:
            return text
        taken = (
            used
            + table.visible_width(text)
            + sum(table.visible_width(part[0]) for part in right)
        )
        gap = max(0, width - taken)
        return "%s%s%s" % (
            text,
            " " * gap,
            "".join(self._paint(part[0], part[3], close) for part in right),
        )

    def _band(self, line, width, on_band):
        """The row, on the band under the selected row.

        The band is the accent, and it is the full width of the panel:
        Herdr's own navigation surface draws the selected row that way,
        and it is the clearest statement of "this is the one you are on"
        a terminal can make.

        The line is padded to the full width *inside* the band, so the
        band is a band and not a stripe behind the words.

        **The band is a background, and the reset that closes a colour
        closes the background too.** So the row on the band is ONE run:
        the band opens it and the band closes it, here. Every part of
        the row is drawn without closing its own colour -- that is what
        `close` says to `_paint` -- so there is no reset inside the row
        to end the band early, and the text after it would be drawn in
        the panel's background colour on the terminal's own background:
        invisible, with a short stripe of band left behind.
        """
        if not on_band:
            return line
        padded = line + " " * max(0, width - table.visible_width(line))
        return "%s%s%s" % (self.palette.selection(), padded, self.palette.reset)

    def _row_colour(self, colour, on_band):
        """The colour a part of the row is drawn in.

        On the selected row every part is drawn in the panel's background
        colour, because the band under it is the accent: light text on a
        light band is unreadable, and Herdr's own navigation surface does
        exactly this.
        """
        if on_band:
            return self.palette.on_selection()
        return colour

    def _paint(self, text, colour, reset=True):
        """The text in the colour, with the colour closed again.

        A colour that is opened and not closed runs on into everything
        after it: the tree markers, the glyph and the name of the row
        under the cursor were all drawn in the accent once, because the
        cell did not close the run. So every colour the overlay opens is
        closed here, in one place.

        A row on the band is the exception: the band is a *background*,
        and the reset that closes a colour closes the background too, so
        the band would end at the first part of the row. The parts of a
        band row are all in the same colour, so they are not closed
        between them: the band is closed once, at the end of the row.
        """
        if not text or not colour:
            return text
        if not reset:
            return "%s%s" % (colour, text)
        return "%s%s%s" % (colour, text, self.palette.reset)

    def _tree_cell(self, row):
        """A row's branch markers: the tree, drawn with the plugin's glyphs.

        The markers are the tree's own -- the herdr-navigator's, so the two
        read as one family -- and they are box-drawing characters rather
        than Nerd Font icons: the structure of the tree is not decoration,
        so it survives `icons = false`.

        The cell is as wide as the depth of the row and no wider. The
        branch marker of a space is one column, so it is not padded: a
        padded cell would put the content of a space and of the tab under
        it in the same column, and the tree would close up again.
        """
        tee = self.visuals.marker("tee") or DEFAULT_TEE
        elbow = self.visuals.marker("elbow") or DEFAULT_ELBOW
        branch = self.visuals.marker("branch") or DEFAULT_BRANCH

        width = max(len(tee), len(elbow))
        bar = FRAME_VERTICAL + " " * max(0, width - 1)
        blank = " " * width

        if row.depth == 0:
            if (
                row.kind == navigatr_mod.WORKSPACE
                and row.params.get("workspace_id", "") in self._folded
            ):
                # A folded space says so: its rows are hidden, and the
                # marker is what says that without a word.
                return self.visuals.marker("collapsed") or DEFAULT_COLLAPSED
            return branch
        parts = []
        for last in row.ancestors_last:
            parts.append(blank if last else bar)
        marker = elbow if row.is_last else tee
        parts.append(marker + " " * max(0, width - len(marker)))
        return "".join(parts)

    def _input_body(self, layer, inner):
        """The lines of the input state: the prompt and what is being typed."""
        lines = []
        if layer.hint:
            lines.append(
                "%s%s%s"
                % (
                    self.palette.hint,
                    truncate(ROW_INDENT + layer.hint, inner),
                    self.palette.reset,
                )
            )
        prompt, buffer, separator = self._input_line(inner)
        lines.append(
            "%s%s%s%s%s%s%s"
            % (
                self.palette.accent,
                prompt,
                self.palette.reset,
                separator,
                self.palette.bold,
                buffer,
                self.palette.reset,
            )
        )
        lines.append(
            "%s%s%s"
            % (
                self.palette.hint,
                truncate("end to confirm · esc to cancel", inner),
                self.palette.reset,
            )
        )
        return lines

    def _chrome(self, layer, inner, items):
        """The lines around the rows: the hint, the filter and the messages.

        They are counted against the screen, so the number of rows that fit
        depends on what is here.
        """
        return self._chrome_lines(layer.hint if layer is not None else "", inner, items)

    def _layer_chrome(self, layer, inner, items):
        """The lines around the rows of a layer, the custom layer's included.

        The custom layer has things to say for itself: that there is
        nothing to run, that the plugins' actions could not be read, or
        that some of them were left out. Those lines are rows on the
        screen like any other, so they have to be in the count that decides
        how many rows fit -- otherwise the window is k rows too large and
        the last k rows can never be scrolled into view.
        """
        lines = self._chrome(layer, inner, items)
        if self.layer_id == custom_mod.LAYER_ID:
            lines.extend(self._custom_lines(inner, items))
        return lines

    def _chrome_lines(self, hint, inner, items):
        """The chrome of a list of rows, whatever the list is.

        The navigatr has the same chrome as a layer: the same hint line,
        the same filter line and the same messages. Only the hint differs,
        because the navigatr is not a layer.
        """
        lines = []

        if hint:
            # The hint is set in from the edge, like the rows it describes.
            lines.append(
                "%s%s%s"
                % (
                    self.palette.hint,
                    truncate(ROW_INDENT + hint, inner),
                    self.palette.reset,
                )
            )

        if self.filtering or self.filter_query:
            lines.append(self._filter_line(inner))

        if self.message:
            colour = self.palette.ok if self.message_kind == "ok" else self.palette.err
            lines.append(
                "%s%s%s"
                % (
                    colour,
                    truncate(self._plain_text(self.message), inner),
                    self.palette.reset,
                )
            )

        # A key used twice in one layer makes one of the two items
        # unreachable. Say so instead of dropping one in silence.
        duplicates = sorted(set(self._duplicate_keys()))
        if duplicates:
            lines.append(
                "%s%s%s"
                % (
                    self.palette.err,
                    truncate(
                        "duplicate key: %s"
                        % ", ".join(repr(key) for key in duplicates),
                        inner,
                    ),
                    self.palette.reset,
                )
            )

        if self._query() and not items:
            # A filter that matches nothing says so. An empty box looks
            # like a broken overlay, and the user cannot tell whether the
            # filter or the layer is at fault.
            lines.append(
                "%s%s%s"
                % (
                    self.palette.err,
                    truncate("no match for %s" % self._query(), inner),
                    self.palette.reset,
                )
            )

        return lines

    def _custom_lines(self, inner, items):
        """What the custom layer has to say for itself.

        The same chrome as every other layer -- the hint, the filter, the
        messages -- plus the things that are true only of this layer: that
        there is nothing to run, that the plugins' actions could not be
        read, what was left out and why, and how many of the entries the
        layer ran out of keys for.
        """
        lines = []
        if self.custom_error:
            lines.append(
                "%s%s%s"
                % (
                    self.palette.err,
                    truncate(self.custom_error, inner),
                    self.palette.reset,
                )
            )
        elif not items and not self._query():
            # Nothing at all: say so plainly, rather than showing an empty
            # box that looks like a broken overlay.
            lines.append(
                "%s%s%s"
                % (
                    self.palette.err,
                    truncate("no custom commands and no plugin actions", inner),
                    self.palette.reset,
                )
            )
        if self.custom_skipped:
            lines.append(
                "%s%s%s"
                % (
                    self.palette.hint,
                    truncate(
                        "%d %s left out: %s"
                        % (
                            len(self.custom_skipped),
                            "entry" if len(self.custom_skipped) == 1 else "entries",
                            "; ".join(self.custom_skipped),
                        ),
                        inner,
                    ),
                    self.palette.reset,
                )
            )
        if self.custom_unkeyed:
            # The pool of keys is finite, and an install with many plugins
            # has more actions than it has characters. Those rows are
            # listed -- the filter finds them -- but no key runs them, so
            # the layer says so rather than leave rows that look runnable.
            lines.append(
                "%s%s%s"
                % (
                    self.palette.hint,
                    truncate(
                        "%d %s have no key: they cannot be run from here"
                        % (
                            self.custom_unkeyed,
                            "entry" if self.custom_unkeyed == 1 else "entries",
                        ),
                        inner,
                    ),
                    self.palette.reset,
                )
            )
        return lines

    def _filter_line(self, inner):
        """The line that shows the filter: the query, typed or applied.

        While the query is being typed it is the live part of the overlay,
        so it is drawn in the accent. Once it is applied it is still shown
        -- the user has to be able to see what is hiding rows -- but it is
        not editable until "/" is pressed again.
        """
        prompt = FILTER_KEY
        buffer = self.filter_draft if self.filtering else self.filter_query
        room = inner - table.visible_width(prompt) - 1
        buffer = truncate(buffer, max(0, room))
        separator = " " if room > 0 else ""
        if self.filtering:
            return "%s%s%s%s%s%s%s" % (
                self.palette.accent,
                prompt,
                self.palette.reset,
                separator,
                self.palette.bold,
                buffer,
                self.palette.reset,
            )
        return "%s%s%s%s%s%s%s" % (
            self.palette.hint,
            prompt,
            self.palette.reset,
            separator,
            self.palette.hint,
            buffer,
            self.palette.reset,
        )

    # -- the filter -----------------------------------------------------------

    def _query(self):
        """The filter in force: the one being typed, or the applied one."""
        return self.filter_draft if self.filtering else self.filter_query

    def _matches(self, item, query):
        """True when the item's description, its key or its chord has it."""
        return (
            query in item.desc.lower()
            or query in item.key.lower()
            or query in item.chord.lower()
        )

    def _groups(self, items):
        """The items split into groups: a heading and the rows under it."""
        groups = []
        heading = None
        group = []
        for item in items:
            if item.heading:
                if heading is not None or group:
                    groups.append((heading, group))
                heading = item
                group = []
                continue
            group.append(item)
        if heading is not None or group:
            groups.append((heading, group))
        return groups

    def _visible_items(self, layer):
        """The rows this layer shows: its items, narrowed by the filter.

        A heading is a marker, not a row: the query never matches it, and it
        is shown only while its group has a row that matches. A heading to
        nowhere is worse than no heading at all.

        A heading is shown once, for its group: the tree emits one heading
        per group, and the loop below adds it once. It is not repeated for
        every row under it.
        """
        query = self._query().lower()
        if not query:
            return list(layer.items)

        kept = []
        for heading, group in self._groups(layer.items):
            matched = [item for item in group if self._matches(item, query)]
            if not matched:
                continue
            if heading is not None:
                kept.append(heading)
            kept.extend(matched)
        return kept

    def _start_filter(self):
        """Open the filter, with the current query so it can be edited."""
        self.filtering = True
        self.filter_draft = self.filter_query
        self.scroll = 0

    def _clear_filter(self):
        """Drop the filter that is in force, and show every row again."""
        self.filtering = False
        self.filter_draft = ""
        self.filter_query = ""
        self.scroll = 0

    def _handle_filter(self, key):
        """The keys that mean something while a filter is being typed.

        A printable character is part of the query, not a command: `p`, `v`
        and `x` are actions in the layers, and while the filter is open
        they are only letters.

        The scroll keys still scroll. A filter that matches thirty rows is
        still thirty rows, and the user has to be able to reach the last
        one without accepting the filter first.
        """
        if key.name == "esc":
            # Escape clears the filter and shows everything again. It does
            # not go up a level: the filter is what it opened.
            self._clear_filter()
            return
        if key.name == "enter":
            # The filter stays in force, so the user can see what it hides.
            self.filter_query = self.filter_draft
            self.filtering = False
            self.scroll = 0
            self._cursor_to_the_first_match()
            return
        if key.name == "backspace":
            self.filter_draft = self.filter_draft[:-1]
            self.scroll = 0
            return
        # The scroll keys, before the query: they move the window over the
        # rows the query matches.
        if self._scroll_by_key(key):
            return
        if key.name == "char" and key.char.isprintable():
            self.filter_draft += key.char
            self.scroll = 0

    # -- the window -----------------------------------------------------------

    def _budget(self, rows, chrome_rows, frame):
        """How many item rows fit on the screen.

        The rows are counted from what the render actually writes: the
        frame's two borders -- or the plain footer when there is no frame
        -- the lines around the rows, the row the cursor ends up on, and
        the detail region under the list.

        That last row is the one that used to be missing. Every line the
        overlay writes ends with a newline, so the last newline leaves the
        cursor on the row *after* the last line, and a frame that filled
        the screen exactly scrolled by one: the top border, and the
        breadcrumb in it, went off the top of the screen.
        """
        used = chrome_rows + (FRAME_ROWS if frame else PLAIN_ROWS)
        used += self._detail_rows(rows)
        return max(0, rows - used)

    def _window(self, items, budget):
        """The slice of the items on screen, with the scroll clamped to it."""
        limit = max(0, len(items) - budget)
        start = max(0, min(self.scroll, limit))
        self.scroll = start
        return start, start + budget

    def _window_state(self):
        """(items, budget) for the layer as it is right now.

        The budget is the same one the render uses. It has to be: the
        scroll is clamped to it, and a clamp that is k rows too small --
        because the custom layer's own lines were left out of the count --
        makes the last k rows unreachable, and the position badge claims
        a row that can never be shown.
        """
        if self.navigatr is not None:
            items = self._navigatr_visible()
            rows, inner, frame = self._layout()
            chrome = self._navigatr_chrome(inner, items)
            return items, self._budget(rows, len(chrome), frame)
        layer = self.tree.layer(self.layer_id)
        rows, inner, frame = self._layout()
        if layer is None:
            return [], 1
        items = self._visible_items(layer)
        chrome = self._layer_chrome(layer, inner, items)
        return items, self._budget(rows, len(chrome), frame)

    def _scroll(self, delta):
        """Move the window by `delta` rows, and stop at either end."""
        items, budget = self._window_state()
        limit = max(0, len(items) - budget)
        start = max(0, min(self.scroll, limit))
        self.scroll = max(0, min(start + delta, limit))

    def _scroll_by_key(self, key):
        """Scroll for a scroll key. True when the key was one of them.

        The same keys scroll in browsing mode and while the filter is
        open: the arrows were ignored before the scroll existed, so they
        are free, and `j` and `k` are not among them because they are
        actions in the panes and navigation layers.

        In the navigatr the same keys move the cursor instead of the
        window: there the cursor is the position, and the window follows
        it.
        """
        if self.navigatr is not None:
            return self._navigatr_scroll_by_key(key)
        if key.name == "up":
            self._scroll(-1)
            return True
        if key.name == "down":
            self._scroll(1)
            return True
        if key.name == "pageup":
            self._scroll(-self._page())
            return True
        if key.name == "pagedown":
            self._scroll(self._page())
            return True
        if key.name == "ctrl-d":
            self._scroll(self._page())
            return True
        if key.name == "ctrl-u" or (key.name == "char" and key.char == CTRL_U):
            self._scroll(-self._page())
            return True
        return False

    def _page(self):
        """How far a page key moves: half the visible rows, at least one."""
        _items, budget = self._window_state()
        return max(1, budget // 2)

    def _navigatr_scroll_by_key(self, key):
        """The scroll keys, in the navigatr: they move the cursor.

        The window follows the cursor, so the user never has to think about
        which of the two is moving.
        """
        if key.name == "up":
            self._move_cursor(-1)
            return True
        if key.name == "down":
            self._move_cursor(1)
            return True
        if key.name == "pageup":
            self._move_cursor(-self._page())
            return True
        if key.name == "pagedown":
            self._move_cursor(self._page())
            return True
        if key.name == "ctrl-d":
            self._move_cursor(self._page())
            return True
        if key.name == "ctrl-u" or (key.name == "char" and key.char == CTRL_U):
            self._move_cursor(-self._page())
            return True
        return False

    # -- the frame -----------------------------------------------------------

    def _top_border(self, inner):
        """The top border, with the breadcrumb in it.

        The right-hand end of the border carries what the list holds: how
        many spaces and panes, or how many agents and how many of them
        are blocked. It is the cheapest thing in the window to be useful,
        and it is where Herdr's own navigation surface puts its count.
        """
        summary = self.navigatr.summary if self.navigatr is not None else ""
        return self._border(
            FRAME_TOP_LEFT, FRAME_TOP_RIGHT, self._breadcrumb(), inner, summary
        )

    def _bottom_border(self, inner):
        """The bottom border, with the key hints and the position in it."""
        # The border keeps four columns for itself: the left corner, the right
        # corner and the rule beside each. The badge takes some of what is
        # left, so the hints are told what they will really get - otherwise
        # they are cut after the fact and end in an ellipsis.
        room = inner - 4
        _, badge_width, spacer = self._badge_layout(self._position(), room)
        return self._border(
            FRAME_BOTTOM_LEFT,
            FRAME_BOTTOM_RIGHT,
            self._footer_text(room - badge_width - spacer),
            inner,
            self._position(),
        )

    def _position(self):
        """Where we are in the list, e.g. "12/44", or "" when it all fits.

        The indicator only appears when the layer is longer than the window.
        A "44/44" on a list that fits says nothing, and a position that is
        not there is worse than no position at all.
        """
        if self.navigatr is not None:
            return self._navigatr_position()
        if not self._view_total or self._view_total <= self._view_budget:
            return ""
        return "%d/%d" % (self._view_first, self._view_total)

    def _navigatr_position(self):
        """Where the cursor is in the tree, e.g. "3/17".

        The cursor, not the window: in the navigatr the cursor is the
        position, and the window is only where it happens to be drawn.
        """
        if not self._view_total or self._view_total <= self._view_budget:
            return ""
        cursor = self.navigatr.cursor if self.navigatr is not None else 0
        return "%d/%d" % (cursor + 1, self._view_total)

    def _badge_layout(self, badge, room):
        """The badge, how wide it is, and the space before it.

        The badge and the key hints share one room in the border, so whoever
        writes the hints has to know how much the badge will take. Asking the
        border afterwards means the hints are cut after the fact, and a hint
        that ends in "..." is worse than one that was never there.
        """
        badge_shown = ""
        if badge:
            candidate = truncate(badge, max(0, room - 3))
            # The badge needs a column of text and a column of rule beside
            # it, or the border would be wider than the screen.
            if table.visible_width(candidate) + 3 <= room:
                badge_shown = candidate
        badge_width = table.visible_width(badge_shown)
        # The space between the hints and the badge.
        spacer = 1 if badge_shown else 0
        return badge_shown, badge_width, spacer

    def _border(self, left, right, text, inner, badge=""):
        """A top or bottom border with `text` in it.

        The corners and the left edge are the accent -- that edge is the bar
        that says which layer you are in -- and the rule itself is muted, so
        the frame does not shout over the content.

        The text is truncated rather than allowed to push the border out: the
        right border has to be at the same column on every line. The badge
        (where we are in the list) sits next to the right border, so it
        survives when the hints have to be shortened -- and it is dropped
        altogether rather than push the border past the edge of the screen.
        """
        room = inner - 4
        badge_shown, badge_width, spacer = self._badge_layout(badge, room)
        shown = truncate(text, max(1, room - badge_width - spacer))
        fill = max(
            1, inner - 3 - table.visible_width(shown) - badge_width - spacer
        )
        accent = self.palette.accent
        hint = self.palette.hint
        reset = self.palette.reset
        return "".join(
            [
                accent, left, reset,
                hint, FRAME_HORIZONTAL, reset,
                " ", shown, " ",
                hint, badge_shown, " " if badge_shown else "",
                hint, FRAME_HORIZONTAL * fill, reset,
                hint, right, reset,
            ]
        )

    def _frame_row(self, line, inner, frame):
        """One line of the body, between the two borders."""
        if not frame:
            return line
        return "%s%s%s%s%s%s%s" % (
            self.palette.accent,
            FRAME_VERTICAL,
            self.palette.reset,
            self._fit(line, inner),
            self.palette.hint,
            FRAME_VERTICAL,
            self.palette.reset,
        )

    def _fit(self, line, inner):
        """Pad `line` to `inner` columns, so the right border lines up.

        Only padding. A line that is too wide is left too wide: the frame
        must not hide it, because that is what the anti-wrap guard is for.
        """
        width = table.visible_width(line)
        if width < inner:
            return line + " " * (inner - width)
        return line

    def _breadcrumb(self):
        """The path to the current layer: "layers › panes › tabs".

        The last crumb is the layer you are in, and it is the one that is
        coloured: it answers "where am I?" at a glance.
        """
        parts = [self.tree.root.title]
        if self.navigatr is not None:
            parts.append(AGENTS_TITLE if self._is_agents() else NAVIGATR_TITLE)
        else:
            for layer_id in self.stack:
                layer = self.tree.layer(layer_id)
                if layer is not None:
                    parts.append(layer.title)
        if len(parts) < 2:
            return parts[0]
        return "%s › %s%s%s%s" % (
            " › ".join(parts[:-1]),
            self.palette.accent,
            self.palette.bold,
            parts[-1],
            self.palette.reset,
        )

    def _footer_text(self, width=None):
        """The key hints: what the keys do.

        The reference key is the one the tree was built with, so a user who
        changes `reference_key` in layers.toml sees the real key here.

        The hint says what Escape does *now*: while a filter is in force it
        clears the filter, and only a second Escape goes up a level. A hint
        that says "esc close" when Escape does something else is a hint
        that lies, and the footer is the only place the user can read.
        """
        if self.navigatr is not None:
            return self._navigatr_footer(width)
        reference_key = self.tree.reference_key or "?"
        if self.filtering:
            # The query is being typed: Escape throws it away, Enter keeps
            # it.
            return "esc clear · enter keep · %s reference" % reference_key
        if self.filter_query:
            # A filter is in force: Escape is the key that clears it.
            return "esc clear filter · ⌫ up · %s reference" % reference_key
        # "/filter" and not "/ filter": the hint has to fit beside the
        # position in the bottom border at the width the popup really has.
        return "esc close · ⌫ up · %s reference · /filter" % reference_key

    def _navigatr_footer(self, width=None):
        """The key hints of the navigatr.

        The navigatr has no layer actions, so `j` and `k` are free to move
        the cursor -- and the user expects them to, because they move the
        cursor everywhere else. The footer says so, because the footer is
        the only place the user can read what the keys do.

        Escape goes back to the root layer, and it does not close the
        overlay: the navigatr is a mode, not the end of the road. It
        clears the filter first, when there is one.

        **The footer is one line and it does not have room for everything.**
        The room is the popup's own width, and the position badge takes
        some of it. So the hints are a ladder, widest first, and the first
        one that fits is the one that is shown: a hint that gets cut in
        half says less than one that is complete.

        The keys that cannot be guessed come first in the ladder, because
        they are the ones nobody finds any other way: `h` and `l` fold a
        space, `H` and `L` fold them all, and `r` starts the rearranging.
        The numbers and the movement keys have been in the footer since
        the beginning and the user knows them.
        """
        noun = "agent" if self._is_agents() else "space"
        if self.filtering:
            # The query is being typed: Escape throws it away, Enter keeps
            # it.
            return "esc clear · enter keep · ↑↓ j/k move"
        if self.filter_query:
            # A filter is in force: Escape clears it and the navigatr
            # stays. The hint has to say which of the two Escape does.
            # The rows are still numbered and a digit still jumps, so the
            # numbers are mentioned here too when they fit.
            base = "esc clear filter · enter jump · ↑↓ j/k move"
            with_numbers = "esc clear filter · 1-9 %s · enter jump · ↑↓ j/k move" % noun
            if width is None or table.visible_width(with_numbers) <= width:
                return with_numbers
            return base
        if self._rearranging:
            return self._rearrange_footer(width)
        if self._is_agents():
            # The agents list is a flat list of agents. There are no spaces
            # in it to fold and nothing in it to rearrange, so the footer
            # does not offer the keys that do those things.
            for text in (
                "esc back · 1-9 agent · enter jump · ↑↓ j/k move · /filter",
                "esc back · enter jump · ↑↓ j/k move · /filter",
                "esc back · enter jump · ↑↓ j/k move",
                "esc back",
            ):
                if width is None or table.visible_width(text) <= width:
                    return text
            return "esc back"
        for text in (
            "esc back · 1-9 space · h/l fold · H/L all · r move · enter jump · ↑↓ j/k move",
            "esc back · 1-9 space · h/l fold · r move · enter jump · ↑↓ j/k move",
            "esc back · h/l fold · H/L all · r move · enter jump · ↑↓ j/k move",
            "esc back · h/l fold · r move · enter jump · ↑↓ j/k move",
            "esc back · h/l fold · r move · enter jump",
            "esc back · h/l fold · enter jump · ↑↓ j/k move",
            "esc back · enter jump · ↑↓ j/k move · /filter",
            "esc back · enter jump · ↑↓ j/k move",
            "esc back",
        ):
            if width is None or table.visible_width(text) <= width:
                return text
        return "esc back"

    def _rearrange_footer(self, width=None):
        """The key hints while the rearranging is on.

        The footer says what enter does NOW: it picks the row up when
        nothing is carried, and puts the carried row down when something
        is. A hint that says one of the two when the key does the other
        is a hint that lies.
        """
        if self._carried is None:
            base = "esc cancel · enter pick up · ↑↓ j/k move"
            if width is None or table.visible_width(base) <= width:
                return base
            return "esc cancel · enter pick up"
        if self._carried["kind"] == navigatr_mod.PANE:
            wide = "esc cancel · enter drop · d split %s · ↑↓ j/k move" % self._split
            base = "esc cancel · enter drop · ↑↓ j/k move"
        else:
            wide = "esc cancel · enter drop · ↑↓ j/k move"
            base = "esc cancel · enter drop"
        if width is None or table.visible_width(wide) <= width:
            return wide
        return base

    def _plain_footer(self, inner):
        """The key hints as a line of their own, for the frameless layout."""
        position = self._position()
        # The position is appended after the hints, so it comes off the room
        # they are given.
        taken = table.visible_width(position) + (3 if position else 0)
        text = self._footer_text(inner - len(ROW_INDENT) - taken)
        if position:
            text = "%s · %s" % (text, position)
        return "%s%s%s" % (
            self.palette.hint,
            truncate(ROW_INDENT + text, inner),
            self.palette.reset,
        )

    def _input_line(self, width):
        """The prompt and the buffer, budgeted against one width.

        The two are halves of one line, so they share one budget. Truncated
        separately they give a line up to 2*width+1 columns wide, which
        wraps the cursor and lands the next line in the middle of a row.
        A long branch name in `rename_pane` or `new_worktree` reaches it.

        The prompt gets at most half the line: it is a fixed label, and the
        buffer is what the user is typing.

        Returns (prompt, buffer, separator).
        """
        prompt = truncate(self.input_prompt, max(1, width // 2))
        room = width - table.visible_width(prompt) - 1
        buffer = truncate(self.input_buffer, max(0, room))
        return prompt, buffer, " " if room > 0 else ""

    def _item_row(self, item):
        """The cells of one item row: key, icon, description, chord and note."""
        if item.heading:
            return self._heading_row(item)
        if item.key:
            colour = self.palette.hint if item.is_hint else self.palette.accent
            key = "%s%s%s" % (colour, item.key, self.palette.reset)
        else:
            key = ""
        icon = ""
        if item.is_layer or item.is_mode:
            # One glyph per layer, on the entry that enters it -- and one
            # for the navigatr, which the root lists as well.
            icon = self.visuals.layer_icon(item.action.split(":", 1)[1])
        chord = (
            "%s%s%s" % (self.palette.chord, item.chord, self.palette.reset)
            if item.chord
            else ""
        )
        note = ""
        if item.note:
            # The row has something to say about itself: the custom layer's
            # window commands have to be pressed in Herdr, because the
            # overlay is a popup and cannot open another one.
            note = "%s%s%s" % (self.palette.hint, item.note, self.palette.reset)
        elif item.is_hint and item.chord:
            # An action Herdr owns: the chord has to be pressed in Herdr
            # itself. The note is a column of its own, so it cannot push the
            # chords out of line.
            note = "%spress this in herdr%s" % (self.palette.hint, self.palette.reset)
        return [key, icon, item.desc, chord, note]

    def _heading_row(self, item):
        """The row that heads a group in the reference view.

        The tree marker says "a group starts here", and the group's own
        glyph says which one. Both come from the herdr-navigator plugin, so
        the two read as one family.
        """
        glyph = self.visuals.group_icon(item.heading)
        desc = "%s %s" % (glyph, item.desc) if glyph else item.desc
        styled = "%s%s%s%s" % (
            self.palette.accent,
            self.palette.bold,
            desc,
            self.palette.reset,
        )
        return [self.visuals.marker("branch"), "", styled, "", ""]

    # -- input --------------------------------------------------------------

    def _key_lookup(self):
        """Map key -> item for the current layer, plus any duplicated keys.

        Two items in one layer with the same key are a configuration mistake:
        one of them silently shadows the other. We keep the first and report
        the clash rather than dropping one of them in silence.
        """
        layer = self.tree.layer(self.layer_id)
        lookup = {}
        duplicates = []
        for item in layer.items:
            if not item.key:
                continue
            if item.key in lookup:
                duplicates.append(item.key)
                continue
            lookup[item.key] = item
        return lookup, duplicates

    def _duplicate_keys(self):
        return self._key_lookup()[1]

    def handle(self, key):
        if key.name == "eof":
            self.running = False
            return
        if key.name == "ctrl-c":
            self.running = False
            return

        if self.input_prompt is not None:
            self._handle_input(key)
            return

        if self.filtering:
            # While the filter is open it owns the keys: a letter is part of
            # the query, not a command.
            self._handle_filter(key)
            return

        if self.navigatr is not None:
            # The navigatr owns the keys while it is open: there are no
            # layer actions in it, so the keys are its own.
            self._handle_navigatr(key)
            return

        if key.name == "esc":
            # A filter that is in force is the last thing the user asked
            # for, so Escape undoes it before it undoes anything else: the
            # list goes back to every row, and only a second Escape goes
            # up a level.
            if self.filter_query:
                self._clear_filter()
                return
            self._up()
            return
        if key.name == "backspace":
            self._up()
            return

        # The scroll keys. The arrows were ignored before, so they are free;
        # `j` and `k` are not, because they are actions in the panes and
        # navigation layers.
        if self._scroll_by_key(key):
            return

        if key.name in ("left", "right", "tab", "enter"):
            return

        if key.name != "char":
            return

        pressed = key.char

        # The reference view. The key is the one the tree was built with
        # (layers.toml's `reference_key`), not a constant here.
        if self.layer_id == "root" and pressed == self.tree.reference_key:
            self._enter("reference")
            return

        lookup, duplicates = self._key_lookup()
        if pressed in duplicates:
            self.message = "duplicate key %r in this layer" % pressed
            self.message_kind = "err"
            return

        item = lookup.get(pressed)
        if item is None:
            if pressed == FILTER_KEY:
                # The filter. A layer that binds "/" itself keeps it: the
                # filter only takes the key when nothing else wants it.
                self._start_filter()
                return
            self.message = "no binding for %r" % pressed
            self.message_kind = "err"
            return

        if item.action.startswith("layer:"):
            self._enter(item.action.split(":", 1)[1])
            return

        if item.action == tree_mod.NAVIGATR_ACTION:
            # The navigatr. It is listed in the root, so the key is
            # discoverable, and a layer that binds the key itself keeps it
            # -- exactly like the filter key: the key is only taken when
            # nothing else wants it.
            self._open_navigatr()
            return

        if item.action == tree_mod.AGENTS_ACTION:
            # The agents list. The same mode with a different list: the
            # same keys, the same footer, and the agents of the session
            # the ones that want attention first.
            self._open_navigatr(navigatr_mod.AGENTS)
            return

        if not item.executable:
            self.message = "%s has no socket method -- press %s in herdr" % (
                item.desc,
                item.chord or "the chord",
            )
            self.message_kind = "err"
            return

        prompt = item.prompt
        if prompt and not item.value:
            self.input_prompt = prompt
            self.input_buffer = ""
            self.input_action = item.action
            self.input_item = item
            return

        # The custom layer's rows are not keymap actions: the overlay says
        # which entry the key stands for, and the action map runs it.
        self.context.entry = self._custom_entry(item)
        self._run(item, item.value or None)

    def _custom_entry(self, item):
        """The custom entry a key stands for, or None.

        The custom layer's rows are built when the layer is opened, so the
        overlay keeps them: the action map has to know which plugin action
        to invoke, and which command to run.
        """
        if self.layer_id != custom_mod.LAYER_ID:
            return None
        return self.custom_entries.get(item.key)

    # -- the navigatr --------------------------------------------------------

    def _open_navigatr(self, kind=navigatr_mod.PLACES):
        """Open the navigatr, with the live session in it.

        The data is fetched here, not on every render: a render happens on
        every keystroke, and a socket round trip per keystroke would make
        the overlay feel like it is lagging. Re-entering the navigatr
        fetches it again, so the tree is never staler than the last visit.
        """
        self.navigatr = navigatr_mod.fetch(self.context.client, kind=kind)
        # The filter belongs to the layer it was typed in, and so does the
        # scroll: a fresh navigatr starts at the top with nothing hidden.
        self._reset_view()

    def _close_navigatr(self):
        """Leave the navigatr and go back to the root layer.

        The overlay stays open: the navigatr is a mode, not the end of the
        road.
        """
        self.navigatr = None
        self.layer_id = "root"
        self.stack = []
        self._stop_rearranging()
        self._reset_view()

    def _handle_navigatr(self, key):
        """The keys the navigatr owns.

        There are no layer actions in it, so `j` and `k` are free -- and
        the user expects them to move the cursor, because they move it
        everywhere else. The arrows move it too, and so do the page keys.

        Escape undoes the filter before it undoes anything else, exactly
        as it does in a layer: a filter in force is the last thing the
        user asked for, so it goes first and the navigatr stays.

        `h` and `l` fold and unfold the space the cursor is in; `H` and `L`
        fold and unfold every space. `r` starts the rearranging, which is
        where a tab or a pane is picked up and put down somewhere else.
        """
        if key.name == "esc":
            if self.filter_query:
                self._clear_filter()
                return
            if self._rearranging:
                self._stop_rearranging()
                return
            self._close_navigatr()
            return
        if key.name == "backspace":
            if self._rearranging:
                self._stop_rearranging()
                return
            self._close_navigatr()
            return
        if key.name == "enter":
            if self._rearranging:
                self._rearrange_enter()
                return
            self._jump()
            return
        if key.name == "char":
            if key.char == FILTER_KEY:
                if self._rearranging:
                    # A filter would take the row that is being carried
                    # out of the list, and there would be nowhere to put
                    # it down.
                    self.message = "esc leaves the rearranging first"
                    self.message_kind = "err"
                    return
                # The filter. The navigatr has no actions of its own, so
                # "/" is always free here.
                self._start_filter()
                return
            if key.char == "j":
                self._move_cursor(1)
                return
            if key.char == "k":
                self._move_cursor(-1)
                return
            if key.char in "123456789":
                # A number moves the cursor to that row: a space in the
                # places tree, an agent in the agents list. `enter` is what
                # selects it, the same as it is for every other row.
                self._move_to_number(key.char)
                return
            if self._rearranging:
                self._rearrange_char(key.char)
                return
            if key.char in "hlHL":
                self._fold_key(key.char)
                return
            if key.char == "r":
                self._start_rearranging()
                return
            self.message = "no binding for %r" % key.char
            self.message_kind = "err"
            return
        # The scroll keys: up, down, the page keys and ctrl+d / ctrl+u.
        self._navigatr_scroll_by_key(key)

    # -- the folds ------------------------------------------------------------

    def _space_of(self, rows, index):
        """The workspace id of the space the row is in, or "".

        The space of a row that IS a space is the row itself; the space of
        anything under one is the nearest space above it.
        """
        if not 0 <= index < len(rows):
            return ""
        row = rows[index]
        if row.kind == navigatr_mod.WORKSPACE:
            return row.params.get("workspace_id", "")
        for candidate in range(index - 1, -1, -1):
            if rows[candidate].kind == navigatr_mod.WORKSPACE:
                return rows[candidate].params.get("workspace_id", "")
        return ""

    def _fold_key(self, pressed):
        """Fold or unfold a space, or every space."""
        if self._is_agents():
            self.message = "there are no spaces to fold here"
            self.message_kind = "err"
            return
        if self._query():
            # A filter is showing everything, so a fold would change
            # nothing the user can see.
            self.message = "the filter is showing everything"
            self.message_kind = "err"
            return
        rows = self._navigatr_visible()
        if pressed in "hl":
            space_id = self._space_of(rows, self.navigatr.cursor)
            if not space_id:
                self.message = "there is no space here to fold"
                self.message_kind = "err"
                return
            self._set_folds({space_id: pressed == "h"})
            return
        # H and L: every space there is.
        self._set_folds(
            {
                row.params.get("workspace_id", ""): pressed == "H"
                for row in rows
                if row.kind == navigatr_mod.WORKSPACE
            }
        )

    def _set_folds(self, changes):
        """Fold or unfold spaces, and keep the cursor where it was.

        The cursor is a place in the list of visible rows, and folding
        takes rows out of that list -- so the cursor is put back on the
        row it was on. If that row was under a space that was folded,
        the cursor goes to the space, which is still there.
        """
        rows = self._navigatr_visible()
        current = None
        space = ""
        if 0 <= self.navigatr.cursor < len(rows):
            current = rows[self.navigatr.cursor]
            space = self._space_of(rows, self.navigatr.cursor)

        self._folds.update(changes)
        after = self._folded_rows(self.navigatr.rows)

        if current is not None:
            for index, row in enumerate(after):
                if row is current:
                    self.navigatr.cursor = index
                    return
        for index, row in enumerate(after):
            if (
                row.kind == navigatr_mod.WORKSPACE
                and row.params.get("workspace_id", "") == space
            ):
                self.navigatr.cursor = index
                return
        self.navigatr.cursor = self._selectable_row(after, 0)

    # -- the rearranging -----------------------------------------------------

    def _start_rearranging(self):
        """Start the rearranging: the mode where enter picks up and drops."""
        if self._is_agents():
            self.message = "there is nothing to rearrange in the agents list"
            self.message_kind = "err"
            return
        if self._query():
            self.message = "the filter is showing everything"
            self.message_kind = "err"
            return
        self._rearranging = True
        self._carried = None
        self.message = "enter picks it up, enter puts it down"
        self.message_kind = "ok"

    def _stop_rearranging(self):
        """Leave the rearranging, and put down whatever was carried."""
        self._rearranging = False
        self._carried = None
        self.message = ""

    def _rearrange_char(self, pressed):
        """The keys the rearranging owns on top of the navigatr's."""
        if pressed == "d":
            if self._carried is None or self._carried["kind"] != navigatr_mod.PANE:
                self.message = "a split is only asked for when a pane is carried"
                self.message_kind = "err"
                return
            self._split = "down" if self._split == "right" else "right"
            self.message = "split %s" % self._split
            self.message_kind = "ok"
            return
        if pressed in "hlHLr":
            # A fold would take the row being carried out of the list, and
            # there would be nowhere to put it down.
            self.message = "esc leaves the rearranging first"
            self.message_kind = "err"
            return
        self.message = "no binding for %r" % pressed
        self.message_kind = "err"

    def _rearrange_enter(self):
        """Pick the row under the cursor up, or put the carried one down."""
        rows = self._navigatr_visible()
        if not rows:
            self.message = "there is nothing to rearrange"
            self.message_kind = "err"
            return
        index = self.navigatr.cursor
        if not 0 <= index < len(rows):
            return
        row = rows[index]
        if self._carried is None:
            self._pick_up(row)
            return
        self._drop(row, rows)

    def _pick_up(self, row):
        """Take the row with the cursor: a tab or a pane."""
        if row.kind == navigatr_mod.TAB:
            self._carried = {"kind": navigatr_mod.TAB, "row": row}
            self.message = "carrying the tab %s" % row.label
            self.message_kind = "ok"
            return
        if row.kind == navigatr_mod.PANE:
            self._carried = {"kind": navigatr_mod.PANE, "row": row}
            self.message = "carrying the pane %s" % row.label
            self.message_kind = "ok"
            return
        self.message = "a space is not something you can move"
        self.message_kind = "err"

    def _drop(self, target, rows):
        """Put the carried row down on the target row."""
        carried = self._carried
        if carried is None:
            return
        client = self.context.client
        if carried["kind"] == navigatr_mod.TAB:
            ok, message = navigatr_mod.move_tab(
                client, carried["row"], target, self.navigatr.rows
            )
        else:
            ok, message = navigatr_mod.move_pane(
                client, carried["row"], target, self._split
            )
        if not ok:
            # The row is still carried: the user can aim somewhere else
            # instead of starting over.
            self.message = message
            self.message_kind = "err"
            return
        self._carried = None
        self._rearranging = False
        self.message = message
        self.message_kind = "ok"
        # The tree is a picture of the session, and the session has just
        # changed: it is fetched again, or the navigatr shows a place that
        # is not there any more.
        kind = self.navigatr.kind
        self.navigatr = navigatr_mod.fetch(client, kind=kind)
        self._reset_view()
        # The context is a snapshot, and a pane that moved is not in the
        # place it was. The overlay closes if the space or the tab it was
        # opened in is gone -- the same rule every other action follows.
        if self._left_the_context():
            self.running = False

    def _jump(self):
        """Go to the row under the cursor, and close the overlay.

        A target that has gone away -- a pane closed while the navigatr
        was open -- is reported and the overlay stays open, because the
        useful next move is another key.
        """
        rows = self._navigatr_visible()
        if not rows:
            self.message = "there is nothing to go to"
            self.message_kind = "err"
            return
        self._jump_to_row(rows, self.navigatr.cursor)

    def _move_to_number(self, digit):
        """Move the cursor to the row carrying that number.

        The number does NOT jump. It moves the cursor, so the user can see
        where they are pointing before committing, and `enter` does what
        enter has always done: it goes to the row under the cursor. With
        several rows to choose from, aiming with a number and confirming
        with enter is faster than hunting with the arrow keys, and it
        cannot land on the wrong one.
        """
        rows = self._navigatr_visible()
        numbers = self._navigatr_numbers(rows)
        for index, number in numbers.items():
            if number == digit:
                self.navigatr.cursor = index
                return
        noun = "agent" if self._is_agents() else "space"
        self.message = "no %s numbered %s" % (noun, digit)
        self.message_kind = "err"

    def _jump_to_row(self, rows, index):
        """Jump to one row of the tree, and close the overlay."""
        if index < 0 or index >= len(rows):
            self.message = "there is nothing to go to"
            self.message_kind = "err"
            return
        if not self._navigatr_selectable(rows[index]):
            # A tab is a heading, not a place: the cursor never stops on
            # one, and there is nothing to go to.
            self.message = "that row is a heading, not a place"
            self.message_kind = "err"
            return
        # The cursor goes where the jump went, so the overlay and the session
        # agree if it is still open.
        self.navigatr.cursor = index
        ok, message = navigatr_mod.jump(self.context.client, rows[index])
        if not ok:
            self.message = message
            self.message_kind = "err"
            return
        self.running = False

    def _handle_input(self, key):
        if key.name == "esc":
            self.input_prompt = None
            self.input_buffer = ""
            self.input_action = None
            self.input_item = None
            self.message = "cancelled"
            self.message_kind = "ok"
            return
        if key.name == "enter":
            item = self.input_item
            value = self.input_buffer
            self.input_prompt = None
            self.input_buffer = ""
            self.input_action = None
            self.input_item = None
            if item is not None:
                self._run(item, value)
            return
        if key.name == "backspace":
            self.input_buffer = self.input_buffer[:-1]
            return
        if key.name == "char":
            self.input_buffer += key.char

    def _run(self, item, value):
        from . import actions as actions_mod

        executor = actions_mod.EXECUTORS.get(item.action)
        if executor is None:
            self.message = "%s is not executable" % item.action
            self.message_kind = "err"
            return
        try:
            outcome = executor(self.context, value)
        except HerdrError as exc:
            self.message = "%s failed: %s" % (item.action, exc.message)
            self.message_kind = "err"
            return
        except HerdrUnavailable as exc:
            self.message = "herdr unreachable: %s" % exc
            self.message_kind = "err"
            return
        except Exception as exc:  # noqa: BLE001 - an overlay must not die
            self.message = "%s raised %s" % (item.action, exc.__class__.__name__)
            self.message_kind = "err"
            return

        self.message = outcome.message or item.desc
        # A refusal is not a success: the overlay colours the message to
        # say which one it was.
        self.message_kind = outcome.kind or "ok"
        # The session can move under the overlay. The context is a
        # snapshot of where it was opened, and every action that names a
        # pane, a tab or a space names it from that snapshot -- so the
        # context is re-read after every action, and the overlay closes
        # itself when the workspace or the tab it was opened in is no
        # longer the current one.
        if self._left_the_context():
            self.running = False
            return
        # `keep = true` in the tree keeps the overlay open for repeatable
        # verbs (move focus, resize). Without it the nav layer closes on the
        # first move and the user cannot move twice.
        if outcome.close or not (outcome.keep_open or item.keep):
            self.running = False

    def _left_the_context(self):
        """True when the action moved the workspace or the tab.

        A popup is a session-modal resource: it takes every keystroke
        wherever it was opened. So an action that switches the workspace
        and leaves the overlay open strands the popup on the old one --
        the user is in the new workspace and the keyboard is still in the
        old, and the plugin cannot be opened again until the overlay is
        closed by hand.

        The same is true of the tab: the overlay's context names the tab
        it was opened in, and an action that switches tabs leaves every
        later action pointing at the old one.

        So the context is re-read after every action, and the overlay
        closes when the workspace or the tab changed. The pane is not part
        of this: moving the focus between panes is the whole point of the
        navigation layer, and the overlay stays open for it.
        """
        before = (self.context.workspace_id, self.context.tab_id)
        # `refresh()` keeps the old ids when the call fails, so a dead
        # socket leaves the overlay open rather than closing it.
        self.context.refresh()
        after = (self.context.workspace_id, self.context.tab_id)
        return after != before

    def _enter(self, layer_id):
        if layer_id == "reference":
            self.stack.append("reference")
            self.layer_id = "reference"
        elif layer_id in self.tree.layers:
            self.stack.append(layer_id)
            self.layer_id = layer_id
        else:
            return
        self._reset_view()
        if layer_id == custom_mod.LAYER_ID:
            self._open_custom()

    def _open_custom(self):
        """Fill the custom layer with the user's commands and the plugins' actions.

        The plugins' actions are fetched here, not on every render: a render
        happens on every keystroke, and a socket round trip per keystroke
        would make the overlay feel like it is lagging. Entering the layer
        again fetches them again, so the list is never staler than the last
        visit -- the same rule the navigatr follows.
        """
        layer = self.tree.layers.get(custom_mod.LAYER_ID)
        if layer is None:
            return
        plugin_actions, plugins, error = custom_mod.fetch(self.context.client)
        entries, skipped = custom_mod.build(
            self.tree.keymap, plugin_actions, plugins
        )
        if self.custom_base_items is None:
            # The items the tree had for this layer, kept: a user who gave
            # the layer items of their own keeps them.
            self.custom_base_items = list(layer.items)
        layer.items = list(self.custom_base_items) + [
            tree_mod.Item(
                key=entry.key,
                desc=entry.desc,
                action=entry.action,
                layer=custom_mod.LAYER_ID,
                chord=entry.chord,
                note=entry.note,
            )
            for entry in entries
        ]
        self.custom_entries = {entry.key: entry for entry in entries if entry.key}
        self.custom_error = error
        self.custom_skipped = skipped
        self.custom_unkeyed = len(custom_mod.unkeyed(entries))

    def _up(self):
        if self.stack:
            self.stack.pop()
            self.layer_id = self.stack[-1] if self.stack else "root"
        else:
            self.running = False
            return
        self._reset_view()

    def _reset_view(self):
        """A new layer starts at the top, with no filter.

        The filter belongs to the layer it was typed in: it hides rows of
        that layer, and it says nothing about the next one.
        """
        self.scroll = 0
        self.filtering = False
        self.filter_draft = ""
        self.filter_query = ""

    # -- main loop ----------------------------------------------------------

    def run(self):
        if termios is None or not self._stdin.isatty():
            self.out.write("managr needs a terminal" + NL)
            return 1

        fd = self._stdin.fileno()
        old = termios.tcgetattr(fd)
        self._saved_termios = old
        self._install_signal_handlers(fd, old)
        reader = KeyReader(fd)
        try:
            tty.setraw(fd)
            self.render()
            while self.running:
                try:
                    data = os.read(fd, 64)
                except OSError:
                    break
                if not data:
                    break
                # One read can hold several keystrokes, or half of one, so the
                # reader hands them over one at a time.
                reader.feed(data)
                for key in reader.keys():
                    self.handle(key)
                    if not self.running:
                        break
                    self.render()
        finally:
            self._restore(fd, old)
            self._clear_screen()
        return 0

    def _install_signal_handlers(self, fd, old):
        """Restore the terminal when a signal kills us.

        `finally` does not run when the process is killed by a signal, so a
        SIGTERM would leave the tty in raw mode. That matters if the overlay is
        ever hosted in a normal pane rather than a popup: the user's shell
        would come back with echo and line editing switched off.

        """
        def handler(signum, frame):
            # TCSANOW, not TCSADRAIN: draining waits for pending output to be
            # written, and if the reader is not keeping up that wait can block
            # inside a signal handler forever, hanging the process instead of
            # letting it die.
            #
            # Nothing else happens here either: no escape sequences, no
            # flush. A write blocks the same way a drain does, and a signal
            # handler that blocks means the process ignores the signal and
            # needs SIGKILL.
            self._restore(fd, old, drain=False)
            # Re-raise with the default disposition so the exit status is
            # honest about how the process died.
            signal.signal(signum, signal.SIG_DFL)
            os.kill(os.getpid(), signum)

        for signum in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT):
            try:
                signal.signal(signum, handler)
            except (OSError, ValueError):
                pass

    def _restore(self, fd, old, drain=True):
        """Put the terminal back the way we found it.

        Safe to call from a signal handler: it touches the terminal's
        settings and the signal dispositions, and nothing else.
        """
        when = termios.TCSADRAIN if drain else termios.TCSANOW
        try:
            termios.tcsetattr(fd, when, old)
        except (termios.error, OSError):
            pass
        for signum in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT):
            try:
                signal.signal(signum, signal.SIG_DFL)
            except (OSError, ValueError):
                pass

    def _clear_screen(self):
        """Wipe the overlay's output.

        Not called from the signal handler: a write there blocks when the
        pty's buffer is full, and a blocked signal handler means the process
        never dies. The popup is being torn down anyway, so losing the wipe
        costs nothing.
        """
        try:
            self.out.write("\x1b[0m\x1b[2J\x1b[H")
            # The cursor was hidden while the overlay drew its list, and
            # the terminal it was hidden in is the user's own: it has to
            # be given back.
            self.out.write(SHOW_CURSOR)
            self.out.flush()
        except (OSError, ValueError):
            pass
