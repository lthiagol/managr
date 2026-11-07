"""End-to-end test of the overlay through a real pty.

A Herdr popup has no pane id, so there is no socket method that can send it
keystrokes -- `pane.send_input` cannot reach one. That makes a pty the only
way to test the thing that actually matters: that the overlay puts the
terminal into raw mode, decodes keystrokes, renders, and exits cleanly.

The client is faked, so nothing here touches a running Herdr.

Run: python3 tests/test_overlay_pty.py
"""

import json
import os
import pty
import re
import select
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unicodedata
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))


# How wide a line is, measured here rather than with the renderer's own
# function: a test that asks the code under test how wide its output is
# proves nothing.
ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")

# The shared state file lives under the config dir, and the config dir is
# computed from `HERDR_CONFIG_PATH`. The tests drive the real overlay, so
# they get their own config dir: without this they would write to the
# user's own `~/.config/herdr/plugins/`.
_STATE_DIR = None
_STATE_CONFIG = None


def setUpModule():
    global _STATE_DIR, _STATE_CONFIG
    _STATE_DIR = tempfile.mkdtemp(prefix="managr-pty-")
    _STATE_CONFIG = os.path.join(_STATE_DIR, "config.toml")
    with open(_STATE_CONFIG, "w", encoding="utf-8") as handle:
        handle.write('[keys]\nprefix = "ctrl+a"\n')
    os.environ["HERDR_CONFIG_PATH"] = _STATE_CONFIG


def tearDownModule():
    if _STATE_DIR:
        shutil.rmtree(_STATE_DIR, ignore_errors=True)


def display_width(text):
    plain = ANSI.sub("", text)
    return sum(
        2 if unicodedata.east_asian_width(char) in ("W", "F") else 1 for char in plain
    )

# A standalone driver: build the tree with a fixed keymap, use a fake client,
# and run the overlay. Kept separate from the overlay module so the test does
# not have to patch anything.
DRIVER = r'''
import os, sys
sys.path.insert(0, "__SRCPATH__")
from managr import keymap as keymap_mod, overlay as overlay_mod, tree as tree_mod

class FakeClient:
    def __init__(self):
        self.calls = []
    def call(self, method, params=None):
        self.calls.append((method, params))
        if method == "pane.current":
            return {"pane": {"pane_id": "w1:p1", "tab_id": "w1:t1", "workspace_id": "w1"}}
        if method == "tab.list":
            return {"tabs": [{"tab_id": "w1:t1", "label": "one"}]}
        return {}

km = keymap_mod.Keymap(prefix="C-a", bindings={
    "focus_pane_left": keymap_mod.Binding("focus_pane_left", "focus left", "h"),
    "split_vertical": keymap_mod.Binding("split_vertical", "split right", "v"),
    "close_pane": keymap_mod.Binding("close_pane", "close pane", "x"),
    "help": keymap_mod.Binding("help", "help", "?"),
})
tree = tree_mod.build(keymap=km, labels={})
client = FakeClient()
ctx = __import__("managr.actions", fromlist=["Context"]).Context(
    client, workspace_id="w1", tab_id="w1:t1", pane_id="w1:p1")
surface = overlay_mod.Overlay(tree, ctx)
code = surface.run()
sys.stderr.write("EXIT=" + str(code) + "\n")
sys.stderr.write("CALLS=" + repr(client.calls) + "\n")
'''.replace("__SRCPATH__", os.path.join(ROOT, "src"))


def run_with_keys(keys, timeout=6.0, columns=80, rows=24):
    """Run the driver in a pty, send `keys`, return (output, stderr)."""
    output, stderr, _slave, _during = _run(
        keys, timeout, columns=columns, rows=rows
    )
    return output, stderr


def last_frame(output):
    """The last frame the overlay drew: the screen as it is now.

    The overlay clears the screen before every frame, so the frames can be
    told apart. The last one is what the user is looking at.
    """
    return output.split("\x1b[H\x1b[2J")[-1]


def after_scroll(lines, rows):
    """What the terminal shows after the overlay wrote `lines`.

    Every line the overlay writes ends with a newline, so the cursor ends
    up on the row *after* the last line. When that row is past the bottom
    of the screen, the terminal scrolls by one row and the top line goes
    off the top.

    A test that only looks at the bytes the overlay wrote cannot see
    this: the frame is all there, and it is the terminal that scrolls.
    """
    if len(lines) >= rows:
        return lines[len(lines) - rows + 1 :]
    return lines


def _run(keys, timeout=6.0, keep_slave=False, columns=80, rows=24):
    """Run the driver in a pty, send `keys`.

    Returns (output, stderr, slave_fd, during).

    The slave fd is only returned -- and only kept open -- when
    `keep_slave` is set. The parent needs it to look at the pty's line
    discipline after the child exited: that is the only honest way to tell
    whether the overlay put the terminal back.
    """
    master, slave = pty.openpty()
    # Give the pty a real window size, the way a Herdr popup would have.
    try:
        import fcntl
        import struct
        import termios

        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", rows, columns, 0, 0))
    except Exception:  # noqa: BLE001 - not fatal, the overlay has a fallback
        pass

    proc = subprocess.Popen(
        [sys.executable, "-c", DRIVER],
        stdin=slave,
        stdout=slave,
        stderr=subprocess.PIPE,
        close_fds=True,
    )
    if keep_slave:
        # The parent keeps the slave open: the line discipline belongs to
        # the pty, so this is how the test sees the overlay's raw mode.
        slave_fd = slave
    else:
        slave_fd = None
        os.close(slave)

    output = b""
    deadline = time.time() + timeout
    # Give the overlay a moment to enter raw mode and render.
    time.sleep(0.4)
    during = None
    if keep_slave:
        import termios as termios_mod

        during = termios_mod.tcgetattr(slave)
    for key in keys:
        try:
            os.write(master, key)
        except OSError:
            break
        time.sleep(0.25)
        while select.select([master], [], [], 0.05)[0]:
            try:
                chunk = os.read(master, 65536)
            except OSError:
                break
            if not chunk:
                break
            output += chunk

    # Drain whatever is left.
    while select.select([master], [], [], 0.3)[0]:
        try:
            chunk = os.read(master, 65536)
        except OSError:
            break
        if not chunk:
            break
        output += chunk

    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=2)
    os.close(master)
    stderr = proc.stderr.read().decode("utf-8", "replace")
    proc.stderr.close()
    return output.decode("utf-8", "replace"), stderr, slave_fd, during


class TestOverlayPty(unittest.TestCase):
    def test_renders_the_root_layer_then_exits_on_escape(self):
        out, err = run_with_keys([b"\x1b"])
        self.assertIn("layers", out)
        self.assertIn("navigation", out)
        self.assertIn("panes", out)
        self.assertIn("EXIT=0", err)

    def test_entering_a_layer_and_going_back(self):
        out, err = run_with_keys([b"p", b"\x1b", b"\x1b"])
        # The panes layer has items the root does not.
        self.assertIn("split right", out)
        self.assertIn("close pane", out)
        self.assertIn("EXIT=0", err)

    def test_an_action_is_executed_over_the_socket(self):
        out, err = run_with_keys([b"p", b"v"])
        self.assertIn("pane.split", err)
        self.assertIn("'direction': 'right'", err)
        self.assertIn("EXIT=0", err)

    def test_control_c_exits(self):
        out, err = run_with_keys([b"\x03"])
        self.assertIn("EXIT=0", err)

    def test_two_keys_in_one_read_are_both_handled(self):
        # A read from the terminal can return more than one keystroke.
        # Decoding the whole read as one key loses both of them: "pv" used to
        # arrive as a single unknown key, so the layer never opened and the
        # split never happened.
        out, err = run_with_keys([b"pv"])
        self.assertIn("pane.split", err)
        self.assertIn("'direction': 'right'", err)
        self.assertIn("EXIT=0", err)

    def test_the_terminal_is_restored_on_exit(self):
        # The overlay must put the tty back, otherwise the user's shell ends up
        # in raw mode after the popup closes.
        #
        # This has to be checked on the pty's line discipline, not on the
        # output. The restore block writes "\x1b[0m" whether or not
        # tcsetattr ran, so an assertion on the output passes even with the
        # restore deleted. The slave fd is kept open in the parent so it can
        # be inspected after the child has exited.
        import termios

        out, err, slave, during = _run([b"\x1b"], keep_slave=True)
        try:
            self.assertIn("EXIT=0", err)
            self.assertFalse(
                during[3] & termios.ICANON,
                "the overlay never entered raw mode, so the restore proves nothing",
            )
            after = termios.tcgetattr(slave)
            self.assertTrue(
                after[3] & termios.ICANON,
                "the terminal was left in raw mode after the overlay exited",
            )
        finally:
            os.close(slave)

    def test_every_line_ends_with_crlf(self):
        # Raw mode clears OPOST, so ONLCR is off and a bare "\n" does not
        # return the cursor to column 0. Without explicit CRLF the overlay
        # renders as a diagonal staircase: each line starts where the previous
        # one ended. This is the regression guard for that.
        out, err = run_with_keys([b"p", b"\x1b", b"\x1b"])
        without_crlf = out.replace("\r\n", "")
        self.assertNotIn(
            "\n", without_crlf, "the overlay wrote a bare newline; the output would staircase"
        )
        self.assertIn("\r\n", out)

    def test_no_line_is_wider_than_the_terminal(self):
        # A line exactly as wide as the terminal leaves the cursor in the
        # last column, and the next character wraps it to column 0 of a new
        # row: every line after it lands a column to the left and the whole
        # layout falls apart. Every line has to be one column short of the
        # terminal's width.
        #
        # The frame's borders are part of the line, so they are part of the
        # budget: the frame has to fit *inside* it. 53 columns is the
        # default -- the popup asks for 70% of an 80 column terminal.
        for columns in (20, 40, 53, 80):
            out, err = run_with_keys([b"n", b"\x1b", b"\x1b"], columns=columns)
            self.assertIn("navigation", out)
            plain = [ANSI.sub("", line) for line in out.split("\r\n")]
            # The frame has to be there, or this test is not testing it.
            self.assertTrue(
                any(line.startswith("╭") for line in plain),
                "no frame was drawn at %d columns" % columns,
            )
            for line in out.split("\r\n"):
                self.assertLessEqual(
                    display_width(line),
                    columns - 1,
                    "a line is %d columns wide in a %d column terminal: %r"
                    % (display_width(line), columns, line),
                )

    def test_the_right_border_is_at_the_same_column_on_every_line(self):
        # The frame is only a frame if its right edge does not wander.
        for columns in (20, 40, 53, 80):
            out, err = run_with_keys([b"n", b"\x1b", b"\x1b"], columns=columns)
            edges = set()
            for line in out.split("\r\n"):
                plain = ANSI.sub("", line)
                if plain.startswith("│") and plain.endswith("│"):
                    edges.add(plain.rindex("│"))
                elif plain.startswith("╭") or plain.startswith("╰"):
                    edges.add(plain.rindex("╮" if plain.startswith("╭") else "╯"))
            self.assertTrue(edges, "no framed line at %d columns" % columns)
            self.assertEqual(
                len(edges), 1, "the right border moves: %r" % sorted(edges)
            )

    def test_the_chord_column_lines_up(self):
        # The chord used to start in a different column on every row,
        # because the description was as long as it liked.
        #
        # The sample has to hold more than one chord. With a single chord
        # the assertion below holds for any layout at all, so the test
        # could not fail: the ragged layout it guards against passed it.
        out, err = run_with_keys([b"p", b"\x1b", b"\x1b"], columns=80)
        columns = []
        for line in out.split("\r\n"):
            plain = ANSI.sub("", line)
            if "C-a" in plain:
                columns.append(plain.index("C-a"))
        self.assertTrue(columns, "no chord was rendered")
        self.assertGreaterEqual(
            len(columns),
            2,
            "the sample holds %d chord(s), so it cannot fail" % len(columns),
        )
        self.assertEqual(len(set(columns)), 1, "the chords do not line up")


class TestOverlayScrollingPty(unittest.TestCase):
    """The reference view has more rows than the screen has lines.

    The rows that fell off the bottom used to be simply gone, and the
    layers that overflow are the ones that hurt. This drives the real
    overlay in a real pty, at a height where the list does not fit, and
    checks that the last row can be reached.
    """

    # A keymap with more bindings than the screen has lines, so the
    # reference view cannot fit.
    DRIVER = r'''
import os, sys
sys.path.insert(0, "__SRCPATH__")
from managr import keymap as keymap_mod, overlay as overlay_mod, tree as tree_mod

bindings = {}
for index in range(40):
    action = "focus_pane_%02d" % index
    bindings[action] = keymap_mod.Binding(action, "focus pane %02d" % index, "f%02d" % index)
km = keymap_mod.Keymap(prefix="C-a", bindings=bindings)
tree = tree_mod.build(keymap=km, labels={})
# The last row of the reference view, so the test does not have to guess it.
sys.stderr.write("LAST=" + tree.reference.items[-1].desc + "\n")
sys.stderr.write("COUNT=" + str(len(tree.reference.items)) + "\n")

class FakeClient:
    def call(self, method, params=None):
        return {}

ctx = __import__("managr.actions", fromlist=["Context"]).Context(FakeClient())
overlay_mod.Overlay(tree, ctx).run()
'''.replace("__SRCPATH__", os.path.join(ROOT, "src"))

    def _run_driver(self, keys, rows=14, columns=53, timeout=20.0):
        master, slave = pty.openpty()
        try:
            import fcntl
            import struct
            import termios

            fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", rows, columns, 0, 0))
        except Exception:  # noqa: BLE001 - not fatal, the overlay has a fallback
            pass

        proc = subprocess.Popen(
            [sys.executable, "-c", self.DRIVER],
            stdin=slave,
            stdout=slave,
            stderr=subprocess.PIPE,
            close_fds=True,
        )
        os.close(slave)

        output = b""
        time.sleep(0.5)
        for key in keys:
            try:
                os.write(master, key)
            except OSError:
                break
            time.sleep(0.2)
            while select.select([master], [], [], 0.05)[0]:
                try:
                    chunk = os.read(master, 65536)
                except OSError:
                    break
                if not chunk:
                    break
                output += chunk
        while select.select([master], [], [], 0.3)[0]:
            try:
                chunk = os.read(master, 65536)
            except OSError:
                break
            if not chunk:
                break
            output += chunk
        try:
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)
        os.close(master)
        stderr = proc.stderr.read().decode("utf-8", "replace")
        proc.stderr.close()
        return output.decode("utf-8", "replace"), stderr

    def test_the_last_row_of_the_reference_view_can_be_reached(self):
        # "?" opens the reference view, then the page keys scroll it.
        out, err = self._run_driver([b"?"] + [b"\x1b[6~"] * 8 + [b"\x03"])
        match = re.search(r"LAST=(.*)", err)
        self.assertIsNotNone(match, "the overlay never started: %r" % err)
        last = match.group(1).strip()
        self.assertTrue(last, "the reference view has no rows")
        frame = ANSI.sub("", last_frame(out))
        self.assertIn(last, frame)
        # The first row is gone: the window really moved.
        self.assertNotIn("focus pane 00", frame)

    def test_the_position_is_shown_in_the_frame(self):
        out, err = self._run_driver([b"?"] + [b"\x03"])
        frame = ANSI.sub("", last_frame(out))
        self.assertRegex(frame, r"\d+/\d+", "the frame does not say where we are")

    def test_the_reference_view_still_fits_the_screen(self):
        out, err = self._run_driver([b"?"] + [b"\x03"])
        frame = ANSI.sub("", last_frame(out))
        lines = [line for line in frame.split("\r\n") if line]
        # The frame's bottom border is on the screen: a row drawn over it
        # would push it off.
        self.assertTrue(lines[-1].startswith("╰"), "the bottom border is gone: %r" % lines[-1])
        # One row short of the screen: every line ends with a newline, so
        # the last one puts the cursor past the last row, and a frame that
        # fills the screen scrolls by one.
        self.assertLessEqual(len(lines), 13)

    def test_the_breadcrumb_is_on_the_screen(self):
        # The breadcrumb is in the top border, so it is the first thing
        # that goes when the frame is a row too tall: the frame scrolls,
        # and the top of it leaves the screen.
        #
        # The scroll is modelled here: the bytes the overlay wrote still
        # hold the whole frame, and it is the terminal that scrolls.
        out, err = self._run_driver([b"?"] + [b"\x03"])
        frame = ANSI.sub("", last_frame(out))
        lines = [line for line in frame.split("\r\n") if line]
        visible = after_scroll(lines, 14)
        self.assertTrue(
            visible and visible[0].startswith("╭"),
            "the top border scrolled off the screen: %r" % (visible[:1],),
        )
        self.assertIn(
            "all bindings", visible[0], "the breadcrumb is not in the top border"
        )

    def test_the_right_border_stays_in_its_column_while_scrolling(self):
        # The position sits in the bottom border, so the border has to keep
        # its width: a border that moves shifts every row after it.
        out, err = self._run_driver([b"?"] + [b"\x1b[6~"] * 3 + [b"\x03"])
        edges = set()
        for line in out.split("\r\n"):
            plain = ANSI.sub("", line)
            if plain.startswith("│") and plain.endswith("│"):
                edges.add(plain.rindex("│"))
            elif plain.startswith("╭") or plain.startswith("╰"):
                edges.add(plain.rindex("╮" if plain.startswith("╭") else "╯"))
        self.assertTrue(edges, "no framed line was drawn")
        self.assertEqual(len(edges), 1, "the right border moves: %r" % sorted(edges))

    def test_no_line_is_wider_than_the_terminal_while_scrolling(self):
        out, err = self._run_driver([b"?"] + [b"\x1b[6~"] * 3 + [b"\x03"])
        for line in out.split("\r\n"):
            self.assertLessEqual(
                display_width(line),
                52,
                "a line is %d columns wide in a 53 column terminal: %r"
                % (display_width(line), line),
            )

    def test_the_filter_narrows_the_layer(self):
        # The filter is driven end to end: "/" opens it, the letters are
        # the query, and Enter accepts it.
        out, err = run_with_keys([b"p", b"/", b"split", b"\r", b"\x03"], timeout=8.0)
        frame = ANSI.sub("", last_frame(out))
        self.assertIn("split right", frame)
        self.assertNotIn("close pane", frame)

    def test_every_line_ends_with_crlf_while_filtering(self):
        out, err = run_with_keys([b"p", b"/", b"split", b"\r", b"\x03"], timeout=8.0)
        self.assertNotIn("\n", out.replace("\r\n", ""))


class TestSignalRestoresTerminal(unittest.TestCase):
    """A signal must not leave the tty in raw mode.

    `finally` does not run when a process is killed by a signal, so the
    terminal restore has to happen in the signal handler too.
    """

    DRIVER = r'''
import os, sys, termios, time
sys.path.insert(0, "__SRCPATH__")
from managr import keymap as keymap_mod, overlay as overlay_mod, tree as tree_mod

km = keymap_mod.Keymap(prefix="C-a", bindings={})
tree = tree_mod.build(keymap=km, labels={})

class FakeClient:
    def call(self, method, params=None):
        return {}

ctx = __import__("managr.actions", fromlist=["Context"]).Context(FakeClient())
surface = overlay_mod.Overlay(tree, ctx)
surface.run()
'''.replace("__SRCPATH__", os.path.join(ROOT, "src"))

    def test_sigterm_leaves_the_terminal_restored(self):
        import fcntl
        import struct
        import termios as termios_mod

        master, slave = pty.openpty()
        fcntl.ioctl(slave, termios_mod.TIOCSWINSZ, struct.pack("HHHH", 24, 80, 0, 0))

        before = termios_mod.tcgetattr(slave)

        proc = subprocess.Popen(
            [sys.executable, "-c", self.DRIVER],
            stdin=slave,
            stdout=slave,
            stderr=subprocess.DEVNULL,
            close_fds=True,
        )
        os.close(slave)

        # Keep reading, or the pty buffer fills and the overlay blocks on
        # write -- which is exactly what made the first version of this test
        # hang instead of failing.
        import threading

        def drain():
            while proc.poll() is None:
                try:
                    if not select.select([master], [], [], 0.1)[0]:
                        continue
                    if not os.read(master, 65536):
                        return
                except OSError:
                    return

        reader = threading.Thread(target=drain, daemon=True)
        reader.start()
        time.sleep(1.0)

        during = termios_mod.tcgetattr(master)
        self.assertFalse(during[3] & termios_mod.ICANON, "the overlay never entered raw mode")

        proc.terminate()
        proc.wait(timeout=10)

        after = termios_mod.tcgetattr(master)
        self.assertTrue(
            after[3] & termios_mod.ICANON,
            "the terminal was left in raw mode after SIGTERM",
        )
        os.close(master)

    def test_sigterm_does_not_block_when_the_terminal_is_not_read(self):
        # The overlay writes to the pty. If nobody reads it, the pty's
        # output queue fills up and the overlay blocks in the middle of a
        # render. The signal handler must not write to the terminal either:
        # a write blocks exactly the same way, and a signal handler that
        # blocks means the process ignores SIGTERM and needs SIGKILL.
        import fcntl
        import struct
        import termios as termios_mod
        import threading

        master, slave = pty.openpty()
        fcntl.ioctl(slave, termios_mod.TIOCSWINSZ, struct.pack("HHHH", 24, 80, 0, 0))

        proc = subprocess.Popen(
            [sys.executable, "-c", self.DRIVER],
            stdin=slave,
            stdout=slave,
            stderr=subprocess.DEVNULL,
            close_fds=True,
        )
        # The slave stays open here: the line discipline belongs to the pty,
        # and the output queue is how the test sees that the child is
        # blocked writing.
        try:
            # Let the overlay enter raw mode and render first. Writing to a
            # pty that still has pending input makes tcsetattr block on
            # macOS, and the test would be measuring the wrong thing.
            time.sleep(1.0)

            # Fill the pty's output queue from this side, so the overlay's
            # next render cannot get out and the overlay blocks in the
            # middle of a write.
            os.set_blocking(slave, False)
            while True:
                try:
                    os.write(slave, b" " * 256)
                except BlockingIOError:
                    break
            os.set_blocking(slave, True)

            def feed():
                for _ in range(20):
                    try:
                        os.write(master, b"p")
                    except OSError:
                        return
                    time.sleep(0.05)

            feeder = threading.Thread(target=feed, daemon=True)
            feeder.start()
            time.sleep(1.0)

            queued = struct.unpack(
                "i", fcntl.ioctl(slave, termios_mod.TIOCOUTQ, b"\0" * 4)
            )[0]
            self.assertGreater(queued, 0, "the pty's output queue was empty")

            proc.terminate()
            try:
                proc.wait(timeout=8)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)
                self.fail(
                    "the overlay ignored SIGTERM: the signal handler blocked on a write"
                )
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=5)
            os.close(master)
            os.close(slave)


class TestNavigatrPty(unittest.TestCase):
    """The navigatr, end to end, in a real pty.

    The unit tests drive the overlay's state machine; this one drives the
    real thing through a terminal: the tree is drawn, the cursor is on the
    screen, and the jump goes out over the socket.
    """

    DRIVER = r'''
import os, sys
sys.path.insert(0, "__SRCPATH__")
from managr import keymap as keymap_mod, overlay as overlay_mod, tree as tree_mod

class FakeClient:
    def __init__(self):
        self.calls = []
    def call(self, method, params=None):
        self.calls.append((method, params))
        if method == "workspace.list":
            return {"workspaces": [
                {"workspace_id": "w1", "label": "dev", "focused": True},
                {"workspace_id": "w2", "label": "ops", "focused": False},
            ]}
        if method == "tab.list":
            return {"tabs": [
                {"tab_id": "w1:t1", "label": "code", "focused": True, "workspace_id": "w1"},
                {"tab_id": "w2:t1", "label": "main", "focused": False, "workspace_id": "w2"},
            ]}
        if method == "pane.list":
            return {"panes": [
                {"pane_id": "w1:p1", "focused": True, "workspace_id": "w1",
                 "tab_id": "w1:t1", "agent": "opencode", "agent_status": "idle",
                 "cwd": "/tmp/dev", "terminal_title_stripped": "OC | dev"},
                {"pane_id": "w2:p1", "focused": False, "workspace_id": "w2",
                 "tab_id": "w2:t1", "cwd": "/tmp/ops", "terminal_title_stripped": "bash"},
            ]}
        if method == "agent.list":
            return {"agents": [
                {"agent": "opencode", "agent_status": "idle", "pane_id": "w1:p1",
                 "terminal_title_stripped": "OC | dev"},
            ]}
        return {}

km = keymap_mod.Keymap(prefix="C-a", bindings={})
tree = tree_mod.build(keymap=km, labels={})
client = FakeClient()
ctx = __import__("managr.actions", fromlist=["Context"]).Context(client)
overlay_mod.Overlay(tree, ctx).run()
sys.stderr.write("CALLS=" + repr(client.calls) + "\n")
'''.replace("__SRCPATH__", os.path.join(ROOT, "src"))

    def _run_driver(self, keys, rows=24, columns=80, timeout=20.0):
        master, slave = pty.openpty()
        try:
            import fcntl
            import struct
            import termios

            fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", rows, columns, 0, 0))
        except Exception:  # noqa: BLE001 - the overlay has a size fallback
            pass

        proc = subprocess.Popen(
            [sys.executable, "-c", self.DRIVER],
            stdin=slave,
            stdout=slave,
            stderr=subprocess.PIPE,
            close_fds=True,
        )
        os.close(slave)

        output = b""
        time.sleep(0.5)
        for key in keys:
            try:
                os.write(master, key)
            except OSError:
                break
            time.sleep(0.25)
            while select.select([master], [], [], 0.05)[0]:
                try:
                    chunk = os.read(master, 65536)
                except OSError:
                    break
                if not chunk:
                    break
                output += chunk
        while select.select([master], [], [], 0.3)[0]:
            try:
                chunk = os.read(master, 65536)
            except OSError:
                break
            if not chunk:
                break
            output += chunk
        try:
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)
        os.close(master)
        stderr = proc.stderr.read().decode("utf-8", "replace")
        proc.stderr.close()
        return output.decode("utf-8", "replace"), stderr

    def test_the_navigatr_draws_the_tree_of_the_session(self):
        out, err = self._run_driver([b"j", b"\x03"])
        frame = ANSI.sub("", last_frame(out))
        self.assertIn("dev", frame)
        self.assertIn("code", frame)
        self.assertIn("OC | dev", frame)
        self.assertIn("opencode", frame)
        self.assertIn("ops", frame)
        self.assertIn("navigatr", frame)

    def test_the_cursor_is_on_the_screen(self):
        out, err = self._run_driver([b"j", b"\x03"])
        frame = ANSI.sub("", last_frame(out))
        self.assertIn(">", frame, "the cursor is not visible")

    def _tree_rows(self, frame):
        """The lines of the tree in the frame, in the order they are drawn."""
        return [
            line
            for line in frame.split("\r\n")
            if any(marker in line for marker in ("▾", "├─", "└─"))
        ]

    def test_the_content_starts_after_the_rows_own_marker(self):
        # The marker and the content it marks move together: the content
        # of a row starts one column after that row's own branch marker
        # ends. The tree markers are a different width on every row now,
        # so this is checked at more than one width.
        for columns in (40, 80):
            out, err = self._run_driver([b"j", b"\x03"], columns=columns)
            frame = ANSI.sub("", last_frame(out))
            rows = self._tree_rows(frame)
            self.assertTrue(rows, "no tree at %d columns" % columns)
            columns_of_content = []
            for line in rows:
                marker = None
                for candidate in ("▾", "├─", "└─"):
                    if candidate in line:
                        marker = candidate
                        break
                self.assertIsNotNone(marker, "a row has no marker: %r" % line)
                marker_end = line.index(marker) + len(marker)
                after = line[marker_end:]
                content = after.lstrip(" ")
                self.assertTrue(content, "the row has no content: %r" % line)
                start = marker_end + (len(after) - len(content))
                self.assertEqual(
                    start,
                    marker_end + 1,
                    "the content does not start after the marker at %d columns: %r"
                    % (columns, line),
                )
                columns_of_content.append(start)
            # And the tree opens outward: the rows are not all in one
            # column.
            self.assertGreater(
                len(set(columns_of_content)),
                1,
                "every row is in the same column at %d columns: %r"
                % (columns, columns_of_content),
            )

    def test_the_root_lists_the_navigatr_with_its_key(self):
        # The key that opens the navigatr used to be a hidden constant.
        # It is in the root now, with the key beside it.
        out, err = self._run_driver([b"\x03"])
        frame = ANSI.sub("", last_frame(out))
        self.assertIn("navigatr", frame, "the root does not list the navigatr")
        rows = [line for line in frame.split("\r\n") if "navigatr" in line]
        self.assertTrue(rows)
        # The key is in the row, at the start of it.
        self.assertRegex(rows[0], r"\bj\b")

    def test_escape_leaves_the_navigatr_and_the_overlay_stays_open(self):
        out, err = self._run_driver([b"j", b"\x1b", b"\x03"])
        frames = out.split("\x1b[H\x1b[2J")
        # The last frame is the one the user is left looking at: the root
        # layer again, because Escape left the navigatr.
        after = ANSI.sub("", frames[-1])
        self.assertIn("layers", after)
        # The breadcrumb no longer names the navigatr, and neither does
        # the hint: the root lists the navigatr as an entry, so the word
        # is on the screen -- but the overlay is not in it.
        self.assertNotIn("layers › navigatr", after)
        self.assertNotIn("the spaces, tabs and panes", after)
        # And the overlay is still running: the process only stopped
        # because the test sent ctrl-c.
        self.assertNotIn("Traceback", err)

    def test_enter_jumps_and_closes_the_overlay(self):
        out, err = self._run_driver([b"j", b"\r", b"\x03"])
        self.assertIn("'workspace.focus'", err)
        self.assertIn("'workspace_id': 'w1'", err)

    def test_every_line_ends_with_crlf(self):
        out, err = self._run_driver([b"j", b"\x03"])
        self.assertNotIn("\n", out.replace("\r\n", ""))

    def test_no_line_is_wider_than_the_terminal(self):
        for columns in (20, 40, 53, 80):
            out, err = self._run_driver([b"j", b"\x03"], columns=columns)
            for line in out.split("\r\n"):
                self.assertLessEqual(
                    display_width(line),
                    columns - 1,
                    "a line is %d columns wide in a %d column terminal: %r"
                    % (display_width(line), columns, line),
                )

    def test_the_right_border_is_at_the_same_column_on_every_line(self):
        for columns in (20, 40, 53, 80):
            out, err = self._run_driver([b"j", b"\x03"], columns=columns)
            edges = set()
            for line in out.split("\r\n"):
                plain = ANSI.sub("", line)
                if plain.startswith("│") and plain.endswith("│"):
                    edges.add(plain.rindex("│"))
                elif plain.startswith("╭") or plain.startswith("╰"):
                    edges.add(plain.rindex("╮" if plain.startswith("╭") else "╯"))
            self.assertTrue(edges, "no framed line at %d columns" % columns)
            self.assertEqual(
                len(edges), 1, "the right border moves at %d columns: %r"
                % (columns, sorted(edges))
            )

    def test_the_agents_list_draws_the_agents_of_the_session(self):
        # The second navigatr: the same program, a flat list.
        out, err = self._run_driver([b"a", b"\x03"])
        frame = ANSI.sub("", last_frame(out))
        self.assertIn("layers › agents", frame)
        self.assertIn("opencode", frame)
        # The status is a glyph and a word.
        self.assertIn("●", frame)
        self.assertIn("idle", frame)
        # And the footer says which keys do what.
        self.assertIn("1-9 agent", frame)

    def test_enter_on_an_agent_jumps_to_its_pane(self):
        # `agent.focus` takes the agent's pane: the parameter name was read
        # off the live server.
        out, err = self._run_driver([b"a", b"\r", b"\x03"])
        self.assertIn("'agent.focus'", err)
        self.assertIn("'target': 'w1:p1'", err)

    def test_no_line_is_wider_than_the_terminal_in_the_agents_list(self):
        for columns in (20, 40, 53, 80):
            out, err = self._run_driver([b"a", b"\x03"], columns=columns)
            for line in out.split("\r\n"):
                self.assertLessEqual(
                    display_width(line),
                    columns - 1,
                    "a line is %d columns wide in a %d column terminal: %r"
                    % (display_width(line), columns, line),
                )




if __name__ == "__main__":
    unittest.main(verbosity=2)
