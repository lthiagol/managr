#!/usr/bin/env python3
"""Independently verify the fixes, by breaking the code on purpose.

This does not trust anyone's report -- not the agent's, not the tests'. Every
fix gets a revert-check: the code is reverted in a scratch copy of the tree
and the copied test suite has to fail. A guard that stays green when the bug
is back proves nothing.

The copy has to be the working directory when the tests run: a test computes
ROOT from its own __file__, so running it from anywhere else exercises the
original source and proves nothing.

    python3 scripts/verify_claims.py
"""

import os
import pty
import re
import select
import shutil
import subprocess
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# The sibling plugin. The two plugins live in one repository and the guards
# in both of them have to be real, so the checks below break the sibling's
# code too when it is here.
V1_ROOT = os.path.join(os.path.dirname(ROOT), "v1")

RESULTS = []

# The escape sequences the overlay writes. The overlay is coloured -- the
# accent comes from the user's own Herdr theme -- so the checks below look
# at the text the terminal shows, not at the bytes: a check for a piece of
# text must not fail because of an escape sequence in the middle of it.
ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")


def visible(text):
    """`text` without the escape sequences: what the terminal shows."""
    return ANSI.sub("", text)


def report(name, ok, detail=""):
    RESULTS.append((name, ok, detail))
    print("  %s %s%s" % ("PASS" if ok else "FAIL", name, ("  -- " + detail) if detail else ""))


def skip(name, detail):
    print("  SKIP %s  -- %s" % (name, detail))


def run_tests(suite, cwd=None):
    """Run a test file. With `cwd`, run it from there so that the test's own
    ROOT resolves to the copy, not to the original source tree."""
    if cwd:
        path = os.path.join("tests", suite)
    else:
        path = os.path.join(ROOT, "tests", suite)
    proc = subprocess.run(
        [sys.executable, path],
        capture_output=True,
        text=True,
        cwd=cwd or os.path.join(ROOT),
    )
    return proc.returncode == 0, proc.stdout + proc.stderr


def copy_tree():
    """A scratch copy of the plugin, to break on purpose.

    The compiled caches are left out: a stale `.pyc` whose recorded
    timestamp and size still match the broken file is used instead of the
    file, and the suite then passes for the wrong reason.
    """
    work = tempfile.mkdtemp(prefix="managr-verify-")
    ignore = shutil.ignore_patterns("__pycache__")
    for name in ("src", "tests", "bin", "scripts"):
        shutil.copytree(
            os.path.join(ROOT, name), os.path.join(work, name), ignore=ignore
        )
    shutil.copy2(
        os.path.join(ROOT, "herdr-plugin.toml"), os.path.join(work, "herdr-plugin.toml")
    )
    return work


def copy_v1_tree():
    """A scratch copy of the sibling plugin, or None when it is not here."""
    if not os.path.isdir(os.path.join(V1_ROOT, "src")):
        return None
    work = tempfile.mkdtemp(prefix="managr-verify-v1-")
    ignore = shutil.ignore_patterns("__pycache__")
    for name in ("src", "tests", "bin", "scripts"):
        source = os.path.join(V1_ROOT, name)
        if os.path.isdir(source):
            shutil.copytree(source, os.path.join(work, name), ignore=ignore)
    return work


def break_code(work, edits):
    """Break the code in a copy. Returns None, or why it could not."""
    for relpath, old, new in edits:
        target = os.path.join(work, relpath)
        with open(target, "r", encoding="utf-8") as handle:
            text = handle.read()
        if old not in text:
            return "could not find the code to revert in %s" % relpath
        with open(target, "w", encoding="utf-8") as handle:
            handle.write(text.replace(old, new, 1))
    return None


def check_revert(name, edits, suite, description, tree=None):
    """Break the code in a copy and confirm the copied suite catches it.

    `edits` is a list of (relative path, old, new). Every one of them has to
    apply: a revert that does not match anything would leave the code
    untouched, and the suite would pass for the wrong reason.
    """
    work = tree() if tree else copy_tree()
    if work is None:
        skip(name, "the sibling plugin is not here")
        return
    try:
        problem = break_code(work, edits)
        if problem is not None:
            report(name, False, problem)
            return
        passed, output = run_tests(suite, cwd=work)
        report(
            name,
            not passed,
            "the suite failed as it should" if not passed
            else "the suite still passed, so the guard is fake",
        )
        if passed:
            print(output[-2000:])
    finally:
        shutil.rmtree(work, ignore_errors=True)


def drive(keys, timeout=8.0, columns=100, rows=30):
    """Run the overlay in a pty, send `keys`, return (output, stderr)."""
    driver = r'''
import os, sys
sys.path.insert(0, "__SRC__")
from managr import keymap as km, overlay as ov, tree as tr
kmk = km.Keymap(prefix="C-a", bindings={
    "focus_pane_left": km.Binding("focus_pane_left", "focus left", "h"),
    "split_vertical": km.Binding("split_vertical", "split right", "v"),
})
tree = tr.build(keymap=kmk, labels={})
class C:
    def __init__(self): self.calls = []
    def call(self, m, p=None):
        self.calls.append((m, p))
        # Report as it happens: a keep=true action never returns, so printing
        # after run() would never happen.
        sys.stderr.write("CALL " + m + "\n")
        sys.stderr.flush()
        return {"type": "pane_focus_direction", "focus": {"changed": True}}
ctx = __import__("managr.actions", fromlist=["Context"]).Context(C())
ov.Overlay(tree, ctx).run()
sys.stderr.write("CALLS=" + repr(ctx.client.calls) + "\n")
'''.replace("__SRC__", os.path.join(ROOT, "src"))

    master, slave = pty.openpty()
    try:
        import fcntl
        import struct
        import termios

        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", rows, columns, 0, 0))
    except Exception:
        pass

    proc = subprocess.Popen(
        [sys.executable, "-c", driver],
        stdin=slave,
        stdout=slave,
        stderr=subprocess.PIPE,
        cwd=ROOT,
    )
    os.close(slave)
    out = b""
    time.sleep(1.0)
    for key in keys:
        try:
            os.write(master, key)
        except OSError:
            break
        time.sleep(0.6)
        while select.select([master], [], [], 0.05)[0]:
            try:
                chunk = os.read(master, 65536)
            except OSError:
                break
            if not chunk:
                break
            out += chunk
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=5)
    os.close(master)
    stderr = proc.stderr.read().decode("utf-8", "replace")
    proc.stderr.close()
    return out.decode("utf-8", "replace"), stderr


# -- the fixes, checked by reverting them ---------------------------------

# A reader that decodes a whole read as one key: the bug the KeyReader was
# written to fix.
NAIVE_READER = '''class KeyReader:
    """The reader before the fix: the whole read is one key."""

    def __init__(self, fd, escape_timeout=ESC_TIMEOUT):
        self.fd = fd
        self.escape_timeout = escape_timeout
        self._buffer = b""

    def feed(self, data):
        self._buffer += data

    def keys(self):
        if not self._buffer:
            return
        key = decode(self._buffer)
        self._buffer = b""
        yield key

    def next_key(self):
        if not self._buffer:
            return None
        key = decode(self._buffer)
        self._buffer = b""
        return key


'''


def check_multikey():
    """Two keys in one write must both be handled."""
    out, err = drive([b"pv"])
    calls = "pane.split" in err
    report(
        "two keys in one read are both handled",
        calls,
        "pane.split was not called" if not calls else "pane.split was called",
    )


def check_keep_open():
    """A `keep = true` action must leave the overlay open."""
    out, err = drive([b"n", b"h"])
    called = "pane.focus_direction" in err
    # If the overlay had closed on the first action, the process would have
    # exited and the nav layer would not still be rendered.
    seen = visible(out)
    still_open = "layers › navigation" in seen and "focus left" in seen
    report(
        "a keep=true action leaves the overlay open",
        called and still_open,
        "the action ran and the overlay stayed on the nav layer"
        if (called and still_open)
        else "called=%s still_open=%s" % (called, still_open),
    )


def check_restore_guard_is_real():
    """Remove the terminal restore and confirm the test catches it."""
    check_revert(
        "removing the terminal restore fails the pty tests",
        [(
            "src/managr/overlay.py",
            "termios.tcsetattr(fd, when, old)",
            "pass  # restore removed on purpose",
        )],
        "test_overlay_pty.py",
        "the restore",
    )


def check_crlf_guard_is_real():
    """Revert NL to a bare newline and confirm the staircase test catches it."""
    check_revert(
        "reverting CRLF fails the pty tests",
        [("src/managr/overlay.py", 'NL = "\\r\\n"', 'NL = "\\n"')],
        "test_overlay_pty.py",
        "the CRLF",
    )


def check_keep_guard_is_real():
    """Revert the keep fix and confirm its test catches it."""
    check_revert(
        "reverting the keep fix fails the layer tests",
        [(
            "src/managr/overlay.py",
            "if outcome.close or not (outcome.keep_open or item.keep):",
            "if outcome.close or not outcome.keep_open:",
        )],
        "test_layers.py",
        "the keep check",
    )


def check_keyreader_guard_is_real():
    """Put the naive reader back and confirm the decoding tests catch it."""
    path = os.path.join("src", "managr", "overlay.py")
    with open(os.path.join(ROOT, path), "r", encoding="utf-8") as handle:
        text = handle.read()
    start = text.index("class KeyReader:")
    end = text.index("class Overlay:")
    check_revert(
        "reverting the key reader fails the layer tests",
        [(path, text[start:end], NAIVE_READER)],
        "test_layers.py",
        "the key reader",
    )


def check_signal_guard_is_real():
    """Stop installing the signal handler and confirm the pty test catches it."""
    check_revert(
        "removing the signal handler fails the pty tests",
        [(
            "src/managr/overlay.py",
            "        for signum in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT):\n"
            "            try:\n"
            "                signal.signal(signum, handler)\n"
            "            except (OSError, ValueError):\n"
            "                pass\n",
            "        for signum in ():\n"
            "            try:\n"
            "                signal.signal(signum, handler)\n"
            "            except (OSError, ValueError):\n"
            "                pass\n",
        )],
        "test_overlay_pty.py",
        "the signal handler",
    )


def check_blocking_handler_guard_is_real():
    """Write to the terminal from the signal handler and confirm it hangs."""
    check_revert(
        "a blocking signal handler fails the pty tests",
        [(
            "src/managr/overlay.py",
            "        for signum in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT):\n"
            "            try:\n"
            "                signal.signal(signum, signal.SIG_DFL)\n"
            "            except (OSError, ValueError):\n"
            "                pass\n"
            "\n"
            "    def _clear_screen(self):",
            "        for signum in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT):\n"
            "            try:\n"
            "                signal.signal(signum, signal.SIG_DFL)\n"
            "            except (OSError, ValueError):\n"
            "                pass\n"
            "        self.out.write(\"\\x1b[0m\\x1b[2J\\x1b[H\")\n"
            "        self.out.flush()\n"
            "\n"
            "    def _clear_screen(self):",
        )],
        "test_overlay_pty.py",
        "the blocking handler",
    )


def check_dirty_decoder_guard_is_real():
    """Leave the decoder dirty after a control key and confirm it is caught."""
    check_revert(
        "a dirty decoder after a control key fails the layer tests",
        [(
            "src/managr/overlay.py",
            "                # A control key interrupts a half-decoded character. The\n"
            "                # decoder still holds the bytes of it, and the *next*\n"
            "                # keystroke would decode to garbage and be dropped.\n"
            "                self._reset_partial()\n",
            "",
        )],
        "test_layers.py",
        "the decoder reset",
    )


def check_swallowed_escape_guard_is_real():
    """Swallow an escape behind a half character and confirm it is caught."""
    check_revert(
        "a swallowed escape fails the layer tests",
        [(
            "src/managr/overlay.py",
            "            if byte == ESC_BYTES:\n"
            "                # An escape sequence part-way through the buffer, behind a\n"
            "                # half-decoded character. The character is garbage now; drop\n"
            "                # it and let the escape branch decode the sequence.\n"
            "                self._reset_partial()\n"
            "                return self._escape_key()\n",
            "",
        )],
        "test_layers.py",
        "the escape branch",
    )


def check_csi_final_guard_is_real():
    """Accept any byte as the end of a CSI sequence and confirm it is caught."""
    check_revert(
        "a loose CSI final byte fails the layer tests",
        [(
            "src/managr/overlay.py",
            "        if byte in CSI_FINALS or byte == b\"~\":\n"
            "            return index\n"
            "        # A byte that cannot end a sequence: this is not a CSI sequence at\n"
            "        # all, so the \"[\" was a literal \"[\" the user typed.\n"
            "        return None\n",
            "        return index\n",
        )],
        "test_layers.py",
        "the CSI final byte",
    )


def check_user_items_guard_is_real():
    """Drop a user's items for an unredefined layer and confirm it is caught."""
    check_revert(
        "dropping a user's items fails the layer tests",
        [(
            "src/managr/tree.py",
            "                if layer_id not in user_order:\n"
            "                    items[layer_id] = entries\n",
            "                if False:\n"
            "                    items[layer_id] = entries\n",
        )],
        "test_layers.py",
        "the user items",
    )


def check_layer_key_guard_is_real():
    """Overwrite a redefined layer's key and confirm it is caught."""
    check_revert(
        "dropping a layer's key fails the layer tests",
        [(
            "src/managr/tree.py",
            "            if layer.has_key:\n"
            "                fields[\"key\"] = layer.key\n",
            "            fields[\"key\"] = layer.key\n",
        )],
        "test_layers.py",
        "the layer key",
    )


def check_custom_reference_guard_is_real():
    """Leave the custom commands out of the reference view."""
    check_revert(
        "leaving out the custom commands fails the layer tests",
        [(
            "src/managr/tree.py",
            "    for entry in keymap.custom:\n",
            "    for entry in []:\n",
        )],
        "test_layers.py",
        "the custom commands",
    )


def check_duplicate_keys_guard_is_real():
    """Stop reporting duplicate keys and confirm the test catches it."""
    check_revert(
        "not reporting duplicate keys fails the layer tests",
        [(
            "src/managr/overlay.py",
            "            if item.key in lookup:\n"
            "                duplicates.append(item.key)\n"
            "                continue\n",
            "            if item.key in lookup:\n"
            "                continue\n",
        )],
        "test_layers.py",
        "the duplicate keys",
    )


def check_reference_key_guard_is_real():
    """Hardcode the reference key and confirm the test catches it."""
    check_revert(
        "hardcoding the reference key fails the layer tests",
        [(
            "src/managr/tree.py",
            "    reference_key = settings.get(\"reference_key\") or \"\"\n",
            "    reference_key = \"?\"\n",
        )],
        "test_layers.py",
        "the reference key",
    )


def check_unreachable_warning_guard_is_real():
    """Stop warning about unreachable items and confirm the test catches it."""
    check_revert(
        "not warning about unreachable items fails the layer tests",
        [(
            "src/managr/tree.py",
            "    _warn_unreachable(layers, order, synthetic)\n",
            "",
        )],
        "test_layers.py",
        "the unreachable warning",
    )


def check_live_check_guard_is_real():
    """Let a silent no-op pass the live check and confirm the test catches it."""
    check_revert(
        "a silent no-op passing the live check fails its own check",
        [(
            "scripts/live_check.py",
            "    return False, \"no feedback for a no-op action\"",
            "    return True, \"no feedback for a no-op action\"",
        )],
        "test_layers.py",
        "the live check status",
    )


def check_anti_wrap_guard_is_real():
    """Let a line be as wide as the terminal and confirm the test catches it.

    A line of exactly `cols` columns leaves the cursor in the last column,
    and the next character wraps it to column 0 of a new row: everything
    after it lands a column to the left.
    """
    check_revert(
        "removing the anti-wrap cap fails the pty tests",
        [(
            "src/managr/overlay.py",
            "width = max(1, columns - 1)",
            "width = columns",
        )],
        "test_overlay_pty.py",
        "the anti-wrap cap",
    )


def check_measured_columns_guard_is_real():
    """Hardcode the column widths and confirm the layout tests catch it."""
    check_revert(
        "hardcoded column widths fail the layout tests",
        [(
            "src/managr/table.py",
            "    width = 0\n"
            "    for row in grid:\n"
            "        if position < len(row):\n"
            "            width = max(width, visible_width(row[position]))\n"
            "    return width\n",
            "    return 8\n",
        )],
        "test_table.py",
        "the measured column widths",
    )


def check_column_padding_guard_is_real():
    """Stop padding the columns and confirm the layout tests catch it.

    Without the padding the columns are ragged: the chord starts in a
    different column on every row, which is the bug the table was written
    to fix.
    """
    check_revert(
        "unpadded columns fail the layout tests",
        [(
            "src/managr/table.py",
            "    if align == \"right\":\n"
            "        return \" \" * pad + text\n"
            "    return text + \" \" * pad\n",
            "    return text\n",
        )],
        "test_table.py",
        "the column padding",
    )


def check_note_priority_guard_is_real():
    """Drop the chord before the note again and confirm the test catches it.

    A note says "press this in herdr", so it means nothing without the
    chord to press: dropping the chord and keeping the note leaves the
    user with an instruction and no key.
    """
    check_revert(
        "dropping the chord before the note fails the layout tests",
        [(
            "src/managr/overlay.py",
            "    table.Column(name=\"chord\", optional=True, priority=1, min_width=1),\n"
            "    table.Column(name=\"note\", optional=True, priority=0, min_width=1),\n",
            "    table.Column(name=\"chord\", optional=True, priority=0, min_width=1),\n"
            "    table.Column(name=\"note\", optional=True, priority=1, min_width=1),\n",
        )],
        "test_layers.py",
        "the note priority",
    )


def check_input_budget_guard_is_real():
    """Truncate the prompt and the buffer separately again.

    They are two halves of one line, so they share one budget: truncated
    separately they give a line up to 2*width+1 columns wide, which wraps
    the cursor and lands the next line in the middle of a row.
    """
    check_revert(
        "a prompt and a buffer truncated separately fail the layout tests",
        [(
            "src/managr/overlay.py",
            "        prompt = truncate(self.input_prompt, max(1, width // 2))\n"
            "        room = width - table.visible_width(prompt) - 1\n"
            "        buffer = truncate(self.input_buffer, max(0, room))\n"
            "        return prompt, buffer, \" \" if room > 0 else \"\"\n",
            "        prompt = truncate(self.input_prompt, width)\n"
            "        buffer = truncate(self.input_buffer, width)\n"
            "        return prompt, buffer, \" \"\n",
        ), (
            "src/managr/overlay.py",
            "                truncate(\"end to confirm · esc to cancel\", inner),\n",

            "                \"end to confirm · esc to cancel\",\n",
        )],
        "test_layers.py",
        "the input budget",
    )


def check_chord_alignment_guard_is_real():
    """Make the columns ragged again and confirm the pty test catches it.

    The chord used to start in a different column on every row, because
    the description was as long as it liked. The guard only means
    something when the sample holds more than one chord.
    """
    check_revert(
        "a ragged chord column fails the pty tests",
        [(
            "src/managr/table.py",
            "    if align == \"right\":\n"
            "        return \" \" * pad + text\n"
            "    return text + \" \" * pad\n",
            "    return text\n",
        )],
        "test_overlay_pty.py",
        "the chord alignment",
    )


def check_width_measurement_guard_is_real():
    """Break `visible_width` and confirm the layout tests catch it.

    The table test measures the width itself: a test that asks the code
    under test how wide its own output is proves nothing.
    """
    check_revert(
        "a broken visible_width fails the layout tests",
        [(
            "src/managr/table.py",
            "    width = 0\n"
            "    for char in strip_ansi(text):\n"
            "        width += char_width(char)\n"
            "    return width\n",
            "    return 1\n",
        )],
        "test_table.py",
        "the independent width measurement",
    )


def check_control_char_guard_is_real():
    """Let the control characters through again and confirm it is caught.

    The cells hold text the user wrote: a stray escape makes the terminal
    wait for a final byte and swallow the rest of the line, and a
    vertical tab moves the cursor.
    """
    check_revert(
        "control characters in user data fail the layout tests",
        [(
            "src/managr/table.py",
            "    text = text.replace(\"\\r\\n\", \" \").replace(\"\\r\", \" \").replace(\"\\n\", \" \")\n"
            "    return _clean(text)\n",
            "    return text.replace(\"\\r\\n\", \" \").replace(\"\\r\", \" \").replace(\"\\n\", \" \")\n",
        )],
        "test_layers.py",
        "the control characters",
    )


def check_frame_budget_guard_is_real():
    """Draw the frame outside the anti-wrap budget and confirm it is caught.

    The frame's borders are part of the line, so they have to fit inside the
    `cols - 1` budget. A frame that claims the content's columns pushes the
    right border past the edge of the terminal, and every line after it
    lands a column to the left.
    """
    check_revert(
        "a frame outside the anti-wrap budget fails the pty tests",
        [(
            "src/managr/overlay.py",
            "        inner = width - 2 if frame else width",
            "        inner = width",
        )],
        "test_overlay_pty.py",
        "the frame's budget",
    )


def check_accent_guard_is_real():
    """Stop reading Herdr's config and confirm the test catches it."""
    check_revert(
        "an accent that ignores Herdr's config fails the visuals tests",
        [(
            "src/managr/visuals.py",
            "    offer(\"the plugin's accent setting\", settings.get(\"accent\"))\n"
            "    offer(\"Herdr's theme.custom.accent\", _dig(herdr, \"theme\", \"custom\", \"accent\"))\n"
            "    offer(\"Herdr's ui.accent\", _dig(herdr, \"ui\", \"accent\"))\n",
            "    offer(\"the plugin's accent setting\", settings.get(\"accent\"))\n",
        )],
        "test_visuals.py",
        "the accent from Herdr's config",
    )


def check_contrast_guard_is_real():
    """Use an unreadable accent and confirm the test catches it.

    The popup is drawn over whatever the terminal shows, and the plugin
    cannot know what that is. An accent that fails against both a dark and
    a light background is unreadable on either.
    """
    check_revert(
        "an unreadable accent fails the visuals tests",
        [(
            "src/managr/visuals.py",
            "        if contrast(rgb, DARK_BACKGROUND) >= MINIMUM_CONTRAST:\n"
            "            accent, source = rgb, label\n"
            "            break\n"
            "        if contrast(rgb, LIGHT_BACKGROUND) >= MINIMUM_CONTRAST:\n"
            "            accent, source = rgb, label\n"
            "            break\n",
            "        accent, source = rgb, label\n"
            "        break\n",
        )],
        "test_visuals.py",
        "the contrast guard",
    )


def check_glyph_width_guard_is_real():
    """Count the Nerd Font glyph as two columns and confirm it is caught.

    An icon that took two columns would push every row one column to the
    right, and the right border of the frame with it.
    """
    check_revert(
        "a two-column glyph fails the layout tests",
        [(
            "src/managr/table.py",
            "    if 0xE000 <= ord(char) <= 0xF8FF:\n"
            "        # The private use area, which is where the Nerd Font icons live: one\n"
            "        # column, even when the font draws them double width. A terminal\n"
            "        # that cannot draw the glyph shows a box, which is one column too.\n"
            "        return 1\n",
            "    if 0xE000 <= ord(char) <= 0xF8FF:\n"
            "        return 2\n",
        )],
        "test_table.py",
        "the one-column glyph",
    )


def check_icons_guard_is_real():
    """Ignore the `icons` setting and confirm the test catches it."""
    check_revert(
        "ignoring the icons setting fails the visuals tests",
        [(
            "src/managr/visuals.py",
            "        return bool(self.settings.get(\"icons\", True))",
            "        return True",
        )],
        "test_visuals.py",
        "the icons setting",
    )


# -- the scrolling and the filter -------------------------------------------

# The keys that scroll. `j` and `k` are not among them: they are actions in
# the panes and the navigation layers.
SCROLL_KEYS = (
    "        if key.name == \"up\":\n"
    "            self._scroll(-1)\n"
    "            return True\n"
    "        if key.name == \"down\":\n"
    "            self._scroll(1)\n"
    "            return True\n"
    "        if key.name == \"pageup\":\n"
    "            self._scroll(-self._page())\n"
    "            return True\n"
    "        if key.name == \"pagedown\":\n"
    "            self._scroll(self._page())\n"
    "            return True\n"
    "        if key.name == \"ctrl-d\":\n"
    "            self._scroll(self._page())\n"
    "            return True\n"
    "        if key.name == \"ctrl-u\" or (key.name == \"char\" and key.char == CTRL_U):\n"
    "            self._scroll(-self._page())\n"
    "            return True\n"
)

# The filter that narrows the rows.
FILTER_NARROWS = (
    "        query = self._query().lower()\n"
    "        if not query:\n"
    "            return list(layer.items)"
)


def check_scroll_guard_is_real():
    """Take the scroll away and confirm the tests catch it.

    A layer that does not fit on the screen used to be cut off: the rows
    that fell off the bottom were simply gone.
    """
    check_revert(
        "an overlay that does not scroll fails the layer tests",
        [(
            "src/managr/overlay.py",
            SCROLL_KEYS,
            "",
        )],
        "test_layers.py",
        "the scroll keys",
    )


def check_scroll_clamp_guard_is_real():
    """Let the window move past either end and confirm it is caught."""
    check_revert(
        "a window that scrolls past the end fails the layer tests",
        [(
            "src/managr/overlay.py",
            "        self.scroll = max(0, min(start + delta, limit))",
            "        self.scroll = start + delta",
        )],
        "test_layers.py",
        "the scroll clamp",
    )


def check_row_budget_guard_is_real():
    """Guess the row budget again and confirm the tests catch it.

    The budget is computed from the chrome, so the overlay uses the rows it
    has. A magic constant leaves rows on the screen.
    """
    check_revert(
        "a magic row budget fails the layer tests",
        [(
            "src/managr/overlay.py",
            "        used = chrome_rows + (FRAME_ROWS if frame else PLAIN_ROWS)\n"
            "        used += self._detail_rows(rows)\n"
            "        return max(0, rows - used)",
            "        return max(1, rows - 6)",
        )],
        "test_layers.py",
        "the row budget",
    )


def check_row_budget_overflow_guard_is_real():
    """Draw more rows than fit and confirm the tests catch it.

    A row over the bottom border pushes that border off the screen.
    """
    check_revert(
        "rows drawn over the bottom border fail the layer tests",
        [(
            "src/managr/overlay.py",
            "        used = chrome_rows + (FRAME_ROWS if frame else PLAIN_ROWS)\n"
            "        used += self._detail_rows(rows)\n"
            "        return max(0, rows - used)",
            "        used = chrome_rows + (FRAME_ROWS if frame else PLAIN_ROWS)\n"
            "        used += self._detail_rows(rows)\n"
            "        return max(0, rows - used + 5)",
        )],
        "test_layers.py",
        "the row budget overflow",
    )


def check_frame_height_guard_is_real():
    """Fill the screen with the frame and confirm the tests catch it.

    Every line the overlay writes ends with a newline, so the last newline
    leaves the cursor on the row *after* the last line. A frame that fills
    the screen exactly scrolls by one row, and the top border -- the
    breadcrumb -- goes off the top of the screen.
    """
    check_revert(
        "a frame that is one row too tall fails the layer tests",
        [(
            "src/managr/overlay.py",
            "        used = chrome_rows + (FRAME_ROWS if frame else PLAIN_ROWS)\n"
            "        used += self._detail_rows(rows)\n"
            "        return max(0, rows - used)",
            "        used = chrome_rows + (2 if frame else 1)\n"
            "        used += self._detail_rows(rows)\n"
            "        return max(0, rows - used)",
        )],
        "test_layers.py",
        "the row the cursor needs",
    )


def check_frame_height_pty_guard_is_real():
    """Fill the screen with the frame and confirm the pty test catches it.

    The same bug, seen end to end: the frame is drawn on a real terminal,
    the last newline scrolls it, and the breadcrumb goes off the top.
    """
    check_revert(
        "a frame that is one row too tall fails the pty tests",
        [(
            "src/managr/overlay.py",
            "        used = chrome_rows + (FRAME_ROWS if frame else PLAIN_ROWS)\n"
            "        used += self._detail_rows(rows)\n"
            "        return max(0, rows - used)",
            "        used = chrome_rows + (2 if frame else 1)\n"
            "        used += self._detail_rows(rows)\n"
            "        return max(0, rows - used)",
        )],
        "test_overlay_pty.py",
        "the row the cursor needs, in a pty",
    )


def check_position_guard_is_real():
    """Stop saying where we are in the list and confirm it is caught."""
    check_revert(
        "a frame that does not say where we are fails the layer tests",
        [(
            "src/managr/overlay.py",
            "        if not self._view_total or self._view_total <= self._view_budget:\n"
            "            return \"\"",
            "        return \"\"",
        )],
        "test_layers.py",
        "the position indicator",
    )


def check_position_budget_guard_is_real():
    """Let the position push the border past the edge and confirm it is caught."""
    check_revert(
        "a position that widens the border fails the layer tests",
        [(
            "src/managr/overlay.py",
            "        fill = max(\n"
            "            1, inner - 3 - table.visible_width(shown) - badge_width - spacer\n"
            "        )",
            "        fill = max(1, inner - 3 - table.visible_width(shown))",
        )],
        "test_layers.py",
        "the position's width budget",
    )


def check_arrow_keys_guard_is_real():
    """Let the arrows run an action and confirm the tests catch it.

    The arrows scroll because `j` and `k` are actions: an arrow that ran an
    action would fire a verb the user never asked for.
    """
    check_revert(
        "an arrow key that runs an action fails the layer tests",
        [(
            "src/managr/overlay.py",
            "        if key.name == \"up\":\n"
            "            self._scroll(-1)\n"
            "            return True\n"
            "        if key.name == \"down\":\n"
            "            self._scroll(1)\n"
            "            return True\n",
            "        if key.name in (\"up\", \"down\"):\n"
            "            self.handle(Key(\"char\", \"j\"))\n"
            "            return True\n",
        )],
        "test_layers.py",
        "the arrow keys",
    )


def check_filter_guard_is_real():
    """Make the filter do nothing and confirm the tests catch it."""
    check_revert(
        "a filter that hides nothing fails the layer tests",
        [(
            "src/managr/overlay.py",
            FILTER_NARROWS,
            "        return list(layer.items)",
        )],
        "test_layers.py",
        "the filter",
    )


def check_filter_owns_the_keys_guard_is_real():
    """Let the layer's keys through while the filter is open."""
    check_revert(
        "a filter that does not own the keys fails the layer tests",
        [(
            "src/managr/overlay.py",
            "        if self.filtering:\n"
            "            # While the filter is open it owns the keys: a letter is part of\n"
            "            # the query, not a command.\n"
            "            self._handle_filter(key)\n"
            "            return\n",
            "",
        )],
        "test_layers.py",
        "the filter's key priority",
    )


def check_group_heading_guard_is_real():
    """Keep the headings whose group has nothing in it."""
    check_revert(
        "a heading with no rows under it fails the layer tests",
        [(
            "src/managr/overlay.py",
            "            matched = [item for item in group if self._matches(item, query)]\n"
            "            if not matched:\n"
            "                continue",
            "            matched = [item for item in group if self._matches(item, query)]",
        )],
        "test_layers.py",
        "the group headings",
    )


def check_group_heading_once_guard_is_real():
    """Repeat the heading for every row of its group.

    The heading heads the group: it is shown once, not once per row under
    it. A heading repeated for every row is a screen full of the same word.
    """
    check_revert(
        "a heading shown once per row fails the layer tests",
        [(
            "src/managr/overlay.py",
            "            if heading is not None:\n"
            "                kept.append(heading)\n"
            "            kept.extend(matched)",
            "            if heading is not None:\n"
            "                kept.extend([heading] * len(matched))\n"
            "            kept.extend(matched)",
        )],
        "test_layers.py",
        "the heading shown once",
    )


def check_input_badge_guard_is_real():
    """Leave the position of the previous layer in the frame.

    While a value is being typed there is no list on the screen, so the
    badge of the layer we came from is a lie.
    """
    check_revert(
        "a stale position in the input state fails the layer tests",
        [(
            "src/managr/overlay.py",
            "            self._view_total = 0\n"
            "            self._view_budget = 0\n"
            "            self._view_first = 0\n"
            "            return self._input_body(layer, inner)",
            "            return self._input_body(layer, inner)",
        )],
        "test_layers.py",
        "the position in the input state",
    )


def check_filter_scroll_guard_is_real():
    """Swallow the scroll keys while the filter is open.

    A filter that matches thirty rows is still thirty rows: the user has
    to be able to reach the last one without accepting the filter first.
    """
    check_revert(
        "a filter that swallows the scroll keys fails the layer tests",
        [(
            "src/managr/overlay.py",
            "        # The scroll keys, before the query: they move the window over the\n"
            "        # rows the query matches.\n"
            "        if self._scroll_by_key(key):\n"
            "            return\n",
            "",
        )],
        "test_layers.py",
        "the scroll keys while filtering",
    )


def check_escape_clears_the_filter_guard_is_real():
    """Leave the filter in force when Escape is pressed.

    A filter that is in force is the last thing the user asked for, so
    Escape undoes it before it undoes anything else.
    """
    check_revert(
        "an escape that keeps the filter fails the layer tests",
        [(
            "src/managr/overlay.py",
            "            if self.filter_query:\n"
            "                self._clear_filter()\n"
            "                return\n",
            "",
        )],
        "test_layers.py",
        "escape clearing the filter",
    )


def check_footer_hint_guard_is_real():
    """Say "esc close" while Escape clears the filter.

    The footer is the only place the user can read what the keys do, so a
    hint that says the wrong thing is a hint that lies.
    """
    check_revert(
        "a footer that lies about escape fails the layer tests",
        [(
            "src/managr/overlay.py",
            "        if self.filtering:\n"
            "            # The query is being typed: Escape throws it away, Enter keeps\n"
            "            # it.\n"
            "            return \"esc clear · enter keep · %s reference\" % reference_key\n"
            "        if self.filter_query:\n"
            "            # A filter is in force: Escape is the key that clears it.\n"
            "            return \"esc clear filter · ⌫ up · %s reference\" % reference_key\n",
            "",
        )],
        "test_layers.py",
        "the footer's hint about escape",
    )


def check_filter_reset_guard_is_real():
    """Keep the filter when another layer is entered."""
    check_revert(
        "a filter that follows the user into the next layer fails the tests",
        [(
            "src/managr/overlay.py",
            "        self.scroll = 0\n"
            "        self.filtering = False\n"
            "        self.filter_draft = \"\"\n"
            "        self.filter_query = \"\"\n",
            "        self.scroll = 0\n",
        )],
        "test_layers.py",
        "the filter reset",
    )


def check_filter_key_guard_is_real():
    """Take the filter key from a layer that binds it."""
    check_revert(
        "a layer that loses its own key fails the layer tests",
        [(
            "src/managr/overlay.py",
            "        item = lookup.get(pressed)\n"
            "        if item is None:\n"
            "            if pressed == FILTER_KEY:",
            "        if pressed == FILTER_KEY:\n"
            "            self._start_filter()\n"
            "            return\n"
            "        item = lookup.get(pressed)\n"
            "        if item is None:\n"
            "            if pressed == FILTER_KEY:",
        )],
        "test_layers.py",
        "the filter key",
    )


def check_scroll_pty_guard_is_real():
    """Take the scroll away and confirm the pty test catches it."""
    check_revert(
        "an overlay that does not scroll fails the pty tests",
        [(
            "src/managr/overlay.py",
            SCROLL_KEYS,
            "",
        )],
        "test_overlay_pty.py",
        "the scroll keys, in a pty",
    )


def check_filter_pty_guard_is_real():
    """Make the filter do nothing and confirm the pty test catches it."""
    check_revert(
        "a filter that hides nothing fails the pty tests",
        [(
            "src/managr/overlay.py",
            FILTER_NARROWS,
            "        return list(layer.items)",
        )],
        "test_overlay_pty.py",
        "the filter, in a pty",
    )


def check_accent_chain_guard_is_real():
    """Stop the accent chain at the first unreadable candidate.

    The candidates are a chain: one that cannot be read is refused, but the
    chain goes on, because a readable candidate later in the list is better
    than the built-in accent.
    """
    check_revert(
        "an unreadable accent that ends the chain fails the visuals tests",
        [(
            "src/managr/visuals.py",
            "        # Keep looking. The candidates are a chain, and one that cannot be\n"
            "        # read is refused -- but a later one may be perfectly readable, and\n"
            "        # the user should get the best accent available rather than the\n"
            "        # built-in one.\n"
            "        continue\n",
            "        break\n",
        )],
        "test_visuals.py",
        "the accent chain",
    )


def check_bundled_defaults_guard_is_real():
    """Hardcode the defaults again, instead of reading the bundled file.

    The bundled file has to differ from the hardcoded copy for this to mean
    anything, so the file is changed in the copy as well: a setting whose
    value lives in the file has to come from the file.
    """
    check_revert(
        "hardcoded defaults fail the visuals tests",
        [(
            "src/managr/visuals.py",
            "    data = _read_toml(bundled_path(SETTINGS_FILE))\n"
            "    defaults = {}\n"
            "    for key in KNOWN_SETTINGS:\n"
            "        value = data.get(key)\n"
            "        if isinstance(value, (str, bool, int, float)):\n"
            "            defaults[key] = value\n",
            "    data = {}\n"
            "    defaults = {\"accent\": \"\", \"icons\": True, \"frame\": True, \"theme\": \"\"}\n",
        ), (
            "src/managr/data/visuals.toml",
            "icons = true",
            "icons = false",
        )],
        "test_visuals.py",
        "the bundled defaults",
    )


def check_glyph_measurement_guard_is_real():
    """Make the module under-count a wide character.

    The visuals test measures the width of the glyph and of the row itself,
    so a glyph that really takes two columns is caught even when the
    module's own ruler says one. A test that asked the module under test
    how wide its own output is would have called this one column and
    passed.
    """
    check_revert(
        "a two-column glyph the module under-counts fails the visuals tests",
        [(
            "src/managr/table.py",
            "    if unicodedata.east_asian_width(char) in (\"W\", \"F\"):\n"
            "        # East Asian wide and fullwidth forms take two columns.\n"
            "        return 2\n",
            "    if unicodedata.east_asian_width(char) in (\"W\", \"F\"):\n"
            "        return 1\n",
        ), (
            "src/managr/data/icons.toml",
            "panes = \"\ueb7f\"",
            "panes = \"\u65e5\"",
        )],
        "test_visuals.py",
        "the measured glyph width",
    )


def check_unknown_setting_guard_is_real():
    """Ignore a setting the plugin does not know, without a word."""
    check_revert(
        "a silently ignored setting fails the visuals tests",
        [(
            "src/managr/merge.py",
            "        if key not in known:\n"
            "            unknown_setting(plugin, path, key, seen)\n"
            "            continue\n",
            "        if key not in known:\n"
            "            continue\n",
        )],
        "test_visuals.py",
        "the unknown setting warning",
    )


# -- the navigatr -----------------------------------------------------------


def check_navigatr_tree_guard_is_real():
    """Build an empty tree and confirm the tests catch it.

    The navigatr's tree is the feature: an empty one shows nothing, and
    the user cannot go anywhere.
    """
    check_revert(
        "an empty navigatr tree fails the layer tests",
        [(
            "src/managr/navigatr.py",
            "    return rows\n",
            "    return []\n",
        )],
        "test_layers.py",
        "the navigatr's tree",
    )


def check_navigatr_cursor_guard_is_real():
    """Let the cursor point past the end of the list.

    A cursor past the end points at nothing: the user presses Enter and
    the overlay jumps nowhere.
    """
    check_revert(
        "a cursor that leaves the list fails the layer tests",
        [(
            "src/managr/overlay.py",
            "            self.navigatr.cursor = self._selectable_row(\n"
            "                visible, max(0, min(self.navigatr.cursor, len(visible) - 1))\n"
            "            )\n",
            "            self.navigatr.cursor = self.navigatr.cursor\n",
        )],
        "test_layers.py",
        "the cursor's clamp",
    )


def check_navigatr_jump_guard_is_real():
    """Jump to the agent by name instead of by its pane.

    `agent.focus` takes the agent's pane: the parameter name was read off
    the live server, where a call with the wrong name is rejected with
    `missing field`.
    """
    check_revert(
        "a jump with the wrong parameter fails the layer tests",
        [(
            "src/managr/navigatr.py",
            '            params={"target": pane_id},\n',
            '            params={"target": str(agent.get("agent") or pane_id)},\n',
        )],
        "test_layers.py",
        "the jump's parameter",
    )


def check_navigatr_key_guard_is_real():
    """Stop opening the navigatr from the root layer."""
    check_revert(
        "a root that cannot open the navigatr fails the layer tests",
        [(
            "src/managr/overlay.py",
            "        if item.action == tree_mod.NAVIGATR_ACTION:\n"
            "            # The navigatr. It is listed in the root, so the key is\n"
            "            # discoverable, and a layer that binds the key itself keeps it\n"
            "            # -- exactly like the filter key: the key is only taken when\n"
            "            # nothing else wants it.\n"
            "            self._open_navigatr()\n"
            "            return\n",
            "",
        )],
        "test_layers.py",
        "the navigatr's key",
    )


def check_navigatr_row_guard_is_real():
    """Put the tree markers back in a column of their own.

    The markers were a narrow column on the left and the content was a
    flat wall of labels on the right: a tree that converged instead of
    opening outward. The marker and the content it marks have to move
    together.
    """
    check_revert(
        "a navigatr row with the markers in a column of their own fails the layer tests",
        [(
            "src/managr/overlay.py",
            "            self._tree_cell(row),\n",
            "            self._tree_cell(row).ljust(8),\n",
        )],
        "test_layers.py",
        "the navigatr's composed row",
    )


def check_navigatr_status_guard_is_real():
    """Send the status away from its label.

    The status belongs to its row, so it belongs next to the name: out at
    the right edge of the frame it is forty columns away from the thing it
    describes.
    """
    check_revert(
        "a status that is not next to its label fails the layer tests",
        [(
            "src/managr/overlay.py",
            '            if glyph:\n'
            '                status = "  %s" % glyph\n'
            "                status_colour = self.visuals.status_colour(category)\n",
            '            if glyph:\n'
            '                status = "%s  %s" % (" " * 20, glyph)\n'
            "                status_colour = self.visuals.status_colour(category)\n",
        )],
        "test_layers.py",
        "the navigatr's status",
    )


def check_navigatr_kind_guard_is_real():
    """Leave the kind of a row to the font's glyph alone.

    With `icons = false` the glyphs are gone, and the kind of a row -- a
    space, a tab, a pane, an agent -- has to stay readable: the tree's own
    shape does not say it.
    """
    check_revert(
        "a row with no kind without the icons fails the layer tests",
        [(
            "src/managr/overlay.py",
            "        kind = self.visuals.group_icon(row.kind) or self.visuals.kind_label(row.kind)\n",
            "        kind = self.visuals.group_icon(row.kind)\n",
        )],
        "test_layers.py",
        "the navigatr's kind",
    )


def check_navigatr_listed_guard_is_real():
    """Take the navigatr out of the root.

    It used to be a hidden key: a constant in the overlay that the root
    did not show. A key that does something and is nowhere on the screen is
    a key the user cannot find.
    """
    check_revert(
        "a root that does not list the navigatr fails the layer tests",
        [(
            "src/managr/tree.py",
            "        else:\n"
            "            root.items.append(\n"
            "                Item(\n"
            "                    key=navigatr_key,\n"
            "                    desc=NAVIGATR_TITLE,\n"
            "                    action=NAVIGATR_ACTION,\n"
            '                    layer="root",\n'
            "                )\n"
            "            )\n",
            "        else:\n"
            "            pass\n",
        )],
        "test_layers.py",
        "the navigatr in the root",
    )


def check_navigatr_shadowing_guard_is_real():
    """Let the navigatr take a key that a layer already has.

    The layer's own binding wins, exactly like the filter's key, and the
    root does not list an entry that would promise the key.
    """
    check_revert(
        "a navigatr that takes a layer's key fails the layer tests",
        [(
            "src/managr/tree.py",
            "        elif navigatr_key in taken or navigatr_key == reference_key:\n",
            "        elif False:\n",
        )],
        "test_layers.py",
        "the navigatr's shadowing rule",
    )


def check_navigatr_filter_guard_is_real():
    """Make the filter hide nothing in the navigatr.

    The filter matches the whole row: the space name, the tab name, the
    pane name and the agent name. A filter that only matched one field
    would hide most of the tree.
    """
    check_revert(
        "a filter that hides nothing in the navigatr fails the layer tests",
        [(
            "src/managr/overlay.py",
            "        rows = self._folded_rows(self.navigatr.rows)\n"
            "        keep = [query in row.search.lower() for row in rows]\n",
            "        rows = list(self._folded_rows(self.navigatr.rows))\n"
            "        keep = [True for row in rows]\n",
        )],
        "test_layers.py",
        "the navigatr's filter",
    )


def check_navigatr_failure_guard_is_real():
    """Let a failed list call out of the navigatr.

    The socket can be gone and the server busy. The overlay has to say so
    and stay usable -- the user still has to be able to press Escape.
    """
    check_revert(
        "a failed list call that crashes the overlay fails the layer tests",
        [(
            "src/managr/navigatr.py",
            "    except HerdrUnavailable as exc:\n"
            "        return Navigatr([], error=\"herdr unreachable: %s\" % exc, kind=kind)\n",
            "",
        )],
        "test_layers.py",
        "the navigatr's failure handling",
    )


def check_navigatr_every_render_guard_is_real():
    """Fetch the session on every render.

    A render happens on every keystroke, so this would be a socket round
    trip per keystroke: the overlay would feel like it is lagging.
    """
    check_revert(
        "a session fetched on every render fails the layer tests",
        [(
            "src/managr/overlay.py",
            "    def render(self):\n"
            "        rows, inner, frame = self._layout()\n",
            "    def render(self):\n"
            "        if self.navigatr is not None:\n"
            "            self.navigatr = navigatr_mod.fetch(self.context.client)\n"
            "        rows, inner, frame = self._layout()\n",
        )],
        "test_layers.py",
        "the navigatr's fetch",
    )


def check_navigatr_band_guard_is_real():
    """Take the band off the row under the cursor.

    The band is the accent, and it is the clearest statement of "this is
    the one you are on" a terminal can make. A row without it is a row
    the user cannot tell apart from the others.
    """
    check_revert(
        "a cursor row with no band fails the layer tests",
        [(
            "src/managr/overlay.py",
            "        return self._band(\n"
            "            table.truncate(line, width, close=not on_band), width, on_band\n"
            "        )\n",
            "        return table.truncate(line, width)\n",
        )],
        "test_layers.py",
        "the band under the cursor row",
    )


def check_navigatr_last_child_guard_is_real():
    """Decide the last child of a space over the tabs alone.

    The loose panes are children of the space too, so the tabs and the
    loose panes have to be counted together: otherwise two rows claim to
    be the end of the branch.
    """
    check_revert(
        "a space with two last children fails the layer tests",
        [(
            "src/managr/navigatr.py",
            "        children = len(space_tabs) + len(loose)\n",
            "        children = len(space_tabs)\n",
        )],
        "test_layers.py",
        "the last child of a space",
    )


def check_navigatr_current_marker_guard_is_real():
    """Leave the current row unmarked when the icons are off.

    The marker says which row is the current one. It is information, not
    decoration, so it has to survive `icons = false`.
    """
    check_revert(
        "a current row that is not marked fails the layer tests",
        [(
            "src/managr/overlay.py",
            "            current = self._paint(\n"
            '                self.visuals.marker("arrow") or DEFAULT_ARROW,\n'
            "                self._row_colour(self.palette.accent, on_band),\n"
            "                close,\n"
            "            )\n"
            "        status = \"\"\n",
            "            current = self._paint(\n"
            '                self.visuals.marker("arrow"),\n'
            "                self._row_colour(self.palette.accent, on_band),\n"
            "                close,\n"
            "            )\n"
            "        status = \"\"\n",
        )],
        "test_layers.py",
        "the current row's marker",
    )


def check_navigatr_band_background_guard_is_real():
    """Close a colour inside the band row, and the band ends there.

    The band is a *background*, and the reset that closes a colour closes
    the background too. So the parts that are painted inside the row --
    the kind glyph of the tree -- must not close their own colour there:
    the band ended after the first two columns, and the rest of the row
    was drawn in the panel's background colour on the terminal's own
    background, so the row under the cursor was invisible.
    """
    check_revert(
        "a band that ends inside the cursor row fails the layer tests",
        [(
            "src/managr/overlay.py",
            "        kind = self._paint(\n"
            "            kind,\n"
            "            self._row_colour(self.visuals.kind_colour(row.kind), on_band),\n"
            "            close,\n"
            "        )\n",
            "        kind = self._paint(\n"
            "            kind,\n"
            "            self._row_colour(self.visuals.kind_colour(row.kind), on_band),\n"
            "        )\n",
        )],
        "test_layers.py",
        "the band under the cursor row",
    )


def check_agents_band_background_guard_is_real():
    """Close the colour of the current agent's marker, and the band ends.

    The agents list is flat and has no kind glyph, so the marker of the
    focused agent is the one part that is painted inside the row. It
    closed its own colour, the band ended after it, and the agent under
    the cursor was drawn off the band and could not be read.
    """
    check_revert(
        "a band that ends inside the cursor row of the agents list fails the layer tests",
        [(
            "src/managr/overlay.py",
            "            current = self._paint(\n"
            '                self.visuals.marker("arrow") or DEFAULT_ARROW,\n'
            "                self._row_colour(self.palette.accent, on_band),\n"
            "                close,\n"
            "            )\n"
            "        category = navigatr_mod.status_category(row.status)\n",
            "            current = self._paint(\n"
            '                self.visuals.marker("arrow") or DEFAULT_ARROW,\n'
            "                self._row_colour(self.palette.accent, on_band),\n"
            "            )\n"
            "        category = navigatr_mod.status_category(row.status)\n",
        )],
        "test_layers.py",
        "the band under the cursor row of the agents list",
    )


def check_navigatr_cut_band_guard_is_real():
    """Close the colour run when a row is cut, and the band ends there.

    A row that does not fit is cut, and the cut closes the colour run of
    the text -- which closes the band with it. The band is closed once, at
    the end of the row, so the cut must not close it.
    """
    check_revert(
        "a band that ends at the cut of a row fails the layer tests",
        [(
            "src/managr/overlay.py",
            "        return self._band(\n"
            "            table.truncate(line, width, close=not on_band), width, on_band\n"
            "        )\n",
            "        return self._band(table.truncate(line, width), width, on_band)\n",
        )],
        "test_layers.py",
        "the band under the cursor row",
    )


def check_session_data_colour_guard_is_real():
    """Let the session's own data colour the overlay.

    A pane's title is set by whatever program runs in the pane, and
    Herdr reports it as it is. The label is stripped of its escape
    sequences, and so are the description of a row, the path in the
    detail region and the directory: an escape sequence in any of them
    can move the cursor, clear the screen, or close the colour run of
    the band under the row.
    """
    check_revert(
        "a colour in the session's own data fails the layer tests",
        [(
            "src/managr/overlay.py",
            '        desc = "  %s" % self._plain_text(row.desc) if row.desc else ""\n'
            "\n"
            "        # The status and the description are *dependent* elements:",
            '        desc = "  %s" % row.desc if row.desc else ""\n'
            "\n"
            "        # The status and the description are *dependent* elements:",
        )],
        "test_layers.py",
        "a colour in the description",
    )


def check_session_data_path_guard_is_real():
    """Let the path in the detail region colour the overlay.

    The path is made of the names of the space, the tab and the pane,
    and the directory is the pane's: the session's own data, and it is
    written to the terminal like the label is.
    """
    check_revert(
        "a colour in the path of the detail region fails the layer tests",
        [(
            "src/managr/overlay.py",
            "                    truncate(self._plain_text(row.path), inner),",
            "                    truncate(row.path, inner),",
        ), (
            "src/managr/overlay.py",
            "                    truncate(self._plain_text(row.cwd), inner),",
            "                    truncate(row.cwd, inner),",
        )],
        "test_layers.py",
        "a colour in the path or the directory",
    )


def check_cursor_hidden_guard_is_real():
    """Leave the terminal's cursor under the frame.

    Every line the overlay writes ends with a newline, so the cursor is
    parked on the row *after* the frame: outside the panel, where it
    blinks as a block under the window and reads as a piece of the
    overlay that was left behind.
    """
    check_revert(
        "a cursor parked under the frame fails the layer tests",
        [(
            "src/managr/overlay.py",
            "        self.out.write(SHOW_CURSOR if typing else HIDE_CURSOR)",
            "        pass",
        )],
        "test_layers.py",
        "the cursor under the frame",
    )


def check_agents_number_slot_guard_is_real():
    """Give the number of the agents list no slot of its own.

    The status is a column -- that is what makes it scannable -- and a
    row with no number in it must not shift the rest of the row. The
    tenth agent and the ones after it have no number to press, and the
    column moved two columns to the left after the ninth one.
    """
    check_revert(
        "a status column that moves after the ninth agent fails the layer tests",
        [(
            "src/managr/overlay.py",
            '        number_slot = self._paint(\n'
            '            "%s " % number if number else "  ",\n'
            "            self._row_colour(self.palette.chord, on_band),\n"
            "            close,\n"
            "        )\n",
            '        number_slot = self._paint(\n'
            '            "%s " % number if number else "",\n'
            "            self._row_colour(self.palette.chord, on_band),\n"
            "            close,\n"
            "        )\n",
        )],
        "test_layers.py",
        "the number of the agents list",
    )


def check_cursor_placed_guard_is_real():
    """Show the cursor without putting it where the user is typing.

    Every line the overlay writes ends with a newline, so a cursor that
    is only *shown* blinks under the frame -- outside the panel, where
    it reads as a piece of the overlay that was left behind. It has to
    be put on the line the user is typing on.
    """
    check_revert(
        "a cursor that is shown but not placed fails the layer tests",
        [(
            "src/managr/overlay.py",
            "        if typing:\n"
            "            # The cursor belongs on the line the user is typing on, not\n"
            "            # under the frame.\n"
            "            self.out.write(self._cursor_home(inner, layer, frame))\n",
            "        if False:\n"
            "            self.out.write(self._cursor_home(inner, layer, frame))\n",
        )],
        "test_layers.py",
        "the cursor on the typing line",
    )


def check_agents_data_colour_guard_is_real():
    """Let the agents list colour itself with the session's own data.

    The agents list has its own composer, with its own label and its
    own description. The escape sequences have to be stripped out of
    those too: a program running in a pane can put them in the name of
    an agent.
    """
    check_revert(
        "a colour in the agents list fails the layer tests",
        [(
            "src/managr/overlay.py",
            "        label = self._plain_text(row.label)\n"
            "        # The description is where the agent is:",
            "        label = str(row.label)\n"
            "        # The description is where the agent is:",
        )],
        "test_layers.py",
        "a colour in the agents list",
    )


def check_tab_heading_guard_is_real():
    """Make the tab rows destinations again.

    A tab is a heading, not a place: it always has at least one pane,
    and focusing a tab lands on a pane anyway. So the cursor skips it,
    and a pane to a pane is one key instead of two.
    """
    check_revert(
        "a cursor that stops on a tab row fails the layer tests",
        [(
            "src/managr/navigatr.py",
            "                # A tab is a group label, not a place: the cursor skips it\n"
            "                # and `enter` never goes to one. The panes under it are\n"
            "                # the places.\n"
            "                selectable=False,\n",
            "",
        )],
        "test_layers.py",
        "the cursor never stops on a tab",
    )


def check_navigatr_tier_colour_guard_is_real():
    """Draw the three tiers of the tree in the same colour.

    The space is the section title and is in the accent, the tab is the
    group heading and is muted, and the pane is the item the user jumps
    to and is in the plain colour. Without that the three of them are a
    wall of labels in the same colour, and the eye has to work out the
    structure of the tree row by row.
    """
    check_revert(
        "a tree whose tiers are all in the same colour fails the layer tests",
        [(
            "src/managr/overlay.py",
            "        if row.kind == navigatr_mod.WORKSPACE:\n"
            "            # A space is a section: the loudest thing on its own branch.\n"
            "            return self.palette.accent + self.palette.bold\n",
            "",
        )],
        "test_layers.py",
        "the three tiers of the tree",
    )


def check_navigatr_filter_structure_guard_is_real():
    """Let the filter drop the headings.

    A space or a tab row is a heading, and it is shown when one of the
    rows under it survived the filter: a tree without its headings is a
    list, and the list is what the agents navigatr is for.
    """
    check_revert(
        "a filter that drops the headings fails the layer tests",
        [(
            "src/managr/overlay.py",
            "            for index in range(len(rows) - 1, -1, -1):\n"
            "                parent = self._navigatr_parent(rows, index)\n"
            "                if parent >= 0 and keep[index]:\n"
            "                    keep[parent] = True\n",
            "            pass\n",
        )],
        "test_layers.py",
        "the filter keeps the structure",
    )


def check_navigatr_number_slot_guard_is_real():
    """Leave the number of a space undrawn.

    The number is in a column of its own, in front of the tree's
    markers. It used to sit at the front of the label, and it pushed
    the name of a space two columns to the right of the name of the tab
    under it: the tree's names no longer marched with its depth.
    """
    check_revert(
        "a space without a number in it fails the layer tests",
        [(
            "src/managr/overlay.py",
            '        number_slot = self._paint(\n'
            '            "%s " % number if number else "  ",\n'
            "            self._row_colour(self.palette.chord, on_band),\n"
            "            close,\n"
            "        )\n",
            '        number_slot = self._paint(\n'
            '            "  ",\n'
            "            self._row_colour(self.palette.chord, on_band),\n"
            "            close,\n"
            "        )\n",
        )],
        "test_layers.py",
        "the number of a space",
    )


def check_navigatr_escape_guard_is_real():
    """Leave the navigatr even when a filter is in force.

    A filter in force is the last thing the user asked for, so Escape
    clears it and stays, exactly as it does in a layer.
    """
    check_revert(
        "an escape that ignores the filter fails the layer tests",
        [(
            "src/managr/overlay.py",
            '        if key.name == "esc":\n'
            "            if self.filter_query:\n"
            "                self._clear_filter()\n"
            "                return\n"
            "            if self._rearranging:\n"
            "                self._stop_rearranging()\n"
            "                return\n"
            "            self._close_navigatr()\n"
            "            return\n",
            '        if key.name == "esc":\n'
            "            self._close_navigatr()\n"
            "            return\n",
        )],
        "test_layers.py",
        "escape in the navigatr",
    )


def check_navigatr_orphan_guard_is_real():
    """Drop a tab whose space is gone.

    The four list calls are four round trips, and the space can be closed
    between two of them. The tab is still a place the user can go.
    """
    check_revert(
        "a tab whose space is gone and dropped fails the layer tests",
        [(
            "src/managr/navigatr.py",
            "    orphan_tabs = [\n"
            '        tab for tab in tabs if _id(tab, "workspace_id") not in listed_space_ids\n'
            "    ]\n",
            "    orphan_tabs = []\n",
        )],
        "test_layers.py",
        "a tab whose space is gone",
    )


def check_navigatr_key_length_guard_is_real():
    """List a navigatr entry whose key cannot be pressed.

    The overlay's keyspace is single characters, so a key of two
    characters is an entry that promises a key no keypress could ever
    reach -- the same lie as an entry that promises a key a layer
    already took.
    """
    check_revert(
        "a navigatr key that cannot be pressed fails the layer tests",
        [(
            "src/managr/tree.py",
            "        if _unpressable(navigatr_key):\n",
            "        if False:\n",
        )],
        "test_layers.py",
        "the navigatr key's length",
    )


def check_reference_key_length_guard_is_real():
    """List a reference entry whose key cannot be pressed."""
    check_revert(
        "a reference key that cannot be pressed fails the layer tests",
        [(
            "src/managr/tree.py",
            "        if _unpressable(reference_key):\n",
            "        if False:\n",
        )],
        "test_layers.py",
        "the reference key's length",
    )


def check_navigatr_warning_once_guard_is_real():
    """Print the shadowing warning on every build of the tree.

    The tree is built once per popup, so the user would see it every time
    they opened the overlay.
    """
    check_revert(
        "a warning printed on every build fails the layer tests",
        [(
            "src/managr/tree.py",
            "    merge_mod.warn_once(PLUGIN, message)\n",
            '    sys.stderr.write("%s: %s\\n" % (PLUGIN, message))\n',
        )],
        "test_layers.py",
        "the warning that is printed once",
    )


def check_navigatr_status_drop_guard_is_real():
    """Keep the status on a row that has no room for it.

    The status is a dependent element: it is dropped before the label is
    cut, so the row ends cleanly instead of in a bare ellipsis.
    """
    check_revert(
        "a status that is cut off instead of dropped fails the layer tests",
        [(
            "src/managr/overlay.py",
            "        if status:\n"
            "            # The status is drawn at the right-hand end of the row, so the\n"
            "            # glyphs of a whole list line up: that is what makes them a\n"
            "            # column the eye can scan instead of a colour it has to find\n"
            "            # in every line.\n"
            "            parts.append(\n"
            "                (status, 1, False, self._row_colour(status_colour, on_band), True)\n"
            "            )\n",
            "        if status:\n"
            "            pass\n",
        ), (
            "src/managr/overlay.py",
            "            self._draw_parts(kept, used, width, close),\n"
            "        )\n",
            "            self._draw_parts(kept, used, width, close),\n"
            "            self._paint(status, status_colour),\n"
            "        )\n",
        )],
        "test_layers.py",
        "the dropping of the status",
    )


def check_navigatr_numbered_spaces_guard_is_real():
    """The spaces are numbered, and a number jumps to one.

    The number is in a column of its own, in front of the tree's
    markers: it used to sit at the front of the LABEL, where it pushed
    the name of a space two columns to the right of the name of the tab
    under it, and the tree's names stopped marching with its depth.
    """
    check_revert(
        "the spaces are not numbered fails the layer tests",
        [(
            "src/managr/overlay.py",
            '        number_slot = self._paint(\n'
            '            "%s " % number if number else "  ",\n',
            '        number_slot = self._paint(\n'
            '            "  ",\n',
        )],
        "test_layers.py",
        "the numbering of the spaces",
    )
    check_revert(
        "a number does not move the cursor fails the layer tests",
        [(
            "src/managr/overlay.py",
            '            if key.char in "123456789":\n',
            '            if False:\n',
        )],
        "test_layers.py",
        "the cursor a number moves",
    )


def check_navigatr_label_colour_guard_is_real():
    """Draw the colour a pane title asks for.

    The tree draws the session's own data now, and a program running in a
    pane could colour the overlay.
    """
    check_revert(
        "a colour in a pane title fails the layer tests",
        [(
            "src/managr/overlay.py",
            "        label = self._plain_text(row.label)\n",
            "        label = str(row.label)\n",
        )],
        "test_layers.py",
        "the colour runs in a label",
    )


# -- the two navigatrs ------------------------------------------------------
#
# The places tree and the agents list. The checks below break them on
# purpose: an agent that is a row of its own again, a list that is not
# sorted by attention, a row that says nothing about what is inside it, a
# key that cannot be pressed, and a "working" agent drawn as an unknown
# one.


def check_agent_fold_guard_is_real():
    """Bring the agent row back.

    The agent is folded into the pane that hosts it: `agent.focus` and
    `pane.focus` land on the same pane, so a fourth level of the tree says
    the same thing twice and takes the room the counts need.

    The revert puts the agent back as a row of its own, under the pane
    that hosts it, and takes it out of the pane's row. The tree is four
    levels deep again, and the pane's row says nothing about the agent in
    it -- so the layer tests that say the tree is spaces, tabs and panes
    have to fail.
    """
    check_revert(
        "an agent that is a row of its own again fails the layer tests",
        [(
            "src/managr/navigatr.py",
            '        desc=", ".join(names),\n',
            '        desc="",\n',
        ), (
            "src/managr/navigatr.py",
            "            under.append(tab_row)\n"
            "            under.extend(pane_rows)\n",
            "            under.append(tab_row)\n"
            "            under.extend(pane_rows)\n"
            "            for pane_row in pane_rows:\n"
            "                if pane_row.agent:\n"
            "                    under.append(\n"
            "                        Row(\n"
            "                            kind=AGENT,\n"
            "                            label=pane_row.agent,\n"
            "                            method=FOCUS_METHODS[AGENT],\n"
            '                            params={"target": pane_row.params.get("pane_id", "")},\n'
            "                            depth=3,\n"
            "                            status=pane_row.status,\n"
            "                        )\n"
            "                    )\n",
        )],
        "test_layers.py",
        "the agent folded into the pane's row",
    )


def check_navigatr_agent_description_guard_is_real():
    """Empty the pane row's description.

    The pane's row says which agent is in it: a row that says nothing
    about what is inside it is a row the user cannot read.
    """
    check_revert(
        "a pane row that says nothing about the agent in it fails the layer tests",
        [(
            "src/managr/navigatr.py",
            '        desc=", ".join(names),\n',
            '        desc="",\n',
        )],
        "test_layers.py",
        "the agent in the pane's row",
    )


def check_navigatr_counts_guard_is_real():
    """Take the counts from the server instead of from the lists.

    Herdr reports a `tab_count` and a `pane_count` for a space, but they
    are a snapshot of a session that can change between two of the four
    list calls. The rows drawn and the counts have to agree, so the
    counts are computed from the lists -- and a stale count is a count
    the user cannot trust.
    """
    check_revert(
        "counts that come from the server's own fields fail the layer tests",
        [(
            "src/managr/navigatr.py",
            "            desc=_counts(\n"
            '                (len(space_tabs), "tab"),\n'
            '                (space_panes, "pane"),\n'
            '                (space_agents, "agent"),\n'
            "            ),\n",
            "            desc=_counts(\n"
            '                (int(space.get("tab_count") or 0), "tab"),\n'
            '                (int(space.get("pane_count") or 0), "pane"),\n'
            '                (space_agents, "agent"),\n'
            "            ),\n",
        )],
        "test_layers.py",
        "the counts computed from the lists",
    )


def check_navigatr_number_order_guard_is_real():
    """Cut the label before the dependent elements are given up.

    The number is in a column of its own now, so it is never given up.
    The dependent elements -- the status and the description -- are: they
    go first, and the label is cut last of all. A row that cuts its
    label while it still has a status is a row the user cannot read.
    """
    check_revert(
        "a row that cuts its label instead of giving up the parts fails the layer tests",
        [(
            "src/managr/overlay.py",
            "        while kept and measure() > width:\n"
            "            # The part with the highest priority is the one that goes.\n"
            "            worst = max(kept, key=lambda part: part[1])\n"
            "            kept.remove(worst)\n",
            "        # The dependent elements are never given up.\n",
        )],
        "test_layers.py",
        "the order the row gives things up in",
    )


def check_agents_location_order_guard_is_real():
    """Give up the location of an agent after its status word.

    The location -- the space and the tab the agent is in -- is a
    dependent element, and it is the first of the agent's row to go: the
    word is eight columns and the glyph is one, and the name of the agent
    is the row.
    """
    check_revert(
        "an agent row that keeps its location too long fails the layer tests",
        [(
            "src/managr/overlay.py",
            "            parts.append(\n"
            "                (\n"
            '                    "%s " % category.ljust(STATUS_WORD_WIDTH),\n'
            "                    1,\n",
            "            parts.append(\n"
            "                (\n"
            '                    "%s " % category.ljust(STATUS_WORD_WIDTH),\n'
            "                    2,\n",
        ), (
            "src/managr/overlay.py",
            "            parts.append(\n"
            "                (\n"
            "                    desc,\n"
            "                    2,\n",
            "            parts.append(\n"
            "                (\n"
            "                    desc,\n"
            "                    0,\n",
        )],
        "test_layers.py",
        "the order the agents row gives things up in",
    )


def check_agents_flat_drawing_guard_is_real():
    """Give the agents list a kind glyph and a branch marker.

    The list is flat: every row is an agent, so saying so on every row is
    noise, and a tree's branch markers on a list that is not a tree say
    that there is a tree.
    """
    check_revert(
        "an agents row with a kind glyph and a branch marker fails the layer tests",
        [(
            "src/managr/overlay.py",
            '        prefix = "%s%s %s %s" % (ROW_INDENT, cursor, current, number_slot)\n',
            '        prefix = "%s%s %s %s %s " % (\n'
            "            ROW_INDENT,\n"
            "            cursor,\n"
            "            current,\n"
            "            number_slot,\n"
            "            self._tree_cell(row),\n"
            "            self.visuals.group_icon(row.kind),\n"
            "        )\n",
        )],
        "test_layers.py",
        "the flat drawing of the agents list",
    )


def check_navigatr_pane_once_guard_is_real():
    """Draw the pane that names another space's tab twice.

    A pane belongs to the tab it names, wherever that tab lives. Drawing
    it again as a loose pane of the space it claims puts the same
    destination in the tree twice and counts it in two spaces.
    """
    check_revert(
        "a pane that is drawn twice fails the layer tests",
        [(
            "src/managr/navigatr.py",
            '            if _id(pane, "workspace_id") == space_id\n'
            '            and _id(pane, "tab_id") not in every_tab_id\n',
            '            if _id(pane, "workspace_id") == space_id\n',
        )],
        "test_layers.py",
        "a pane drawn once",
    )


def check_navigatr_status_separator_guard_is_real():
    """Draw the separator with no glyph behind it.

    The separator between the name and the status only exists when there
    is a glyph: a status with no glyph of its own would leave two spaces
    at the end of the row, and they would be counted in the width budget.
    """
    check_revert(
        "a status with no glyph and a separator fails the layer tests",
        [(
            "src/managr/overlay.py",
            "            if glyph:\n"
            '                status = "  %s" % glyph\n'
            "                status_colour = self.visuals.status_colour(category)\n",
            '            status = "  %s" % glyph\n'
            "            status_colour = self.visuals.status_colour(category)\n",
        )],
        "test_layers.py",
        "the separator of a status with no glyph",
    )


def check_overlay_context_refresh_guard_is_real():
    """Stop re-reading the context after an action.

    The context is a snapshot of where the overlay was opened. An action
    that moves the focus leaves it stale, and "split right" after "focus
    right" split the pane that was focused when the overlay opened.
    """
    check_revert(
        "a context that is not re-read fails the layer tests",
        [(
            "src/managr/overlay.py",
            "        self.context.refresh()\n",
            "        pass\n",
        )],
        "test_layers.py",
        "the context re-read after every action",
    )


def check_overlay_leaves_context_guard_is_real():
    """Never close the overlay when the context moved.

    A popup is session-modal: it takes every keystroke wherever it was
    opened. So an overlay left open on the old workspace holds the
    keyboard there, and the plugin cannot be opened again on the new one.
    """
    check_revert(
        "an overlay that stays open on the old workspace fails the layer tests",
        [(
            "src/managr/overlay.py",
            "        return after != before\n",
            "        return False\n",
        )],
        "test_layers.py",
        "the overlay closing itself when the workspace or the tab moved",
    )


def check_status_colour_guard_is_real():
    """Draw every status in the same colour.

    The status is the thing the eye should go to: a blocked agent wants
    the user now, so it is in the error colour, and the quiet ones are
    muted so that they do not shout.
    """
    check_revert(
        "a status that is not coloured by severity fails the visuals tests",
        [(
            "src/managr/visuals.py",
            '            "blocked": self.palette.err,\n',
            "",
        )],
        "test_visuals.py",
        "the colour of a status",
    )


def check_kind_colour_guard_is_real():
    """Draw every kind marker in the same colour.

    The deeper the row, the brighter the marker: the leaves are the
    things you can jump to, so they stand out and the structure recedes.
    """
    check_revert(
        "a kind marker that is not coloured by depth fails the visuals tests",
        [(
            "src/managr/visuals.py",
            '            "pane": self.palette.accent,\n',
            "",
        )],
        "test_visuals.py",
        "the colour of a kind marker",
    )


def check_navigatr_description_guard_is_real():
    """Leave the counts out of the rows.

    A row that says nothing about what is inside it is a row the user
    cannot read: the counts are what turn a list of names into a map of
    the session.
    """
    check_revert(
        "rows that say nothing about what they hold fail the layer tests",
        [(
            "src/managr/navigatr.py",
            "            desc=_counts(\n"
            "                (len(space_tabs), \"tab\"),\n"
            "                (space_panes, \"pane\"),\n"
            "                (space_agents, \"agent\"),\n"
            "            ),\n",
            "            desc=\"\",\n",
        )],
        "test_layers.py",
        "the counts in the rows",
    )


def check_agents_sort_guard_is_real():
    """Leave the agents list in the order the list came in.

    The list is sorted by attention: a blocked agent wants the user now,
    so it is at the top.
    """
    check_revert(
        "an agents list that is not sorted by attention fails the layer tests",
        [(
            "src/managr/navigatr.py",
            "    ordered.sort(key=lambda pair: pair[0])\n",
            "",
        )],
        "test_layers.py",
        "the attention order of the agents list",
    )


def check_agents_flat_guard_is_real():
    """Give the agents list a tree.

    The list is flat: no indentation, no branch markers, and every row at
    the same depth.
    """
    check_revert(
        "an agents list that is not flat fails the layer tests",
        [(
            "src/managr/navigatr.py",
            "            params={\"target\": pane_id},\n"
            "            depth=0,\n",
            "            params={\"target\": pane_id},\n"
            "            depth=1,\n",
        )],
        "test_layers.py",
        "the flat agents list",
    )


def check_working_category_guard_is_real():
    """Fold "working" back into the unknown category.

    Herdr reports five agent states, and "working" is one of them: an
    agent that is busy is not an agent whose state is unknown.
    """
    check_revert(
        "a working agent drawn as an unknown one fails the layer tests",
        [(
            "src/managr/navigatr.py",
            '    ("working", ("working", "running", "busy")),\n',
            "",
        )],
        "test_layers.py",
        "the working category",
    )


def check_agents_listed_guard_is_real():
    """Take the agents list out of the root.

    A key that does something and is nowhere on the screen is a key the
    user cannot find.
    """
    check_revert(
        "a root that does not list the agents list fails the layer tests",
        [(
            "src/managr/tree.py",
            "            root.items.append(\n"
            "                Item(\n"
            "                    key=agents_key,\n"
            "                    desc=AGENTS_TITLE,\n"
            "                    action=AGENTS_ACTION,\n"
            '                    layer="root",\n'
            "                )\n"
            "            )\n",
            "            pass\n",
        )],
        "test_layers.py",
        "the agents list in the root",
    )


def check_agents_key_length_guard_is_real():
    """List an agents entry whose key cannot be pressed.

    The overlay's keyspace is single characters, so a key of two
    characters is an entry that promises a key no keypress could ever
    reach -- the same lie as an entry that promises a key a layer
    already took.
    """
    check_revert(
        "an agents key that cannot be pressed fails the layer tests",
        [(
            "src/managr/tree.py",
            "        if _unpressable(agents_key):\n",
            "        if False:\n",
        )],
        "test_layers.py",
        "the agents key's length",
    )


def check_agents_key_shadowing_guard_is_real():
    """Let the agents list take a key that is already taken.

    The layer's own binding wins, exactly like the filter's key, and the
    root does not list an entry that would promise the key.
    """
    check_revert(
        "an agents list that takes a key that is taken fails the layer tests",
        [(
            "src/managr/tree.py",
            "        elif agents_key in taken or agents_key == reference_key:\n",
            "        elif False:\n",
        )],
        "test_layers.py",
        "the agents list's shadowing rule",
    )


def check_agents_numbering_guard_is_real():
    """Stop numbering the agents.

    Every row of the agents list is an agent, so every row is numbered,
    and the numbers follow the filter: a number must always name a row
    the user can see.
    """
    check_revert(
        "an agents list that is not numbered fails the layer tests",
        [(
            "src/managr/overlay.py",
            "        agents = self._is_agents()\n",
            "        agents = False\n",
        )],
        "test_layers.py",
        "the numbering of the agents",
    )


def check_agents_open_guard_is_real():
    """Stop opening the agents list from the root.

    The agents list is a mode like the navigatr, and the root lists it,
    so the key that opens it is discoverable.
    """
    check_revert(
        "a root that cannot open the agents list fails the layer tests",
        [(
            "src/managr/overlay.py",
            "        if item.action == tree_mod.AGENTS_ACTION:\n"
            "            # The agents list. The same mode with a different list: the\n"
            "            # same keys, the same footer, and the agents of the session\n"
            "            # the ones that want attention first.\n"
            "            self._open_navigatr(navigatr_mod.AGENTS)\n"
            "            return\n",
            "",
        )],
        "test_layers.py",
        "the agents list's key",
    )


def check_wide_character_guard_is_real():
    """Under-count a wide character and confirm the layer tests catch it.

    A CJK title is one character and two columns, so a ruler that counts
    characters calls a row narrower than it is, and the row runs past the
    edge of the frame.
    """
    check_revert(
        "a wide character the module under-counts fails the layer tests",
        [(
            "src/managr/table.py",
            "    if unicodedata.east_asian_width(char) in (\"W\", \"F\"):\n"
            "        # East Asian wide and fullwidth forms take two columns.\n"
            "        return 2\n",
            "    if unicodedata.east_asian_width(char) in (\"W\", \"F\"):\n"
            "        return 1\n",
        )],
        "test_layers.py",
        "the column width of a wide character",
    )


def check_bar_budget_guard_is_real():
    """Let the bar exceed its budget again (the sibling plugin)."""
    check_revert(
        "a bar that exceeds its budget fails the keymap tests",
        [(
            "src/herdrbar/bar.py",
            "    if display_width(line) > max_width and prefix:\n"
            "        line = compose(ordered, with_prefix=False)\n"
            "    if display_width(line) > max_width:\n"
            "        line = _cut_to_width(line, max_width)\n",
            "",
        )],
        "test_keymap.py",
        "the bar's width budget",
        tree=copy_v1_tree,
    )


def check_bar_column_budget_guard_is_real():
    """Measure the bar in characters instead of columns (the sibling plugin).

    `len()` and the column width coincide only because the marker glyph
    happens to be one column wide. A two-column marker -- which a user can
    put in `bar.toml` -- makes the difference visible: the bar then runs
    over its budget in columns while it fits in characters, and the test
    that counts columns is the one that says so.
    """
    check_revert(
        "a bar measured in characters fails the keymap tests",
        [(
            "src/herdrbar/bar.py",
            "MARKER = \"\uf11c\"",
            "MARKER = \"\u65e5\"",
        ), (
            "src/herdrbar/bar.py",
            "    while display_width(line) > max_width and ordered:",
            "    while len(line) > max_width and ordered:",
        ), (
            "src/herdrbar/bar.py",
            "    if display_width(line) > max_width and prefix:\n"
            "        line = compose(ordered, with_prefix=False)\n"
            "    if display_width(line) > max_width:\n"
            "        line = _cut_to_width(line, max_width)\n",
            "",
        )],
        "test_keymap.py",
        "the column width budget",
        tree=copy_v1_tree,
    )


# -- the API completeness pass ----------------------------------------------
#
# The action map is data now, so a tool can ask what an action would do
# without doing it, and check it against the schema. The checks below break
# the map on purpose: a method Herdr does not have, a parameter the schema
# does not define, and a hint row that Herdr can now reach.


def check_api_method_guard_is_real():
    """Give an action a method that does not exist and confirm the tests catch it.

    Herdr does not forbid extra fields, so a wrong parameter name is
    accepted and ignored. A wrong *method* is refused -- but only if
    somebody checks, and until now nobody did.
    """
    check_revert(
        "an action with a method that does not exist fails the api tests",
        [(
            "src/managr/actions.py",
            'Call("pane.close", {"pane_id": "$pane_id"})',
            'Call("pane.closee", {"pane_id": "$pane_id"})',
        )],
        "test_api_check.py",
        "the action map's methods",
    )


def check_api_parameter_guard_is_real():
    """Give an action a parameter the schema does not define."""
    check_revert(
        "an action with a parameter the schema does not define fails the api tests",
        [(
            "src/managr/actions.py",
            'Call("pane.rename", {"pane_id": "$pane_id", "label": "$value"})',
            'Call("pane.rename", {"pane_id": "$pane_id", "name": "$value"})',
        )],
        "test_api_check.py",
        "the action map's parameters",
    )


def check_unreachable_guard_is_real():
    """Forget that a method Herdr has is not a way into copy mode.

    The hint rows are claims about Herdr's API, and a claim that is not
    measured is a claim that rots: when Herdr grows a method for one of
    them, the row has to stop being a hint row.
    """
    check_revert(
        "a hint row whose method Herdr has fails the api tests",
        [(
            "src/managr/actions.py",
            '            "pane.resize": "a one-shot resize, not the interactive mode",\n',
            "",
        )],
        "test_api_check.py",
        "the unreachable list",
    )



# -- the custom layer -------------------------------------------------------
#
# The custom layer lists the user's own commands and the actions of every
# installed plugin. The checks below break it on purpose: a layer that is
# not filled, a list fetched on every render, a shell command that does not
# run, a popup command that is not refused, a qualified plugin action id
# split at the wrong dot, a scroll that cannot reach the last row, an
# entry key pool that takes the filter's key, and a plugin action reported
# as though it had run.


def check_custom_layer_guard_is_real():
    """Leave the custom layer empty and confirm the tests catch it."""
    check_revert(
        "a custom layer that is not filled fails the layer tests",
        [(
            "src/managr/overlay.py",
            "        self._reset_view()\n"
            "        if layer_id == custom_mod.LAYER_ID:\n"
            "            self._open_custom()\n",
            "        self._reset_view()\n",
        )],
        "test_layers.py",
        "the custom layer",
    )


def check_custom_fetch_guard_is_real():
    """Fetch the plugins' actions on every render.

    A render happens on every keystroke, so this would be a socket round
    trip per keystroke: the overlay would feel like it is lagging.
    """
    check_revert(
        "a plugin list fetched on every render fails the layer tests",
        [(
            "src/managr/overlay.py",
            "        items = self._visible_items(layer)\n"
            "        lines = self._layer_chrome(layer, inner, items)\n",
            "        items = self._visible_items(layer)\n"
            "        self._open_custom()\n"
            "        lines = self._layer_chrome(layer, inner, items)\n",
        )],
        "test_layers.py",
        "the custom layer's fetch",
    )


def check_shell_command_guard_is_real():
    """Do not run the user's shell command."""
    check_revert(
        "a shell command that does not run fails the layer tests",
        [(
            "src/managr/actions.py",
            '    command = str(getattr(entry, "command", "") or "")\n',
            '    command = ""\n',
        )],
        "test_layers.py",
        "the shell command",
    )


def check_window_command_guard_is_real():
    """Do not say that a popup command cannot be run from the overlay.

    The overlay IS a popup, and a popup is a singleton in Herdr: the
    command has to be pressed in Herdr, and the user has to be told so.
    """
    check_revert(
        "a popup command that is not refused fails the layer tests",
        [(
            "src/managr/actions.py",
            "    return Outcome(\n"
            '        "a %s command cannot be run from here: %s; press %s in herdr"\n'
            "        % (kind, reason, chord),\n"
            "        keep_open=True,\n"
            '        kind="err",\n'
            "    )\n",
            '    return Outcome("", keep_open=True)\n',
        )],
        "test_layers.py",
        "the popup command",
    )


def check_ui_busy_guard_is_real():
    """Bring the dead `ui_busy` branch back.

    `plugin.action.invoke` cannot answer `ui_busy`: in 0.9.5 that error
    comes from `plugin.pane.open`, and the refusal of a plugin action
    that wants a popup of its own happens inside the action's own
    process, while the invoke itself succeeds. So the branch was dead
    code, and a message that reads a meaning into the code is a message
    about something that cannot happen.
    """
    check_revert(
        "a ui_busy branch that is not there fails the layer tests",
        [(
            "src/managr/actions.py",
            "    call = action.calls[0]\n"
            "    ctx.client.call(call.method, _resolve(call.params, ctx, value))\n",
            "    call = action.calls[0]\n"
            "    try:\n"
            "        ctx.client.call(call.method, _resolve(call.params, ctx, value))\n"
            "    except HerdrError as exc:\n"
            "        if exc.code == \"ui_busy\":\n"
            "            return Outcome(\n"
            "                \"close the overlay first -- that action wants a popup of its own\",\n"
            "                keep_open=True,\n"
            "                kind=\"err\",\n"
            "            )\n"
            "        raise\n",
        )],
        "test_layers.py",
        "the dead ui_busy branch",
    )


def check_plugin_action_started_guard_is_real():
    """Say the plugin action ran.

    `plugin.action.invoke` launches the action's own process and returns:
    the log record it hands back says "running", and the outcome only
    turns up later in the plugin log. So the overlay cannot claim the
    action ran.
    """
    check_revert(
        "a plugin action reported as ran fails the layer tests",
        [(
            "src/managr/actions.py",
            '    return Outcome(("started %s" % title).strip(), keep_open=True)\n',
            '    return Outcome(("ran %s" % title).strip(), keep_open=True)\n',
        )],
        "test_layers.py",
        "the plugin action's message",
    )


def check_dotted_plugin_id_guard_is_real():
    """Split the qualified plugin action id at the first dot again.

    Herdr's plugin ids may hold dots and its action ids may not, so the
    first dot is not the separator: "example.worktree-bootstrap.bootstrap"
    is the plugin "example.worktree-bootstrap" and the action
    "bootstrap", and the server refuses the action id the first dot
    would give.
    """
    check_revert(
        "a qualified id split at the first dot fails the layer tests",
        [(
            "src/managr/custom.py",
            "    text = str(action_id or \"\")\n"
            "    if \".\" not in text:\n"
            "        return \"\", text\n",
            "    text = str(action_id or \"\")\n"
            "    if \".\" not in text:\n"
            "        return \"\", text\n"
            "    plugin_id, _, action = text.partition(\".\")\n"
            "    return plugin_id, action\n"
            "    text = str(action_id or \"\")\n"
            "    if \".\" not in text:\n"
            "        return \"\", text\n",
        )],
        "test_layers.py",
        "the qualified plugin action id",
    )


def check_custom_scroll_budget_guard_is_real():
    """Leave the custom layer's own lines out of the scroll's budget.

    They are rows on the screen too, so a budget that does not count
    them is a budget that is too large, and the last rows of the layer
    can never be scrolled into view.
    """
    check_revert(
        "a scroll budget that ignores the custom layer's lines fails the layer tests",
        [(
            "src/managr/overlay.py",
            "        items = self._visible_items(layer)\n"
            "        chrome = self._layer_chrome(layer, inner, items)\n"
            "        return items, self._budget(rows, len(chrome), frame)\n",
            "        items = self._visible_items(layer)\n"
            "        chrome = self._chrome(layer, inner, items)\n"
            "        return items, self._budget(rows, len(chrome), frame)\n",
        )],
        "test_layers.py",
        "the custom layer's scroll budget",
    )


def check_entry_key_pool_guard_is_real():
    """Put the filter's key in the pool of the entry keys.

    The overlay looks the item up before the filter, so an entry that
    owns "/" would run when the user asked for the filter.
    """
    check_revert(
        "an entry key pool with the filter's key fails the layer tests",
        [(
            "src/managr/custom.py",
            '    "!#$%&()*+,-.:;<=>@[\\\\]^_`{|}~"\n',
            '    "!#$%&()*+,-.:;<=>@[\\\\]^_`{|}~/"\n',
        )],
        "test_layers.py",
        "the pool of the entry keys",
    )


def check_shell_context_guard_is_real():
    """Do not give the shell command Herdr's context.

    Herdr's docs promise a custom command six variables, and the overlay
    is the server's child, so it has to set them itself.
    """
    check_revert(
        "a shell command without Herdr's context fails the layer tests",
        [(
            "src/managr/actions.py",
            '        ("HERDR_ACTIVE_PANE_ID", ctx.pane_id),\n',
            "",
        )],
        "test_layers.py",
        "the shell command's context",
    )


def check_layer_glyph_guard_is_real():
    """Take the custom layer's glyph away.

    Every other layer row in the root has one, and the custom layer's
    row is the only one without it.
    """
    check_revert(
        "a layer without a glyph fails the visual tests",
        [(
            "src/managr/data/icons.toml",
            "custom = \"\uea85\"\n",
            "",
        )],
        "test_visuals.py",
        "the custom layer's glyph",
    )


def check_key_overflow_guard_is_real():
    """Do not say how many entries the layer ran out of keys for.

    The pool of keys is finite, and the entries past the end are listed
    but cannot be run.
    """
    check_revert(
        "a silent key overflow fails the layer tests",
        [(
            "src/managr/overlay.py",
            "        if self.custom_unkeyed:\n",
            "        if False:\n",
        )],
        "test_layers.py",
        "the overflow of the entry keys",
    )


def check_overflow_width_guard_is_real():
    """Let the overflow line run past the edge of the terminal.

    A line as wide as the terminal leaves the cursor in the last column,
    and the next character wraps it: every line after it lands a column
    to the left.
    """
    check_revert(
        "an overflow line that is too wide fails the layer tests",
        [(
            "src/managr/overlay.py",
            "                    truncate(\n"
            "                        \"%d %s have no key: they cannot be run from here\"\n"
            "                        % (\n"
            "                            self.custom_unkeyed,\n"
            "                            \"entry\" if self.custom_unkeyed == 1 else \"entries\",\n"
            "                        ),\n"
            "                        inner,\n"
            "                    ),\n",
            "                    \"%d %s have no key: they cannot be run from here\"\n"
            "                    % (\n"
            "                        self.custom_unkeyed,\n"
            "                        \"entry\" if self.custom_unkeyed == 1 else \"entries\",\n"
            "                    ),\n",
        )],
        "test_layers.py",
        "the width of the overflow line",
    )


def check_plugin_action_id_guard_is_real():
    """Invoke the plugin action with the wrong id."""
    check_revert(
        "a plugin action invoked with the wrong id fails the layer tests",
        [(
            "src/managr/actions.py",
            '                {"action_id": "$plugin_action_id", "plugin_id": "$plugin_id"},\n',
            '                {"action_id": "$plugin_id", "plugin_id": "$plugin_action_id"},\n',
        )],
        "test_layers.py",
        "the plugin action's ids",
    )


def check_custom_items_guard_is_real():
    """Put the fetched rows in place of the user's own items.

    The custom layer's rows are fetched, not declared: a user who gave the
    layer items of their own keeps them.
    """
    check_revert(
        "a custom layer that drops the user's items fails the layer tests",
        [(
            "src/managr/overlay.py",
            "        layer.items = list(self.custom_base_items) + [\n",
            "        layer.items = [\n",
        )],
        "test_layers.py",
        "the user's items for the custom layer",
    )


def main():
    print("independent verification of the fixes, by breaking the code")
    check_multikey()
    check_keep_open()
    check_restore_guard_is_real()
    check_crlf_guard_is_real()
    check_keep_guard_is_real()
    check_keyreader_guard_is_real()
    check_signal_guard_is_real()
    check_blocking_handler_guard_is_real()
    check_dirty_decoder_guard_is_real()
    check_swallowed_escape_guard_is_real()
    check_csi_final_guard_is_real()
    check_user_items_guard_is_real()
    check_layer_key_guard_is_real()
    check_custom_reference_guard_is_real()
    check_duplicate_keys_guard_is_real()
    check_reference_key_guard_is_real()
    check_unreachable_warning_guard_is_real()
    check_live_check_guard_is_real()
    check_anti_wrap_guard_is_real()
    check_measured_columns_guard_is_real()
    check_column_padding_guard_is_real()
    check_note_priority_guard_is_real()
    check_input_budget_guard_is_real()
    check_chord_alignment_guard_is_real()
    check_width_measurement_guard_is_real()
    check_control_char_guard_is_real()
    check_frame_budget_guard_is_real()
    check_accent_guard_is_real()
    check_contrast_guard_is_real()
    check_glyph_width_guard_is_real()
    check_icons_guard_is_real()
    check_scroll_guard_is_real()
    check_scroll_clamp_guard_is_real()
    check_row_budget_guard_is_real()
    check_row_budget_overflow_guard_is_real()
    check_frame_height_guard_is_real()
    check_frame_height_pty_guard_is_real()
    check_position_guard_is_real()
    check_position_budget_guard_is_real()
    check_arrow_keys_guard_is_real()
    check_filter_guard_is_real()
    check_filter_owns_the_keys_guard_is_real()
    check_group_heading_guard_is_real()
    check_group_heading_once_guard_is_real()
    check_input_badge_guard_is_real()
    check_filter_scroll_guard_is_real()
    check_escape_clears_the_filter_guard_is_real()
    check_footer_hint_guard_is_real()
    check_filter_reset_guard_is_real()
    check_filter_key_guard_is_real()
    check_scroll_pty_guard_is_real()
    check_filter_pty_guard_is_real()
    check_accent_chain_guard_is_real()
    check_bundled_defaults_guard_is_real()
    check_glyph_measurement_guard_is_real()
    check_unknown_setting_guard_is_real()
    check_navigatr_tree_guard_is_real()
    check_navigatr_cursor_guard_is_real()
    check_navigatr_jump_guard_is_real()
    check_navigatr_key_guard_is_real()
    check_navigatr_row_guard_is_real()
    check_navigatr_status_guard_is_real()
    check_navigatr_kind_guard_is_real()
    check_navigatr_listed_guard_is_real()
    check_navigatr_shadowing_guard_is_real()
    check_navigatr_filter_guard_is_real()
    check_navigatr_failure_guard_is_real()
    check_navigatr_every_render_guard_is_real()
    check_navigatr_band_guard_is_real()
    check_navigatr_band_background_guard_is_real()
    check_agents_band_background_guard_is_real()
    check_navigatr_cut_band_guard_is_real()
    check_session_data_colour_guard_is_real()
    check_session_data_path_guard_is_real()
    check_cursor_hidden_guard_is_real()
    check_cursor_placed_guard_is_real()
    check_agents_number_slot_guard_is_real()
    check_agents_data_colour_guard_is_real()
    check_tab_heading_guard_is_real()
    check_navigatr_tier_colour_guard_is_real()
    check_navigatr_filter_structure_guard_is_real()
    check_navigatr_number_slot_guard_is_real()
    check_navigatr_last_child_guard_is_real()
    check_navigatr_current_marker_guard_is_real()
    check_navigatr_escape_guard_is_real()
    check_navigatr_orphan_guard_is_real()
    check_navigatr_key_length_guard_is_real()
    check_reference_key_length_guard_is_real()
    check_navigatr_warning_once_guard_is_real()
    check_navigatr_status_drop_guard_is_real()
    check_navigatr_numbered_spaces_guard_is_real()
    check_navigatr_label_colour_guard_is_real()
    check_agent_fold_guard_is_real()
    check_navigatr_agent_description_guard_is_real()
    check_navigatr_counts_guard_is_real()
    check_navigatr_number_order_guard_is_real()
    check_agents_location_order_guard_is_real()
    check_agents_flat_drawing_guard_is_real()
    check_navigatr_pane_once_guard_is_real()
    check_navigatr_status_separator_guard_is_real()
    check_overlay_context_refresh_guard_is_real()
    check_overlay_leaves_context_guard_is_real()
    check_status_colour_guard_is_real()
    check_kind_colour_guard_is_real()
    check_navigatr_description_guard_is_real()
    check_agents_sort_guard_is_real()
    check_agents_flat_guard_is_real()
    check_working_category_guard_is_real()
    check_agents_listed_guard_is_real()
    check_agents_key_length_guard_is_real()
    check_agents_key_shadowing_guard_is_real()
    check_agents_numbering_guard_is_real()
    check_agents_open_guard_is_real()
    check_wide_character_guard_is_real()
    check_bar_budget_guard_is_real()
    check_bar_column_budget_guard_is_real()
    check_api_method_guard_is_real()
    check_api_parameter_guard_is_real()
    check_unreachable_guard_is_real()
    check_custom_layer_guard_is_real()
    check_custom_fetch_guard_is_real()
    check_shell_command_guard_is_real()
    check_window_command_guard_is_real()
    check_ui_busy_guard_is_real()
    check_plugin_action_started_guard_is_real()
    check_dotted_plugin_id_guard_is_real()
    check_custom_scroll_budget_guard_is_real()
    check_entry_key_pool_guard_is_real()
    check_shell_context_guard_is_real()
    check_layer_glyph_guard_is_real()
    check_key_overflow_guard_is_real()
    check_overflow_width_guard_is_real()
    check_plugin_action_id_guard_is_real()
    check_custom_items_guard_is_real()
    check_config_path_guard_is_real()
    check_unknown_setting_guard_is_real_in_the_bar()
    check_missing_label_guard_is_real()
    check_config_path_guard_is_real_here()
    check_accent_config_guard_is_real()
    check_unknown_field_guard_is_real()
    check_explain_count_guard_is_real()
    check_plugin_id_guard_is_real()
    check_renamed_binding_guard_is_real()
    check_fold_guard_is_real()
    check_navigatr_footer_guard_is_real()
    check_tab_move_index_guard_is_real()
    check_pane_move_guard_is_real()
    check_move_tab_guard_is_real()
    check_manifest_guard_is_real()
    check_dead_code_guard_is_real()


# -- the shape pass ----------------------------------------------------------
#
# The checks below are the guards the shape pass added: the layer-state
# window, the one answer for the config path, the one merge rule, the
# published heartbeat, the count in `make explain`, the plugin id, the
# manifest, the missing-label warning, and the dead code that is gone.


def check_config_path_guard_is_real():
    """Hardcode the config path again (the sibling plugin).

    The config dir honoured XDG_CONFIG_HOME and the config file did not:
    two answers, and the state file somewhere the config was not.
    """
    check_revert(
        "a hardcoded config path fails the bar tests",
        [(
            "src/herdrbar/keymap.py",
            '    return os.path.join(config_dir(), "config.toml")\n',
            '    return os.path.expanduser("~/.config/herdr/config.toml")\n',
        )],
        "test_bar.py",
        "the one answer for the config path",
        tree=copy_v1_tree,
    )


def check_unknown_setting_guard_is_real_in_the_bar():
    """Ignore an unknown setting in `bar.toml` in silence (the sibling plugin).

    A misspelled setting that is ignored in silence looks exactly like a
    setting that did not work.
    """
    check_revert(
        "a silently ignored bar setting fails the bar tests",
        [(
            "src/herdrbar/merge.py",
            "        if key not in known:\n"
            "            unknown_setting(plugin, path, key, seen)\n"
            "            continue\n",
            "        if key not in known:\n"
            "            continue\n",
        )],
        "test_bar.py",
        "the unknown setting warning",
        tree=copy_v1_tree,
    )


def check_missing_label_guard_is_real():
    """Silence the missing-label warning again (the sibling plugin).

    The warning is the early signal that a Herdr upgrade added an action
    the labels do not cover, and a broken user config must not hide it.
    """
    check_revert(
        "a silenced missing label fails the keymap tests",
        [(
            "src/herdrbar/keymap.py",
            '            warnings.append("no label for %r" % action)\n',
            '            if not warn:\n'
            '                warnings.append("no label for %r" % action)\n',
        )],
        "test_keymap.py",
        "the missing label warning",
        tree=copy_v1_tree,
    )


def check_config_path_guard_is_real_here():
    """Hardcode the config path again (this plugin)."""
    check_revert(
        "a hardcoded config path fails the shared tests",
        [(
            "src/managr/keymap.py",
            '    return os.path.join(config_dir(), "config.toml")\n',
            '    return os.path.expanduser("~/.config/herdr/config.toml")\n',
        )],
        "test_shared.py",
        "the one answer for the config path",
    )


def check_accent_config_guard_is_real():
    """Read the accent from a hardcoded config file again."""
    check_revert(
        "an accent from a hardcoded config fails the shared tests",
        [(
            "src/managr/visuals.py",
            "    return keymap_mod.user_config_path()\n",
            '    return os.path.join(os.path.expanduser("~"), ".config", "herdr", "config.toml")\n',
        )],
        "test_shared.py",
        "the accent's config file",
    )


def check_unknown_field_guard_is_real():
    """Ignore an unknown field of a layer in silence."""
    check_revert(
        "a silently ignored layer field fails the shared tests",
        [(
            "src/managr/tree.py",
            "            if field not in known:\n"
            "                merge_mod.unknown_field(\n"
            "                    PLUGIN, path, field, what, name or None\n"
            "                )\n",
            "            if field not in known:\n"
            "                pass\n",
        )],
        "test_shared.py",
        "the unknown field warning",
    )


def check_explain_count_guard_is_real():
    """Count the internal runners as Herdr actions again."""
    check_revert(
        "an overcounted explain fails the entry point tests",
        [(
            "bin/managr",
            "    public = actions_mod.public_executors()\n",
            "    public = actions_mod.EXECUTORS\n",
        )],
        "test_entrypoint.py",
        "the count in explain",
    )


def check_plugin_id_guard_is_real():
    """Ask Herdr for a plugin that does not exist again."""
    check_revert(
        "a plugin id that does not exist fails the entry point tests",
        [(
            "bin/managr",
            'PLUGIN_ID = os.environ.get("HERDR_PLUGIN_ID", "managr")\n',
            'PLUGIN_ID = os.environ.get("HERDR_PLUGIN_ID", "herdr-chord")\n',
        )],
        "test_entrypoint.py",
        "the plugin id",
    )


def check_renamed_binding_guard_is_real():
    """Forget the ids the plugin used to have.

    The binding an earlier name wrote names the action of a plugin that
    does not exist. Without the list of those ids the installer would add
    a second binding for the same key instead of replacing the old one.
    """
    check_revert(
        "a binding of an earlier plugin id is not replaced fails the configure tests",
        [(
            "scripts/configure.py",
            'LEGACY_ACTION_IDS = (\n'
            '    "herdr-chord.open",\n'
            '    "herdr-keybar.open",\n'
            '    "herdr-layers.open",\n'
            ')\n',
            'LEGACY_ACTION_IDS = ()\n',
        )],
        "test_configure.py",
        "the list of earlier plugin ids",
    )


def check_fold_guard_is_real():
    """Never fold a space."""
    check_revert(
        "a space that is never folded fails the navigatr tests",
        [(
            "src/managr/overlay.py",
            "        folded = self._folded_spaces(rows)\n",
            "        folded = set()\n",
        )],
        "test_layers.py",
        "the folds",
    )


def check_tab_move_index_guard_is_real():
    """Put the tab one place short of where it should go.

    Herdr's `tab.move` takes the place BEFORE the tab at that index, and
    it counts the moved tab as still in the list. So the index that puts a
    tab where another one is is not the other one's index when the moved
    tab is in front of it.
    """
    check_revert(
        "a tab moved to the wrong place fails the navigatr tests",
        [(
            "src/managr/navigatr.py",
            "    insert_index = target_index if target_index <= carried_index else target_index + 1\n",
            "    insert_index = target_index\n",
        )],
        "test_layers.py",
        "the index a tab is moved to",
    )


def check_pane_move_guard_is_real():
    """Do not move the pane at all."""
    check_revert(
        "a pane that is not moved fails the navigatr tests",
        [(
            "src/managr/navigatr.py",
            '            "pane.move",\n',
            '            "pane.get",\n',
        )],
        "test_layers.py",
        "the pane move",
    )


def check_move_tab_guard_is_real():
    """Move the tab one place short of where it should go.

    `tab.move` puts the tab BEFORE the tab at that index, and it counts the
    moved tab as still being in the list. So the index that lands a tab one
    place along the bar is not the index of the tab it should end up at.
    The action used to send that index, and the tab did not move -- and the
    action said it had.
    """
    check_revert(
        "a tab that is not moved along fails the layer tests",
        [(
            "src/managr/actions.py",
            "    insert_index = target if target < index else target + 1\n",
            "    insert_index = target\n",
        )],
        "test_layers.py",
        "the index a tab is moved along to",
    )


def check_navigatr_footer_guard_is_real():
    """Take the fold keys and the rearranging out of the footer.

    The footer is the only place the user can read what the keys do, and
    the fold keys and the rearranging are the ones nobody can guess. So
    they are in the footer at every width that draws the tree -- and the
    agents list, which has neither of those things, does not offer them.
    """
    check_revert(
        "a footer without the fold keys fails the layer tests",
        [(
            "src/managr/overlay.py",
            '        for text in (\n'
            '            "esc back · 1-9 space · h/l fold · H/L all · r move · enter jump · ↑↓ j/k move",\n'
            '            "esc back · 1-9 space · h/l fold · r move · enter jump · ↑↓ j/k move",\n'
            '            "esc back · h/l fold · H/L all · r move · enter jump · ↑↓ j/k move",\n'
            '            "esc back · h/l fold · r move · enter jump · ↑↓ j/k move",\n'
            '            "esc back · h/l fold · r move · enter jump",\n'
            '            "esc back · h/l fold · enter jump · ↑↓ j/k move",\n'
            '            "esc back · enter jump · ↑↓ j/k move · /filter",\n'
            '            "esc back · enter jump · ↑↓ j/k move",\n'
            '            "esc back",\n'
            "        ):\n",
            '        for text in (\n'
            '            "esc back · 1-9 %s · enter jump · ↑↓ j/k move · /filter" % noun,\n'
            '            "esc back · enter jump · ↑↓ j/k move · /filter",\n'
            "        ):\n",
        )],
        "test_layers.py",
        "the footer",
    )


def check_manifest_guard_is_real():
    """Let the manifest describe a design the code does not have."""
    check_revert(
        "a manifest that describes another design fails the configure tests",
        [(
            "herdr-plugin.toml",
            '#   key = "prefix+space"\n',
            '#   key = "prefix+?"\n',
        )],
        "test_configure.py",
        "the manifest's trigger",
    )


def check_dead_code_guard_is_real():
    """Bring the dead `ping` back."""
    check_revert(
        "a socket client with a dead ping fails the shared tests",
        [(
            "src/managr/client.py",
            '            return message.get("result", {})\n',
            '            return message.get("result", {})\n'
            "\n"
            "    def ping(self):\n"
            "        try:\n"
            '            self.call("ping")\n'
            "            return True\n"
            "        except (HerdrError, HerdrUnavailable):\n"
            "            return False\n",
        )],
        "test_shared.py",
        "the dead socket ping",
    )

    failed = [name for name, ok, _ in RESULTS if not ok]
    print("\n%d passed, %d failed" % (len(RESULTS) - len(failed), len(failed)))
    for name in failed:
        print("  failed: %s" % name)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
