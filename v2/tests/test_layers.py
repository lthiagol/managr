"""Tests for the v2 layer tree and the action mapping.

These are the parts that can be wrong without anything crashing: a layer entry
marked as a hint, an action that claims to be executable but has no executor,
a private key colliding with another key in the same layer.

Run: python3 tests/test_layers.py
"""

import contextlib
import io
import json
import os
import re
import shutil
import tempfile
import sys
import time
import unicodedata
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

from managr import actions as actions_mod  # noqa: E402
from managr import keymap as keymap_mod  # noqa: E402
from managr import merge as merge_mod  # noqa: E402
from managr import navigatr as navigatr_mod  # noqa: E402
from managr import overlay as overlay_mod  # noqa: E402
from managr import tomlite  # noqa: E402
from managr import tree as tree_mod  # noqa: E402
from managr import visuals as visuals_mod  # noqa: E402

# The escape sequences the overlay writes. The checks look at the text the
# terminal shows, not at the bytes.
ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")

# The colour runs: the escape sequences that open and close a style.
STYLE = re.compile(r"\x1b\[([0-9;]*)m")


def display_width(text):
    """The width of a line in terminal columns.

    Measured here rather than with the renderer's own function: a test
    that asks the code under test how wide its own output is proves
    nothing. A character is not a column -- a CJK title is one character
    and two columns -- so `len()` under-counts a row that holds one.
    """
    plain = ANSI.sub("", text)
    return sum(
        2 if unicodedata.east_asian_width(char) in ("W", "F") else 1 for char in plain
    )


def styled_runs(line):
    """(text, styled) for every run of plain text in a raw line.

    `styled` says whether the text is inside a colour run. An escape that
    opens a colour and is never closed colours everything after it, which
    is how a marker's colour leaks into the rest of the row.
    """
    runs = []
    styled = False
    position = 0
    for match in STYLE.finditer(line):
        text = line[position : match.start()]
        if text:
            runs.append((text, styled))
        styled = match.group(1) not in ("0", "")
        position = match.end()
    text = line[position:]
    if text:
        runs.append((text, styled))
    return runs


def style_runs(line):
    """(text, escape) for every run of plain text in a raw line.

    The escape is the sequence that opened the run, or "" when the text
    is not inside one. `styled_runs` only says whether a run is styled;
    this says which colour it is, which is what a leak check needs: the
    description after the name is styled too, in the muted colour, and
    that is not a leak.
    """
    runs = []
    escape = ""
    position = 0
    for match in STYLE.finditer(line):
        text = line[position : match.start()]
        if text:
            runs.append((text, escape))
        escape = match.group(0)
        position = match.end()
    text = line[position:]
    if text:
        runs.append((text, escape))
    return runs


def frame_content(line):
    """The content of a frame row: what is between the two bars.

    The frame pads the row to the inside of the box, so the content has to
    be measured on its own -- the padding would hide the spaces at the end
    of the row, and a row that ends in two stray spaces is a row that is
    two columns wider than it looks.
    """
    bar = overlay_mod.FRAME_VERTICAL
    first = line.find(bar)
    last = line.rfind(bar)
    if first < 0 or last <= first:
        return line
    return line[first + 1 : last]


def run_with(line, text):
    """The escape the run holding `text` is drawn in, or ""."""
    for run_text, escape in style_runs(line):
        if text in run_text:
            return escape
    return ""


def run_exactly(line, text):
    """The escape the run that is exactly `text` is drawn in, or ""."""
    for run_text, escape in style_runs(line):
        if run_text == text:
            return escape
    return ""


def style_before(line, text):
    """The escapes that opened the run of `text`, from the last reset on.

    A run can be opened by more than one escape: the name of the row
    under the cursor is in the accent *and* in bold, and the bold is the
    last escape before the text. So the whole sequence is what says what
    the run is drawn in, and the last one alone does not.
    """
    style = ""
    position = 0
    for match in STYLE.finditer(line):
        if text in line[position : match.start()]:
            return style
        if match.group(1) in ("0", ""):
            # A reset ends the run: the style starts again after it.
            style = ""
        else:
            style += match.group(0)
        position = match.end()
    if text in line[position:]:
        return style
    return ""


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


def fake_keymap():
    return keymap_mod.Keymap(
        prefix="C-a",
        bindings={
            "focus_pane_left": keymap_mod.Binding("focus_pane_left", "focus left", "h"),
            "split_vertical": keymap_mod.Binding("split_vertical", "split right", "v"),
            "help": keymap_mod.Binding("help", "help", "?"),
            "settings": keymap_mod.Binding("settings", "settings", "s"),
        },
    )


class TestTree(unittest.TestCase):
    def setUp(self):
        self.tree = tree_mod.build(keymap=fake_keymap(), labels={})

    def test_root_lists_every_layer(self):
        ids = {item.action for item in self.tree.root.items}
        self.assertIn("layer:nav", ids)
        self.assertIn("layer:panes", ids)
        self.assertIn("layer:reference", ids)

    def test_layer_entries_are_not_hints(self):
        for item in self.tree.root.items:
            self.assertTrue(item.executable, item.action)
            self.assertFalse(item.is_hint, item.action)

    def test_chord_comes_from_the_keymap(self):
        layer = self.tree.layers["nav"]
        item = next(i for i in layer.items if i.action == "focus_pane_left")
        self.assertEqual(item.chord, "C-a h")

    def test_unbound_action_has_no_chord(self):
        layer = self.tree.layers["panes"]
        item = next(i for i in layer.items if i.action == "resize_pane_left")
        self.assertEqual(item.chord, "")

    def test_hint_rows_are_not_executable(self):
        layer = self.tree.layers["session"]
        item = next(i for i in layer.items if i.action == "settings")
        self.assertTrue(item.is_hint)
        self.assertFalse(item.executable)

    def test_every_item_with_a_key_is_unique_in_its_layer(self):
        for layer_id, layer in self.tree.layers.items():
            keys = [i.key for i in layer.items if i.key]
            self.assertEqual(
                len(keys), len(set(keys)), "duplicate key in layer %s" % layer_id
            )

    def test_every_referenced_action_exists(self):
        known = set(actions_mod.EXECUTORS) | set(actions_mod.UNREACHABLE)
        for layer in self.tree.layers.values():
            for item in layer.items:
                self.assertIn(item.action, known, item.action)

    def test_no_orphaned_layers(self):
        referenced = {
            item.action.split(":", 1)[1]
            for item in self.tree.root.items
            if item.is_layer
        }
        self.assertEqual(referenced - {"reference"}, set(self.tree.layers))

    def test_reference_layer_covers_the_keymap(self):
        actions = {item.action for item in self.tree.reference.items}
        self.assertIn("focus_pane_left", actions)
        self.assertIn("help", actions)

    def test_reference_layer_lists_custom_commands(self):
        # The user's own `[[keys.command]]` entries are live bindings, and
        # the reference view claims to list every one of them.
        km = keymap_mod.Keymap(
            prefix="C-a",
            bindings={
                "help": keymap_mod.Binding("help", "help", "?"),
            },
            custom=[
                keymap_mod.CustomCommand(
                    key="prefix+space",
                    type="popup",
                    command="managr overlay",
                    description="open the layer overlay",
                    chord="space",
                    direct=False,
                )
            ],
        )
        tree = tree_mod.build(keymap=km, labels={})
        entries = {item.desc: item for item in tree.reference.items}
        self.assertIn("open the layer overlay", entries)
        self.assertEqual(entries["open the layer overlay"].chord, "C-a space")
        # A custom command is not something the overlay can run.
        self.assertTrue(entries["open the layer overlay"].is_hint)

    def test_items_needing_input_are_marked(self):
        layer = self.tree.layers["tabs"]
        item = next(i for i in layer.items if i.action == "rename_tab")
        self.assertEqual(item.prompt, "tab name")


class TestUserOverride(unittest.TestCase):
    """A `layers.toml` in the plugin config dir merges over the bundled one."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.path = os.path.join(self.dir, "layers.toml")
        previous = os.environ.get("HERDR_PLUGIN_CONFIG_DIR")
        os.environ["HERDR_PLUGIN_CONFIG_DIR"] = self.dir
        self.addCleanup(
            lambda: os.environ.__setitem__("HERDR_PLUGIN_CONFIG_DIR", previous)
            if previous
            else os.environ.pop("HERDR_PLUGIN_CONFIG_DIR", None)
        )

    def _write(self, text):
        with open(self.path, "w", encoding="utf-8") as handle:
            handle.write(text)

    def test_a_new_layer_is_added_and_builtins_are_inherited(self):
        self._write(
            '[[layer]]\nid = "mine"\nkey = "m"\ntitle = "my stuff"\n\n'
            '[[item]]\nlayer = "mine"\nkey = "z"\naction = "zoom"\n'
        )
        tree = tree_mod.build(keymap=fake_keymap(), labels={})
        self.assertIn("mine", tree.layers)
        self.assertIn("panes", tree.layers)
        root_actions = {item.action for item in tree.root.items}
        self.assertIn("layer:mine", root_actions)
        self.assertIn("layer:panes", root_actions)

    def test_a_redefined_layer_replaces_its_items(self):
        self._write(
            '[[layer]]\nid = "panes"\nkey = "p"\ntitle = "panes"\n\n'
            '[[item]]\nlayer = "panes"\nkey = "q"\naction = "zoom"\n'
        )
        tree = tree_mod.build(keymap=fake_keymap(), labels={})
        keys = [i.key for i in tree.layers["panes"].items]
        self.assertEqual(keys, ["q"])

    def test_a_redefined_layer_without_items_keeps_them(self):
        self._write('[[layer]]\nid = "panes"\nkey = "p"\ntitle = "pane things"\n')
        tree = tree_mod.build(keymap=fake_keymap(), labels={})
        self.assertTrue(tree.layers["panes"].items)
        self.assertEqual(tree.layers["panes"].title, "pane things")

    def test_items_for_a_builtin_layer_are_not_dropped(self):
        # A user file that adds items to a built-in layer, without
        # redefining the layer itself, used to be ignored completely: the
        # built-in items stayed and the user's vanished with no warning.
        self._write('[[item]]\nlayer = "panes"\nkey = "q"\naction = "zoom"\n')
        tree = tree_mod.build(keymap=fake_keymap(), labels={})
        keys = [i.key for i in tree.layers["panes"].items]
        self.assertEqual(keys, ["q"])

    def test_a_redefined_layer_without_a_key_keeps_the_builtin_key(self):
        # Overwriting the key with "" would drop the layer out of the root,
        # because the root only lists layers that have a key.
        self._write('[[layer]]\nid = "panes"\ntitle = "pane things"\n')
        tree = tree_mod.build(keymap=fake_keymap(), labels={})
        self.assertEqual(tree.layers["panes"].key, "p")
        root_actions = {item.action for item in tree.root.items}
        self.assertIn("layer:panes", root_actions)

    def test_the_reference_key_comes_from_the_user_file(self):
        self._write('reference_key = "y"\n')
        tree = tree_mod.build(keymap=fake_keymap(), labels={})
        self.assertEqual(tree.reference_key, "y")
        root_keys = [item.key for item in tree.root.items]
        self.assertIn("y", root_keys)
        self.assertNotIn("?", root_keys)

    def test_the_reference_key_defaults_to_the_bundled_one(self):
        tree = tree_mod.build(keymap=fake_keymap(), labels={})
        self.assertEqual(tree.reference_key, "?")

    def test_the_navigatr_key_comes_from_the_user_file(self):
        # The key is a setting, not a constant in the overlay: a user who
        # wants the navigatr somewhere else moves it here.
        self._write('navigatr_key = "x"\n')
        tree = tree_mod.build(keymap=fake_keymap(), labels={})
        self.assertEqual(tree.navigatr_key, "x")
        keys = [item.key for item in tree.root.items]
        self.assertIn("x", keys)
        self.assertNotIn("j", keys)

    def test_the_users_navigatr_key_opens_the_navigatr(self):
        self._write('navigatr_key = "x"\n')
        tree = tree_mod.build(keymap=fake_keymap(), labels={})
        overlay = overlay_mod.Overlay(tree, actions_mod.Context(FakeSession()))
        overlay.handle(overlay_mod.Key("char", "x"))
        self.assertIsNotNone(overlay.navigatr, "the user's key did not open it")

    def test_the_navigatr_key_defaults_to_the_bundled_one(self):
        tree = tree_mod.build(keymap=fake_keymap(), labels={})
        self.assertEqual(tree.navigatr_key, "j")

    def test_the_agents_key_comes_from_the_user_file(self):
        # The key is a setting, not a constant in the overlay: a user who
        # wants the agents list somewhere else moves it here.
        self._write('agents_key = "x"\n')
        tree = tree_mod.build(keymap=fake_keymap(), labels={})
        self.assertEqual(tree.agents_key, "x")
        keys = [item.key for item in tree.root.items]
        self.assertIn("x", keys)
        self.assertNotIn("a", keys)

    def test_the_users_agents_key_opens_the_agents_list(self):
        self._write('agents_key = "x"\n')
        tree = tree_mod.build(keymap=fake_keymap(), labels={})
        overlay = overlay_mod.Overlay(tree, actions_mod.Context(FakeSession()))
        overlay.handle(overlay_mod.Key("char", "x"))
        self.assertIsNotNone(overlay.navigatr, "the user's key did not open it")
        self.assertEqual(overlay.navigatr.kind, "agents")

    def test_the_agents_key_defaults_to_the_bundled_one(self):
        tree = tree_mod.build(keymap=fake_keymap(), labels={})
        self.assertEqual(tree.agents_key, "a")

    def test_an_empty_agents_key_means_not_set(self):
        # An empty value is not a value: the bundled key stands, exactly
        # like the navigatr's key.
        self._write('agents_key = ""\n')
        tree = tree_mod.build(keymap=fake_keymap(), labels={})
        self.assertEqual(tree.agents_key, "a")

    def test_a_layer_that_takes_the_agents_key_keeps_it(self):
        # The layer's own binding wins, exactly like the filter's key: the
        # agents list is then not reachable by that key, and the root does
        # not list an entry that would promise it is.
        self._write('[[layer]]\nid = "mine"\nkey = "a"\ntitle = "my stuff"\n')
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            tree = tree_mod.build(keymap=fake_keymap(), labels={})
        actions = [item.action for item in tree.root.items]
        self.assertNotIn(tree_mod.AGENTS_ACTION, actions)
        self.assertIn("layer:mine", actions)
        owners = [item.action for item in tree.root.items if item.key == "a"]
        self.assertEqual(owners, ["layer:mine"])
        # And it is said out loud: a root that quietly does not have it
        # looks like the plugin lost it.
        self.assertIn("agents", stderr.getvalue())
        self.assertIn("not reachable", stderr.getvalue())

    def test_an_agents_key_that_cannot_be_pressed_is_not_listed(self):
        # The overlay's keyspace is single characters, so a key of two
        # characters is an entry that promises a key no keypress could
        # ever reach.
        self._write('agents_key = "aa"\n')
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            tree = tree_mod.build(keymap=fake_keymap(), labels={})
        actions = [item.action for item in tree.root.items]
        self.assertNotIn(tree_mod.AGENTS_ACTION, actions)
        self.assertNotIn("aa", [item.key for item in tree.root.items])
        # And it is said out loud: a key the user set and the plugin does
        # not use is worth a word.
        self.assertIn("aa", stderr.getvalue())
        self.assertIn("cannot be pressed", stderr.getvalue())

    def test_an_agents_key_that_is_the_navigatrs_key_is_refused(self):
        # The navigatr's key is taken already, so the agents list cannot
        # have it: a key in the root twice is a key that runs two things.
        self._write('agents_key = "j"\n')
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            tree = tree_mod.build(keymap=fake_keymap(), labels={})
        owners = [item.action for item in tree.root.items if item.key == "j"]
        self.assertEqual(owners, [tree_mod.NAVIGATR_ACTION])
        self.assertIn("not reachable", stderr.getvalue())

    def test_an_agents_key_that_is_the_reference_key_is_refused(self):
        self._write('agents_key = "?"\n')
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            tree = tree_mod.build(keymap=fake_keymap(), labels={})
        owners = [item.action for item in tree.root.items if item.key == "?"]
        self.assertEqual(owners, ["layer:reference"])
        self.assertIn("not reachable", stderr.getvalue())

    def test_a_layer_that_takes_the_navigatr_key_keeps_it(self):
        # The layer's own binding wins, exactly like the filter's key: the
        # navigatr is then not reachable by that key, and the root does
        # not list an entry that would promise it is.
        self._write('[[layer]]\nid = "mine"\nkey = "j"\ntitle = "my stuff"\n')
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            tree = tree_mod.build(keymap=fake_keymap(), labels={})
        actions = [item.action for item in tree.root.items]
        self.assertNotIn(tree_mod.NAVIGATR_ACTION, actions)
        # The layer's own entry is there, and it is the one that has "j".
        self.assertIn("layer:mine", actions)
        owners = [item.action for item in tree.root.items if item.key == "j"]
        self.assertEqual(owners, ["layer:mine"])
        # And it is said out loud: a root that quietly does not have the
        # navigatr looks like the plugin lost it.
        self.assertIn("navigatr", stderr.getvalue())
        self.assertIn("not reachable", stderr.getvalue())

    def test_a_layer_that_takes_the_navigatr_key_opens_the_layer(self):
        self._write('[[layer]]\nid = "mine"\nkey = "j"\ntitle = "my stuff"\n')
        tree = tree_mod.build(keymap=fake_keymap(), labels={})
        overlay = overlay_mod.Overlay(tree, actions_mod.Context(FakeSession()))
        overlay.handle(overlay_mod.Key("char", "j"))
        self.assertIsNone(overlay.navigatr, "the navigatr took the layer's key")
        self.assertEqual(overlay.layer_id, "mine")

    def test_a_navigatr_key_that_cannot_be_pressed_is_not_listed(self):
        # The overlay's keyspace is single characters: `handle()` compares
        # the character that was typed with the key of an entry. A key of
        # two characters is an entry that promises a key no keypress could
        # ever reach -- the same lie as an entry that promises a key a
        # layer already took.
        self._write('navigatr_key = "jj"\n')
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            tree = tree_mod.build(keymap=fake_keymap(), labels={})
        actions = [item.action for item in tree.root.items]
        self.assertNotIn(tree_mod.NAVIGATR_ACTION, actions)
        self.assertNotIn("jj", [item.key for item in tree.root.items])
        # And it is said out loud: a key the user set and the plugin does
        # not use is worth a word.
        self.assertIn("jj", stderr.getvalue())
        self.assertIn("cannot be pressed", stderr.getvalue())

    def test_pressing_one_letter_of_a_two_letter_key_says_so(self):
        # The promise is not kept, and the overlay says so rather than
        # doing nothing at all.
        self._write('navigatr_key = "jj"\n')
        tree = tree_mod.build(keymap=fake_keymap(), labels={})
        overlay = overlay_mod.Overlay(tree, actions_mod.Context(FakeSession()))
        overlay.handle(overlay_mod.Key("char", "j"))
        self.assertIsNone(overlay.navigatr)
        self.assertIn("no binding", overlay.message)

    def test_a_reference_key_that_cannot_be_pressed_is_not_listed(self):
        # The same rule as the navigatr's key: the reference view's key
        # is pressed in the overlay too, so it has to be one character.
        self._write('reference_key = "??"\n')
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            tree = tree_mod.build(keymap=fake_keymap(), labels={})
        descs = [item.desc for item in tree.root.items]
        self.assertNotIn("all bindings", descs)
        self.assertIn("cannot be pressed", stderr.getvalue())

    def test_the_shipped_keys_are_all_one_character(self):
        # The keys the plugin ships with have to be pressable, or the
        # guard above would be the only thing standing between the user
        # and a root that lists a key nobody can type.
        path = os.path.join(ROOT, "src", "managr", "data", "layers.toml")
        with open(path, "r", encoding="utf-8") as handle:
            data = tomlite.loads(handle.read())
        for name in ("reference_key", "navigatr_key", "agents_key"):
            self.assertEqual(len(data.get(name, "")), 1, name)
        for entry in data.get("layer", []) or []:
            self.assertEqual(len(entry.get("key", "")), 1, entry)

    def test_an_unknown_setting_is_reported(self):
        self._write('navigater_key = "x"\n')
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            tree_mod.build(keymap=fake_keymap(), labels={})
        self.assertIn("navigater_key", stderr.getvalue())
        self.assertEqual(tree_mod.build(keymap=fake_keymap(), labels={}).navigatr_key, "j")

    def test_items_for_a_layer_that_does_not_exist_warn(self):
        # An item that names a layer nobody declared loads into a layer
        # that has no key, so the root cannot list it and the items are
        # unreachable. Silence would look like the plugin lost them.
        self._write('[[item]]\nlayer = "brandnew"\nkey = "z"\naction = "zoom"\n')
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            tree = tree_mod.build(keymap=fake_keymap(), labels={})
        self.assertIn("brandnew", stderr.getvalue())
        self.assertIn("unreachable", stderr.getvalue())
        # The items are still there; they are just not reachable.
        self.assertEqual([i.key for i in tree.layers["brandnew"].items], ["z"])
        root_actions = {item.action for item in tree.root.items}
        self.assertNotIn("layer:brandnew", root_actions)

    def test_a_layer_that_is_reachable_does_not_warn(self):
        self._write(
            '[[layer]]\nid = "mine"\nkey = "m"\ntitle = "my stuff"\n\n'
            '[[item]]\nlayer = "mine"\nkey = "z"\naction = "zoom"\n'
        )
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            tree_mod.build(keymap=fake_keymap(), labels={})
        self.assertEqual(stderr.getvalue(), "")

    def test_a_layer_named_by_an_item_does_not_warn(self):
        # The layer has no key, but an item names it, so it is reachable.
        self._write(
            '[[layer]]\nid = "mine"\ntitle = "my stuff"\n\n'
            '[[item]]\nlayer = "nav"\nkey = "m"\naction = "layer:mine"\n\n'
            '[[item]]\nlayer = "mine"\nkey = "z"\naction = "zoom"\n'
        )
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            tree_mod.build(keymap=fake_keymap(), labels={})
        self.assertEqual(stderr.getvalue(), "")

    def test_a_users_items_for_the_custom_layer_are_kept(self):
        # The custom layer's rows are fetched, not declared: a user who
        # gives the layer items of their own keeps them.
        self._write(
            '[[layer]]\nid = "custom"\nkey = "c"\ntitle = "custom"\n\n'
            '[[item]]\nlayer = "custom"\nkey = "Q"\naction = "zoom"\n'
            'desc = "my own item"\n'
        )
        tree = tree_mod.build(keymap=fake_keymap(), labels={})
        overlay = overlay_mod.Overlay(tree, actions_mod.Context(FakePlugins()))
        overlay.handle(overlay_mod.Key("char", "c"))
        descs = [item.desc for item in tree.layers["custom"].items]
        self.assertIn("my own item", descs)
        # And the fetched rows are added to them, not put in their place.
        self.assertIn("Open the layered keymap", descs)


class TestTheShadowingWarning(unittest.TestCase):
    """The warning about a shadowed navigatr key is printed once.

    The tree is built once per popup, and the popup is opened over and
    over: a warning on every build is a warning the user sees every time
    they open the overlay. The other one-shot diagnostics go through the
    same set, and this one has to as well.
    """

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.path = os.path.join(self.dir, "layers.toml")
        previous = os.environ.get("HERDR_PLUGIN_CONFIG_DIR")
        os.environ["HERDR_PLUGIN_CONFIG_DIR"] = self.dir
        self.addCleanup(
            lambda: os.environ.__setitem__("HERDR_PLUGIN_CONFIG_DIR", previous)
            if previous
            else os.environ.pop("HERDR_PLUGIN_CONFIG_DIR", None)
        )
        # The warnings are remembered for the life of the process, so the
        # set is cleared on both sides: this test is about what is said,
        # not about what an earlier test said.
        merge_mod._WARNED.clear()
        self.addCleanup(merge_mod._WARNED.clear)

    def _write(self, text):
        with open(self.path, "w", encoding="utf-8") as handle:
            handle.write(text)

    def _build(self):
        """Build the tree and return what it said on stderr."""
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            tree_mod.build(keymap=fake_keymap(), labels={})
        return stderr.getvalue()

    def test_the_shadowing_warning_is_printed_once(self):
        self._write('[[layer]]\nid = "mine"\nkey = "j"\ntitle = "my stuff"\n')
        first = self._build()
        self.assertIn("not reachable", first, "the shadowing was not reported")
        second = self._build()
        self.assertEqual(second, "", "the warning was printed a second time")

    def test_another_shadowed_key_is_reported_on_its_own(self):
        # The warning is per key, not per process: a user who moves the
        # key has to hear about the new one.
        self._write('[[layer]]\nid = "mine"\nkey = "j"\ntitle = "my stuff"\n')
        self._build()
        self._write(
            'navigatr_key = "x"\n\n'
            '[[layer]]\nid = "mine"\nkey = "x"\ntitle = "my stuff"\n'
        )
        self.assertIn("not reachable", self._build())


class TestActions(unittest.TestCase):
    def test_every_executor_is_callable(self):
        for name, fn in actions_mod.EXECUTORS.items():
            self.assertTrue(callable(fn), name)

    def test_actions_needing_input_have_an_executor(self):
        for name in actions_mod.NEEDS_INPUT:
            self.assertIn(name, actions_mod.EXECUTORS, name)

    def test_unreachable_actions_have_no_executor(self):
        for name in actions_mod.UNREACHABLE:
            self.assertNotIn(name, actions_mod.EXECUTORS, name)

    def test_context_refresh_survives_a_rejected_call(self):
        class Boom:
            def call(self, method, params=None):
                from managr.client import HerdrError

                raise HerdrError("nope", "no")

        ctx = actions_mod.Context(Boom(), pane_id="w1:p1")
        ctx.refresh()
        self.assertEqual(ctx.pane_id, "w1:p1")

    def test_focus_relative_tab_wraps(self):
        class Fake:
            def __init__(self):
                self.focused = []

            def call(self, method, params=None):
                if method == "tab.list":
                    return {
                        "tabs": [
                            {"tab_id": "w1:t1", "label": "one"},
                            {"tab_id": "w1:t2", "label": "two"},
                        ]
                    }
                if method == "tab.focus":
                    self.focused.append(params["tab_id"])
                    return {}
                raise AssertionError("unexpected method %s" % method)

        fake = Fake()
        ctx = actions_mod.Context(fake, workspace_id="w1", tab_id="w1:t2")
        outcome = actions_mod.EXECUTORS["next_tab"](ctx)
        self.assertEqual(fake.focused, ["w1:t1"])
        self.assertTrue(outcome.keep_open)


class TabBar:
    """A tab bar that behaves the way Herdr's does.

    `tab.move` puts the tab BEFORE the tab at the index it is given, and it
    counts the moved tab as still being in the list. So the tab that was at
    index 1 and is moved with `insert_index` 2 lands at index 1 -- it did
    not move at all.

    This is the server, written the way the server does it, so an action
    that computes the wrong index is caught here and not in the session.
    """

    def __init__(self, labels):
        self.tabs = [
            {"tab_id": "t%d" % index, "label": label}
            for index, label in enumerate(labels)
        ]
        self.calls = []

    def call(self, method, params=None):
        params = params or {}
        self.calls.append((method, params))
        if method == "tab.list":
            return {"tabs": list(self.tabs)}
        if method == "tab.move":
            self.move(params["tab_id"], params["insert_index"])
            return {"tabs": list(self.tabs)}
        raise AssertionError("unexpected method %s" % method)

    def move(self, tab_id, insert_index):
        index = next(
            i for i, tab in enumerate(self.tabs) if tab["tab_id"] == tab_id
        )
        tab = self.tabs[index]
        # A copy goes in at the index -- the index counts the moved tab as
        # still being there -- and the old one goes away.
        self.tabs.insert(insert_index, dict(tab))
        old = index + 1 if insert_index <= index else index
        del self.tabs[old]

    @property
    def labels(self):
        return [tab["label"] for tab in self.tabs]


class TestMoveTab(unittest.TestCase):
    """Moving a tab one place along the tab bar.

    The action used to say "moved" without moving anything: it sent the
    index of the tab the moved one should end up at, but `tab.move` wants
    the place BEFORE the tab at that index.
    """

    def _move(self, labels, tab_id, action):
        bar = TabBar(labels)
        ctx = actions_mod.Context(bar, workspace_id="w1", tab_id=tab_id)
        outcome = actions_mod.EXECUTORS[action](ctx)
        return bar, outcome

    def test_move_tab_next_moves_the_tab_one_place_along(self):
        bar, outcome = self._move(["one", "two", "three"], "t1", "move_tab_next")
        self.assertEqual(outcome.message, "moved")
        self.assertEqual(bar.labels, ["one", "three", "two"])

    def test_move_tab_previous_moves_the_tab_one_place_along(self):
        bar, outcome = self._move(["one", "two", "three"], "t1", "move_tab_previous")
        self.assertEqual(outcome.message, "moved")
        self.assertEqual(bar.labels, ["two", "one", "three"])

    def test_the_first_tab_moves_along_too(self):
        bar, outcome = self._move(["one", "two", "three"], "t0", "move_tab_next")
        self.assertEqual(outcome.message, "moved")
        self.assertEqual(bar.labels, ["two", "one", "three"])

    def test_the_last_tab_cannot_move_along(self):
        bar, outcome = self._move(["one", "two", "three"], "t2", "move_tab_next")
        self.assertEqual(outcome.message, "already at the end")
        self.assertEqual(bar.labels, ["one", "two", "three"])

    def test_the_first_tab_cannot_move_back(self):
        bar, outcome = self._move(["one", "two", "three"], "t0", "move_tab_previous")
        self.assertEqual(outcome.message, "already at the end")
        self.assertEqual(bar.labels, ["one", "two", "three"])


class TestClientRetry(unittest.TestCase):
    """A dropped connection is stale, not dead. The call must be retried."""

    class FakeSocket:
        """A socket whose first sendall fails with EPIPE, like a dropped one."""

        def __init__(self, fail):
            self.fail = fail
            self.pending = b""

        def sendall(self, data):
            if self.fail:
                self.fail = False
                raise OSError(32, "Broken pipe")
            self.pending = data

        def recv(self, size):
            request = json.loads(self.pending.decode().strip())
            body = {"id": request["id"], "result": {"type": "pong"}}
            return json.dumps(body).encode("utf-8") + b"\n"

        def settimeout(self, value):
            pass

        def close(self):
            pass

    def test_a_broken_pipe_is_retried_on_a_fresh_connection(self):
        from managr import client as client_mod

        sockets = [self.FakeSocket(True), self.FakeSocket(False)]
        client = client_mod.HerdrClient(socket_path="/nonexistent")

        def connect():
            sock = sockets.pop(0)
            client._sock = sock
            return sock

        client._connect = connect
        result = client.call("ping")
        self.assertEqual(result["type"], "pong")
        self.assertEqual(len(sockets), 0, "the second connection was never used")

    def test_a_permanently_dead_socket_raises(self):
        from managr import client as client_mod

        class Dead(client_mod.HerdrClient):
            def _connect(self):
                raise client_mod.HerdrUnavailable("no socket")

        with self.assertRaises(client_mod.HerdrUnavailable):
            Dead().call("ping")


class TestOutcomeDescribing(unittest.TestCase):
    """Herdr says "nothing happened" with `changed: false`; we must say so."""

    def test_a_no_op_is_reported_and_keeps_the_overlay_open(self):
        outcome = actions_mod._describe(
            {"type": "pane_focus_direction", "focus": {"changed": False, "reason": "no_neighbor"}},
            "focus left",
        )
        self.assertEqual(outcome.message, "nothing that way")
        self.assertTrue(outcome.keep_open)

    def test_a_success_reports_the_ok_message(self):
        outcome = actions_mod._describe({"changed": True}, "focus left")
        self.assertEqual(outcome.message, "focus left")
        self.assertFalse(outcome.keep_open)

    def test_a_nested_result_is_found(self):
        outcome = actions_mod._describe({"focus": {"changed": False, "reason": "single_pane"}}, "ok")
        self.assertEqual(outcome.message, "only one pane")

    def test_an_unknown_reason_still_says_something(self):
        outcome = actions_mod._describe({"changed": False, "reason": "brand_new_reason"}, "ok")
        self.assertEqual(outcome.message, "brand_new_reason")

    def test_a_result_without_changed_is_treated_as_success(self):
        outcome = actions_mod._describe({"type": "tab_focused"}, "switched")
        self.assertEqual(outcome.message, "switched")


class TestKeyDecoding(unittest.TestCase):
    def test_printable(self):
        key = overlay_mod.decode(b"p")
        self.assertEqual(key.name, "char")
        self.assertEqual(key.char, "p")

    def test_control_keys(self):
        self.assertEqual(overlay_mod.decode(b"\x1b").name, "esc")
        self.assertEqual(overlay_mod.decode(b"\x7f").name, "backspace")
        self.assertEqual(overlay_mod.decode(b"\x0d").name, "enter")
        self.assertEqual(overlay_mod.decode(b"\x03").name, "ctrl-c")

    def test_arrows(self):
        self.assertEqual(overlay_mod.decode(b"\x1b[A").name, "up")
        self.assertEqual(overlay_mod.decode(b"\x1b[D").name, "left")


class TestKeyReader(unittest.TestCase):
    """A read from the terminal can hold more than one key, or half of one."""

    def _reader(self):
        # The reader peeks at the fd to tell a lone ESC from the start of an
        # escape sequence. A pipe stands in for the terminal.
        read_fd, write_fd = os.pipe()
        self.addCleanup(os.close, read_fd)
        self.addCleanup(os.close, write_fd)
        return overlay_mod.KeyReader(read_fd), write_fd

    def test_two_keys_in_one_read_are_two_keys(self):
        reader, _ = self._reader()
        reader.feed(b"pv")
        keys = list(reader.keys())
        self.assertEqual([(k.name, k.char) for k in keys], [("char", "p"), ("char", "v")])

    def test_a_character_split_across_reads_survives(self):
        reader, _ = self._reader()
        reader.feed(b"\xc3")
        self.assertEqual(list(reader.keys()), [], "a half character must wait")
        reader.feed(b"\xa9")
        keys = list(reader.keys())
        self.assertEqual([(k.name, k.char) for k in keys], [("char", "\u00e9")])

    def test_an_escape_sequence_split_across_reads_is_one_key(self):
        reader, write_fd = self._reader()
        reader.feed(b"\x1b[")
        # The rest of the sequence is on its way, so the reader waits.
        os.write(write_fd, b"A")
        self.assertEqual(list(reader.keys()), [], "a half sequence must wait")
        reader.feed(b"A")
        keys = list(reader.keys())
        self.assertEqual([k.name for k in keys], ["up"])

    def test_a_lone_escape_is_the_escape_key(self):
        reader, _ = self._reader()
        reader.feed(b"\x1b")
        keys = list(reader.keys())
        self.assertEqual([k.name for k in keys], ["esc"])

    def test_a_control_key_is_not_text(self):
        reader, _ = self._reader()
        reader.feed(b"\x03")
        keys = list(reader.keys())
        self.assertEqual([k.name for k in keys], ["ctrl-c"])

    def test_a_control_key_does_not_leave_the_decoder_dirty(self):
        # A control key in the middle of a half-decoded character used to
        # leave the decoder holding the bytes of that character. The *next*
        # keystroke then decoded to garbage and was dropped without a word:
        # the user pressed a key and nothing happened.
        reader, _ = self._reader()
        reader.feed(b"\xc3")  # the first half of "é"
        self.assertEqual(list(reader.keys()), [], "a half character must wait")
        reader.feed(b"\r")  # Enter interrupts it
        self.assertEqual([k.name for k in reader.keys()], ["enter"])
        reader.feed(b"x")
        self.assertEqual(
            [(k.name, k.char) for k in reader.keys()],
            [("char", "x")],
            "the keystroke after a control key was swallowed",
        )

    def test_an_escape_after_a_half_character_is_not_swallowed(self):
        # b"\xc3\x1b" is a half character followed by Escape. The Escape has
        # to survive: the half character is garbage, but the key is not.
        reader, _ = self._reader()
        reader.feed(b"\xc3\x1b")
        self.assertEqual([k.name for k in reader.keys()], ["esc"])

    def test_an_escape_sequence_after_a_half_character_still_decodes(self):
        reader, _ = self._reader()
        reader.feed(b"\xc3\x1b[A")
        self.assertEqual([k.name for k in reader.keys()], ["up"])

    def test_a_csi_sequence_ends_on_a_final_byte(self):
        # ESC "[" and then an ordinary character is not a sequence: the "["
        # was typed by the user. The character after it must not be eaten.
        reader, _ = self._reader()
        reader.feed(b"\x1b[x")
        keys = list(reader.keys())
        self.assertEqual(
            [(k.name, k.char) for k in keys],
            [("esc", ""), ("char", "["), ("char", "x")],
            "the ordinary character after ESC [ was swallowed",
        )

    def test_a_known_csi_sequence_is_still_one_key(self):
        reader, _ = self._reader()
        reader.feed(b"\x1b[1;5A")
        self.assertEqual([k.name for k in reader.keys()], ["up"])

    def test_the_delete_key_sequence_is_one_key(self):
        reader, _ = self._reader()
        reader.feed(b"\x1b[3~")
        self.assertEqual([k.name for k in reader.keys()], ["delete"])


class TestOverlayState(unittest.TestCase):
    """The overlay's navigation and dispatch, without a terminal."""

    class FakeClient:
        def __init__(self):
            self.calls = []

        def call(self, method, params=None):
            self.calls.append((method, params))
            return {"pane": {"pane_id": "w1:p1", "tab_id": "w1:t1", "workspace_id": "w1"}}

    def _overlay(self):
        tree = tree_mod.build(keymap=fake_keymap(), labels={})
        client = self.FakeClient()
        context = actions_mod.Context(client, workspace_id="w1", tab_id="w1:t1", pane_id="w1:p1")
        return overlay_mod.Overlay(tree, context), client

    def test_entering_a_layer(self):
        overlay, _ = self._overlay()
        overlay.handle(overlay_mod.Key("char", "p"))
        self.assertEqual(overlay.layer_id, "panes")
        self.assertEqual(overlay.stack, ["panes"])

    def test_escape_goes_up_a_level_then_closes(self):
        overlay, _ = self._overlay()
        overlay.handle(overlay_mod.Key("char", "p"))
        overlay.handle(overlay_mod.Key("esc"))
        self.assertEqual(overlay.layer_id, "root")
        self.assertTrue(overlay.running)
        overlay.handle(overlay_mod.Key("esc"))
        self.assertFalse(overlay.running)

    def test_executable_action_runs_and_closes(self):
        overlay, client = self._overlay()
        overlay.handle(overlay_mod.Key("char", "p"))
        overlay.handle(overlay_mod.Key("char", "v"))
        self.assertIn(("pane.split", {"direction": "right", "target_pane_id": "w1:p1", "focus": True}), client.calls)
        self.assertFalse(overlay.running)

    def test_an_item_marked_keep_leaves_the_overlay_open(self):
        # `keep = true` is for repeatable verbs: the nav layer has to stay
        # open so focus can be moved twice.
        overlay, client = self._overlay()
        overlay.handle(overlay_mod.Key("char", "n"))
        overlay.handle(overlay_mod.Key("char", "h"))
        self.assertIn(("pane.focus_direction", {"direction": "left"}), client.calls)
        self.assertTrue(overlay.running, "keep = true did not keep the overlay open")

    def test_an_item_not_marked_keep_still_closes(self):
        overlay, _ = self._overlay()
        overlay.handle(overlay_mod.Key("char", "p"))
        overlay.handle(overlay_mod.Key("char", "v"))
        self.assertFalse(overlay.running)

    def test_a_duplicate_key_in_a_layer_is_reported(self):
        # Two items with the same key: one of them silently shadowed the
        # other. The overlay must say so.
        layer = tree_mod.Layer(id="dupes", title="dupes")
        layer.items = [
            tree_mod.Item(key="q", desc="one", action="zoom", layer="dupes"),
            tree_mod.Item(key="q", desc="two", action="close_pane", layer="dupes"),
        ]
        tree = tree_mod.Tree(layers={"dupes": layer}, root=tree_mod.Layer(id="root", title="layers"))
        overlay = overlay_mod.Overlay(tree, actions_mod.Context(self.FakeClient()))
        overlay.layer_id = "dupes"
        self.assertEqual(overlay._duplicate_keys(), ["q"])
        overlay.handle(overlay_mod.Key("char", "q"))
        self.assertIn("duplicate key", overlay.message)
        self.assertEqual(overlay.message_kind, "err")

    def test_the_reference_key_is_the_configured_one(self):
        # The "?" in the overlay used to be hardcoded, so a user who changed
        # `reference_key` in layers.toml could not reach the reference view.
        handle = tempfile.NamedTemporaryFile("w", suffix=".toml", delete=False)
        with handle:
            handle.write('reference_key = "y"\n')
        self.addCleanup(os.unlink, handle.name)
        tree = tree_mod.build(keymap=fake_keymap(), labels={}, definition_path=handle.name)
        self.assertEqual(tree.reference_key, "y")
        overlay = overlay_mod.Overlay(tree, actions_mod.Context(self.FakeClient()))
        overlay.handle(overlay_mod.Key("char", "?"))
        self.assertEqual(overlay.layer_id, "root")
        overlay.handle(overlay_mod.Key("char", "y"))
        self.assertEqual(overlay.layer_id, "reference")

    def test_hint_action_does_not_run(self):
        overlay, client = self._overlay()
        overlay.handle(overlay_mod.Key("char", "s"))
        overlay.handle(overlay_mod.Key("char", "S"))  # settings: a hint
        self.assertEqual(client.calls, [])
        self.assertTrue(overlay.running)
        self.assertIn("no socket method", overlay.message)

    def test_unknown_key_reports(self):
        overlay, _ = self._overlay()
        overlay.handle(overlay_mod.Key("char", "Q"))
        self.assertIn("no binding", overlay.message)

    def test_input_mode_collects_then_runs(self):
        overlay, client = self._overlay()
        overlay.handle(overlay_mod.Key("char", "t"))
        overlay.handle(overlay_mod.Key("char", "r"))  # rename tab -> needs input
        self.assertEqual(overlay.input_prompt, "tab name")
        for char in "abc":
            overlay.handle(overlay_mod.Key("char", char))
        overlay.handle(overlay_mod.Key("enter"))
        self.assertIn(("tab.rename", {"tab_id": "w1:t1", "label": "abc"}), client.calls)

    def test_input_can_be_cancelled(self):
        overlay, client = self._overlay()
        overlay.handle(overlay_mod.Key("char", "t"))
        overlay.handle(overlay_mod.Key("char", "r"))
        overlay.handle(overlay_mod.Key("esc"))
        self.assertEqual(client.calls, [])
        self.assertIsNone(overlay.input_prompt)


class TestOverlayScrolling(unittest.TestCase):
    """A layer that does not fit on the screen must still be reachable.

    The "all bindings" view has more rows than a 70% popup has lines, and
    the rows that fall off the bottom were simply gone. The overlay scrolls
    now, and the scroll keys are the ones nothing else wants: `j` and `k`
    are actions in the panes and navigation layers.
    """

    def _tree(self, count=40):
        """A layer with `count` rows, so it cannot fit on the screen."""
        layer = tree_mod.Layer(id="long", title="long", hint="a long layer")
        layer.items = [
            tree_mod.Item(
                key="",
                desc="item %02d" % index,
                action="zoom",
                layer="long",
                chord="C-a %d" % index,
            )
            for index in range(count)
        ]
        root = tree_mod.Layer(id="root", title="layers")
        return tree_mod.Tree(layers={"long": layer}, root=root)

    def _overlay(self, rows=12, columns=80, count=40):
        overlay = overlay_mod.Overlay(self._tree(count), actions_mod.Context(None))
        overlay._size = lambda: (columns, rows)
        overlay.layer_id = "long"
        overlay.stack = ["long"]
        return overlay

    def _screen(self, overlay):
        """What is on screen right now, without the escape sequences."""
        out = io.StringIO()
        overlay.out = out
        overlay.render()
        return ANSI.sub("", out.getvalue())

    def test_the_last_item_can_be_reached(self):
        overlay = self._overlay()
        for _ in range(200):
            overlay.handle(overlay_mod.Key("down"))
        screen = self._screen(overlay)
        self.assertIn("item 39", screen)
        self.assertNotIn("item 00", screen)

    def test_scrolling_does_not_go_past_the_end(self):
        overlay = self._overlay()
        for _ in range(200):
            overlay.handle(overlay_mod.Key("down"))
        at_the_end = overlay.scroll
        overlay.handle(overlay_mod.Key("down"))
        self.assertEqual(overlay.scroll, at_the_end)
        # The window is the last screenful: the row above it is not on
        # screen, or the last item would be pushed off the bottom.
        screen = self._screen(overlay)
        self.assertIn("item 39", screen)
        self.assertNotIn("item 28", screen)

    def test_scrolling_back_stops_at_the_top(self):
        overlay = self._overlay()
        for _ in range(200):
            overlay.handle(overlay_mod.Key("down"))
        for _ in range(200):
            overlay.handle(overlay_mod.Key("up"))
        self.assertEqual(overlay.scroll, 0)
        self.assertIn("item 00", self._screen(overlay))

    def test_the_page_keys_move_a_page(self):
        overlay = self._overlay()
        overlay.handle(overlay_mod.Key("pagedown"))
        self.assertGreater(overlay.scroll, 1)
        moved = overlay.scroll
        overlay.handle(overlay_mod.Key("pageup"))
        self.assertLess(overlay.scroll, moved)

    def test_the_scroll_keys_work_while_the_filter_is_open(self):
        # A filter that matches thirty rows is still thirty rows: the user
        # has to be able to reach the last one without accepting the
        # filter first. The scroll keys used to be swallowed while the
        # filter was open.
        overlay = self._overlay()
        overlay.handle(overlay_mod.Key("char", "/"))
        for char in "item":
            overlay.handle(overlay_mod.Key("char", char))
        self.assertTrue(overlay.filtering)
        for _ in range(60):
            overlay.handle(overlay_mod.Key("down"))
        screen = self._screen(overlay)
        self.assertIn("item 39", screen)
        self.assertNotIn("item 00", screen)
        # The scroll keys are not text: the query is untouched.
        self.assertEqual(overlay.filter_draft, "item")

    def test_the_page_keys_work_while_the_filter_is_open(self):
        overlay = self._overlay()
        overlay.handle(overlay_mod.Key("char", "/"))
        for char in "item":
            overlay.handle(overlay_mod.Key("char", char))
        overlay.handle(overlay_mod.Key("pagedown"))
        self.assertGreater(overlay.scroll, 1)
        moved = overlay.scroll
        overlay.handle(overlay_mod.Key("pageup"))
        self.assertLess(overlay.scroll, moved)
        self.assertEqual(overlay.filter_draft, "item")

    def test_the_arrow_keys_do_not_run_an_action(self):
        # The arrows are the scroll keys because `j` and `k` are actions.
        # An arrow must never run one.
        calls = []

        class Fake:
            def call(self, method, params=None):
                calls.append(method)
                return {}

        tree = tree_mod.build(keymap=fake_keymap(), labels={})
        overlay = overlay_mod.Overlay(tree, actions_mod.Context(Fake()))
        overlay.handle(overlay_mod.Key("char", "p"))  # the panes layer
        overlay.handle(overlay_mod.Key("down"))
        overlay.handle(overlay_mod.Key("up"))
        self.assertEqual(calls, [])
        self.assertTrue(overlay.running)

    def test_j_and_k_are_still_actions(self):
        # The scroll keys must not have been taken from the layers.
        tree = tree_mod.build(keymap=fake_keymap(), labels={})
        calls = []

        class Fake:
            def call(self, method, params=None):
                calls.append(method)
                return {"changed": True}

        overlay = overlay_mod.Overlay(tree, actions_mod.Context(Fake()))
        overlay.handle(overlay_mod.Key("char", "p"))  # the panes layer
        overlay.handle(overlay_mod.Key("char", "j"))
        self.assertIn("pane.resize", calls)
        self.assertEqual(overlay.scroll, 0)

    def test_the_scroll_resets_when_another_layer_is_entered(self):
        overlay = self._overlay()
        for _ in range(10):
            overlay.handle(overlay_mod.Key("down"))
        self.assertGreater(overlay.scroll, 0)
        overlay.handle(overlay_mod.Key("esc"))
        self.assertEqual(overlay.scroll, 0)

    def test_the_rows_fit_on_the_screen(self):
        # The row budget is computed from the chrome, not guessed: a row
        # drawn over the bottom border would push that border off the
        # screen.
        for rows in (8, 10, 12, 16, 24):
            overlay = self._overlay(rows=rows)
            screen = self._screen(overlay)
            lines = [line for line in screen.split("\r\n") if line]
            self.assertLessEqual(
                len(lines), rows, "%d rows were drawn on a %d row screen" % (len(lines), rows)
            )
            # The bottom border is still on the screen.
            self.assertTrue(
                lines[-1].startswith("╰"),
                "the bottom border was pushed off the screen at %d rows: %r"
                % (rows, lines[-1]),
            )

    def test_the_rows_use_the_space_they_have(self):
        # The row budget is computed from the chrome, so the overlay uses
        # the rows it has. A magic constant leaves rows on the screen.
        overlay = self._overlay(rows=12)
        screen = self._screen(overlay)
        shown = len(re.findall(r"item \d\d", screen))
        # 12 rows: the frame's two borders, the layer's hint, and the row
        # the cursor's last newline needs -- a frame that fills the screen
        # scrolls by one, and the breadcrumb goes off the top.
        self.assertEqual(shown, 8, "the overlay left rows on the screen")

    def test_the_position_is_shown_when_the_layer_does_not_fit(self):
        overlay = self._overlay()
        self.assertIn("1/40", self._screen(overlay))
        overlay.handle(overlay_mod.Key("down"))
        self.assertIn("2/40", self._screen(overlay))

    def test_no_position_is_shown_when_everything_fits(self):
        # A "40/40" on a list that fits says nothing, and a position that
        # is not there is worse than none.
        overlay = self._overlay(rows=60)
        self.assertNotIn("/40", self._screen(overlay))

    def test_the_position_never_makes_a_line_too_wide(self):
        for columns in (20, 30, 40, 53, 80):
            overlay = self._overlay(columns=columns)
            screen = self._screen(overlay)
            for line in screen.split("\r\n"):
                plain = ANSI.sub("", line)
                if not plain:
                    continue
                self.assertLessEqual(
                    len(plain),
                    columns - 1,
                    "a line is %d columns wide in a %d column terminal: %r"
                    % (len(plain), columns, line),
                )


class TestOverlayFrameHeight(unittest.TestCase):
    """The frame has to fit on the screen, breadcrumb and all.

    Every line the overlay writes ends with a newline, so the last newline
    leaves the cursor on the row *after* the last line. A frame that fills
    the screen exactly therefore scrolls by one row, and the top border --
    the breadcrumb -- goes off the top of the screen.
    """

    def _overlay(self, rows=8, columns=80, count=40):
        layer = tree_mod.Layer(id="long", title="long", hint="a long layer")
        layer.items = [
            tree_mod.Item(
                key="",
                desc="item %02d" % index,
                action="zoom",
                layer="long",
                chord="C-a %d" % index,
            )
            for index in range(count)
        ]
        root = tree_mod.Layer(id="root", title="layers")
        tree = tree_mod.Tree(layers={"long": layer}, root=root)
        overlay = overlay_mod.Overlay(tree, actions_mod.Context(None))
        overlay._size = lambda: (columns, rows)
        overlay.layer_id = "long"
        overlay.stack = ["long"]
        return overlay

    def _lines(self, overlay):
        out = io.StringIO()
        overlay.out = out
        overlay.render()
        return [line for line in ANSI.sub("", out.getvalue()).split("\r\n") if line]

    def test_the_frame_leaves_a_row_for_the_cursor(self):
        # The last newline puts the cursor on the row after the bottom
        # border. A frame that fills the screen scrolls by one row, so the
        # frame has to leave that row free.
        for rows in (6, 8, 10, 12, 24):
            overlay = self._overlay(rows=rows)
            lines = self._lines(overlay)
            self.assertLessEqual(
                len(lines),
                rows - 1,
                "%d lines on a %d row screen: the last newline scrolls the frame"
                % (len(lines), rows),
            )

    def test_the_breadcrumb_is_on_the_screen(self):
        # The breadcrumb is in the top border, so it is the first thing
        # that goes when the frame is a row too tall.
        overlay = self._overlay(rows=8)
        visible = after_scroll(self._lines(overlay), 8)
        self.assertTrue(
            visible and visible[0].startswith("╭"),
            "the top border scrolled off the screen: %r" % (visible[:1],),
        )
        self.assertIn("layers", visible[0], "the breadcrumb is not in the top border")

    def test_the_bottom_border_is_on_the_screen(self):
        overlay = self._overlay(rows=8)
        visible = after_scroll(self._lines(overlay), 8)
        self.assertTrue(
            visible and visible[-1].startswith("╰"),
            "the bottom border is not on the screen: %r" % (visible[-1:],),
        )


class TestOverlayFiltering(unittest.TestCase):
    """The filter narrows the rows. It does not change what a key does."""

    class FakeClient:
        def __init__(self):
            self.calls = []

        def call(self, method, params=None):
            self.calls.append((method, params))
            return {"pane": {"pane_id": "w1:p1", "tab_id": "w1:t1", "workspace_id": "w1"}}

    def _overlay(self):
        tree = tree_mod.build(keymap=fake_keymap(), labels={})
        context = actions_mod.Context(
            self.FakeClient(),
            workspace_id="w1",
            tab_id="w1:t1",
            pane_id="w1:p1",
        )
        return overlay_mod.Overlay(tree, context)

    def _screen(self, overlay):
        out = io.StringIO()
        overlay.out = out
        overlay.render()
        return ANSI.sub("", out.getvalue())

    def _type(self, overlay, text):
        for char in text:
            overlay.handle(overlay_mod.Key("char", char))

    def test_the_filter_narrows_the_list(self):
        overlay = self._overlay()
        overlay.handle(overlay_mod.Key("char", "p"))  # the panes layer
        overlay.handle(overlay_mod.Key("char", "/"))
        self.assertTrue(overlay.filtering)
        self._type(overlay, "split")
        screen = self._screen(overlay)
        self.assertIn("split right", screen)
        self.assertNotIn("close pane", screen)

    def test_the_filter_matches_the_key(self):
        # "resize left" is bound to "h", and "h" is in no description.
        overlay = self._overlay()
        overlay.handle(overlay_mod.Key("char", "p"))
        overlay.handle(overlay_mod.Key("char", "/"))
        self._type(overlay, "h")
        screen = self._screen(overlay)
        self.assertIn("resize left", screen)
        self.assertNotIn("close pane", screen)

    def test_the_filter_matches_the_chord(self):
        overlay = self._overlay()
        overlay.handle(overlay_mod.Key("char", "p"))
        overlay.handle(overlay_mod.Key("char", "/"))
        self._type(overlay, "C-a v")  # the chord of "split right"
        screen = self._screen(overlay)
        self.assertIn("split right", screen)
        self.assertNotIn("close pane", screen)

    def test_the_query_is_visible_while_it_is_typed(self):
        overlay = self._overlay()
        overlay.handle(overlay_mod.Key("char", "p"))
        overlay.handle(overlay_mod.Key("char", "/"))
        self._type(overlay, "split")
        # The query is on a line of its own, behind the filter's key.
        self.assertIn("/ split", self._screen(overlay))

    def test_the_applied_filter_is_still_visible(self):
        overlay = self._overlay()
        overlay.handle(overlay_mod.Key("char", "p"))
        overlay.handle(overlay_mod.Key("char", "/"))
        self._type(overlay, "split")
        overlay.handle(overlay_mod.Key("enter"))
        self.assertFalse(overlay.filtering)
        self.assertIn("/ split", self._screen(overlay))

    def test_enter_keeps_the_filter(self):
        overlay = self._overlay()
        overlay.handle(overlay_mod.Key("char", "p"))
        overlay.handle(overlay_mod.Key("char", "/"))
        self._type(overlay, "split")
        overlay.handle(overlay_mod.Key("enter"))
        self.assertEqual(overlay.filter_query, "split")
        screen = self._screen(overlay)
        self.assertIn("split right", screen)
        self.assertNotIn("close pane", screen)

    def test_escape_clears_the_filter(self):
        overlay = self._overlay()
        overlay.handle(overlay_mod.Key("char", "p"))
        overlay.handle(overlay_mod.Key("char", "/"))
        self._type(overlay, "split")
        overlay.handle(overlay_mod.Key("esc"))
        self.assertFalse(overlay.filtering)
        self.assertEqual(overlay.filter_query, "")
        self.assertEqual(overlay.filter_draft, "")
        screen = self._screen(overlay)
        self.assertIn("split right", screen)
        self.assertIn("close pane", screen)

    def test_escape_clears_an_applied_filter(self):
        # Escape undoes the filter before it undoes anything else: the
        # list goes back to every row, and the layer stays.
        overlay = self._overlay()
        overlay.handle(overlay_mod.Key("char", "p"))  # the panes layer
        overlay.handle(overlay_mod.Key("char", "/"))
        self._type(overlay, "split")
        overlay.handle(overlay_mod.Key("enter"))
        self.assertEqual(overlay.filter_query, "split")
        overlay.handle(overlay_mod.Key("esc"))
        self.assertEqual(overlay.filter_query, "")
        self.assertEqual(overlay.layer_id, "panes", "escape left the layer instead")
        screen = self._screen(overlay)
        self.assertIn("split right", screen)
        self.assertIn("close pane", screen)

    def test_escape_goes_up_a_level_once_the_filter_is_cleared(self):
        # The filter is what Escape undoes first; a second Escape goes up
        # a level, and a third closes the overlay at the root.
        overlay = self._overlay()
        overlay.handle(overlay_mod.Key("char", "p"))
        overlay.handle(overlay_mod.Key("char", "/"))
        self._type(overlay, "split")
        overlay.handle(overlay_mod.Key("enter"))
        overlay.handle(overlay_mod.Key("esc"))  # clears the filter
        self.assertEqual(overlay.layer_id, "panes")
        overlay.handle(overlay_mod.Key("esc"))  # now it goes up
        self.assertEqual(overlay.layer_id, "root")
        self.assertTrue(overlay.running)
        overlay.handle(overlay_mod.Key("esc"))  # and closes at the root
        self.assertFalse(overlay.running)

    def test_escape_clears_the_filter_when_it_is_opened_for_editing(self):
        # Opening the filter and escaping out of it clears it too.
        overlay = self._overlay()
        overlay.handle(overlay_mod.Key("char", "p"))
        overlay.handle(overlay_mod.Key("char", "/"))
        self._type(overlay, "split")
        overlay.handle(overlay_mod.Key("enter"))
        overlay.handle(overlay_mod.Key("char", "/"))
        self.assertTrue(overlay.filtering)
        overlay.handle(overlay_mod.Key("esc"))
        self.assertEqual(overlay.filter_query, "")
        self.assertIn("close pane", self._screen(overlay))

    def test_the_footer_says_that_escape_clears_the_filter(self):
        # A hint that says "esc close" when Escape clears the filter is a
        # hint that lies, and the footer is the only place the user reads.
        overlay = self._overlay()
        overlay.handle(overlay_mod.Key("char", "p"))
        overlay.handle(overlay_mod.Key("char", "/"))
        self._type(overlay, "split")
        screen = self._screen(overlay)
        self.assertIn("esc clear", screen)
        self.assertNotIn("esc close", screen)
        overlay.handle(overlay_mod.Key("enter"))
        screen = self._screen(overlay)
        self.assertIn("esc clear filter", screen)

    def test_escape_in_filter_mode_clears_it_and_does_not_go_up_a_level(self):
        overlay = self._overlay()
        overlay.handle(overlay_mod.Key("char", "p"))
        overlay.handle(overlay_mod.Key("char", "/"))
        self._type(overlay, "split")
        overlay.handle(overlay_mod.Key("esc"))
        self.assertEqual(overlay.layer_id, "panes", "escape left the layer instead")
        self.assertEqual(overlay.filter_query, "")

    def test_the_filter_key_opens_the_current_query_for_editing(self):
        overlay = self._overlay()
        overlay.handle(overlay_mod.Key("char", "p"))
        overlay.handle(overlay_mod.Key("char", "/"))
        self._type(overlay, "split")
        overlay.handle(overlay_mod.Key("enter"))
        overlay.handle(overlay_mod.Key("char", "/"))
        self.assertTrue(overlay.filtering)
        self.assertEqual(overlay.filter_draft, "split")

    def test_backspace_deletes_in_filter_mode(self):
        overlay = self._overlay()
        overlay.handle(overlay_mod.Key("char", "p"))
        overlay.handle(overlay_mod.Key("char", "/"))
        self._type(overlay, "split")
        overlay.handle(overlay_mod.Key("backspace"))
        self.assertEqual(overlay.filter_draft, "spli")

    def test_backspace_outside_filter_mode_goes_up_a_level(self):
        # The old meaning of backspace is not changed by the filter.
        overlay = self._overlay()
        overlay.handle(overlay_mod.Key("char", "p"))
        overlay.handle(overlay_mod.Key("backspace"))
        self.assertEqual(overlay.layer_id, "root")

    def test_a_filter_with_no_matches_says_so(self):
        overlay = self._overlay()
        overlay.handle(overlay_mod.Key("char", "p"))
        overlay.handle(overlay_mod.Key("char", "/"))
        self._type(overlay, "zzzz")
        screen = self._screen(overlay)
        self.assertIn("no match for zzzz", screen)

    def test_a_group_heading_disappears_when_its_group_has_no_matches(self):
        # The reference view is grouped: a heading whose group has nothing
        # in it is a heading to nowhere.
        overlay = self._overlay()
        overlay.handle(overlay_mod.Key("char", "?"))  # the reference view
        overlay.handle(overlay_mod.Key("char", "/"))
        self._type(overlay, "focus pane left")
        screen = self._screen(overlay)
        self.assertIn("focus pane left", screen)
        self.assertNotIn("tabs", screen)
        self.assertNotIn("session", screen)

    def test_a_group_heading_is_shown_once(self):
        # The heading heads the group: it is not repeated for every row of
        # the group.
        #
        # The sample has to hold more than one row of the group. With a
        # single row the assertion holds for any layout at all -- a heading
        # repeated per row would look exactly like a heading shown once --
        # so the test could not fail.
        km = keymap_mod.Keymap(
            prefix="C-a",
            bindings={
                "focus_pane_left": keymap_mod.Binding("focus_pane_left", "focus left", "h"),
                "focus_pane_right": keymap_mod.Binding("focus_pane_right", "focus right", "l"),
                "focus_pane_up": keymap_mod.Binding("focus_pane_up", "focus up", "k"),
                "focus_pane_down": keymap_mod.Binding("focus_pane_down", "focus down", "j"),
            },
        )
        tree = tree_mod.build(keymap=km, labels={})
        overlay = overlay_mod.Overlay(tree, actions_mod.Context(self.FakeClient()))
        overlay.handle(overlay_mod.Key("char", "?"))  # the reference view
        overlay.handle(overlay_mod.Key("char", "/"))
        self._type(overlay, "focus")
        screen = self._screen(overlay)
        rows = [line for line in screen.split("\r\n") if "focus pane" in line]
        self.assertGreaterEqual(
            len(rows),
            2,
            "the sample holds %d row(s), so it cannot fail" % len(rows),
        )
        # Four rows match, so the heading has to be there once.
        self.assertEqual(screen.count("panes"), 1, "the heading was shown twice")

    def test_the_filter_resets_when_another_layer_is_entered(self):
        # Backspace goes up a level, and the filter does not follow: it
        # belongs to the layer it was typed in.
        overlay = self._overlay()
        overlay.handle(overlay_mod.Key("char", "p"))
        overlay.handle(overlay_mod.Key("char", "/"))
        self._type(overlay, "split")
        overlay.handle(overlay_mod.Key("enter"))
        overlay.handle(overlay_mod.Key("backspace"))  # up to the root
        self.assertEqual(overlay.layer_id, "root")
        self.assertEqual(overlay.filter_query, "")
        # The root shows all of its rows again.
        screen = self._screen(overlay)
        self.assertIn("navigation", screen)
        self.assertIn("all bindings", screen)

    def test_the_filter_does_not_change_what_a_key_does(self):
        # The filter hides rows. The key still runs the same action.
        overlay = self._overlay()
        client = overlay.context.client
        overlay.handle(overlay_mod.Key("char", "p"))
        overlay.handle(overlay_mod.Key("char", "/"))
        self._type(overlay, "zoom")
        overlay.handle(overlay_mod.Key("enter"))
        overlay.handle(overlay_mod.Key("char", "v"))
        self.assertIn(
            ("pane.split", {"direction": "right", "target_pane_id": "w1:p1", "focus": True}),
            client.calls,
        )

    def test_a_printable_character_is_part_of_the_query_in_filter_mode(self):
        # `p`, `v` and `x` are actions in the layers. While the filter is
        # open they are only letters.
        overlay = self._overlay()
        client = overlay.context.client
        overlay.handle(overlay_mod.Key("char", "p"))
        overlay.handle(overlay_mod.Key("char", "/"))
        self._type(overlay, "vx")
        self.assertEqual(overlay.filter_draft, "vx")
        self.assertEqual(client.calls, [])

    def test_the_filter_key_is_free_in_the_shipped_layers(self):
        # The layers bind letters and "?" (the reference key). "/" has to
        # be free, or the filter would take an action's key.
        path = os.path.join(ROOT, "src", "managr", "data", "layers.toml")
        with open(path, "r", encoding="utf-8") as handle:
            data = tomlite.loads(handle.read())
        for entry in data.get("item", []) or []:
            self.assertNotEqual(entry.get("key"), "/", entry)
        for entry in data.get("layer", []) or []:
            self.assertNotEqual(entry.get("key"), "/", entry)

    def test_a_layer_that_binds_the_filter_key_keeps_it(self):
        # The filter only takes the key when nothing else wants it.
        layer = tree_mod.Layer(id="mine", title="mine")
        layer.items = [
            tree_mod.Item(key="/", desc="slash action", action="zoom", layer="mine"),
        ]
        tree = tree_mod.Tree(
            layers={"mine": layer}, root=tree_mod.Layer(id="root", title="layers")
        )
        overlay = overlay_mod.Overlay(tree, actions_mod.Context(self.FakeClient()))
        overlay.layer_id = "mine"
        overlay.handle(overlay_mod.Key("char", "/"))
        self.assertFalse(overlay.filtering, "the layer's own key was taken")


class TestOverlayInputState(unittest.TestCase):
    """The frame must not report a position for a list that is not shown.

    The badge in the bottom border says where we are in the list. While a
    value is being typed there is no list on the screen, so the badge of
    the layer we came from is a lie.
    """

    def _overlay(self, rows=12, columns=80):
        layer = tree_mod.Layer(id="long", title="long")
        layer.items = [
            tree_mod.Item(
                key="",
                desc="item %02d" % index,
                action="zoom",
                layer="long",
            )
            for index in range(39)
        ]
        layer.items.append(
            tree_mod.Item(
                key="n", desc="rename", action="rename_tab", layer="long"
            )
        )
        root = tree_mod.Layer(id="root", title="layers")
        tree = tree_mod.Tree(layers={"long": layer}, root=root)
        overlay = overlay_mod.Overlay(tree, actions_mod.Context(None))
        overlay._size = lambda: (columns, rows)
        overlay.layer_id = "long"
        overlay.stack = ["long"]
        return overlay

    def _screen(self, overlay):
        out = io.StringIO()
        overlay.out = out
        overlay.render()
        return ANSI.sub("", out.getvalue())

    def test_the_position_is_not_shown_while_a_value_is_being_typed(self):
        overlay = self._overlay()
        self.assertIn("1/40", self._screen(overlay))
        overlay.handle(overlay_mod.Key("char", "n"))
        self.assertIsNotNone(overlay.input_prompt, "the item did not ask for a value")
        screen = self._screen(overlay)
        self.assertNotIn(
            "/40", screen, "the frame still shows the position of the list it hid"
        )

    def test_the_position_comes_back_when_the_input_is_cancelled(self):
        overlay = self._overlay()
        overlay.handle(overlay_mod.Key("char", "n"))
        overlay.handle(overlay_mod.Key("esc"))
        self.assertIsNone(overlay.input_prompt)
        self.assertIn("1/40", self._screen(overlay))


class TestOverlayLayout(unittest.TestCase):
    """The rows line up, and no line is as wide as the terminal.

    The width is measured here rather than with the renderer's own
    function: a test that asks the code under test how wide its output is
    proves nothing.
    """

    ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")

    def display_width(self, text):
        plain = self.ANSI.sub("", text)
        return sum(
            2 if unicodedata.east_asian_width(char) in ("W", "F") else 1
            for char in plain
        )

    def _layer(self, items):
        layer = tree_mod.Layer(id="demo", title="demo")
        layer.items = items
        return layer

    def _render(self, layer, columns=80):
        tree = tree_mod.Tree(
            layers={"demo": layer}, root=tree_mod.Layer(id="root", title="layers")
        )
        return self._render_tree(tree, "demo", columns=columns)

    def _render_tree(self, tree, layer_id, columns=80):
        """Render one layer of a real tree, at a given terminal width."""
        overlay = overlay_mod.Overlay(tree, actions_mod.Context(None))
        buffer = io.StringIO()
        overlay.out = buffer
        overlay.layer_id = layer_id
        overlay.stack = [layer_id]
        overlay._size = lambda: (columns, 24)
        overlay.render()
        return buffer.getvalue()

    def _render_input(self, prompt, buffer, columns=80):
        """Render the input state, at a given terminal width."""
        tree = tree_mod.Tree(
            layers={}, root=tree_mod.Layer(id="root", title="layers")
        )
        overlay = overlay_mod.Overlay(tree, actions_mod.Context(None))
        out = io.StringIO()
        overlay.out = out
        overlay._size = lambda: (columns, 24)
        overlay.input_prompt = prompt
        overlay.input_buffer = buffer
        overlay.render()
        return out.getvalue()

    def _column_of(self, out, needle):
        """The column `needle` starts at, in the row that holds it."""
        for line in out.split("\r\n"):
            plain = self.ANSI.sub("", line)
            if needle in plain:
                return plain.index(needle)
        return -1

    def _row_with(self, out, needle):
        """The row that holds `needle`, or "" when there is none."""
        for line in out.split("\r\n"):
            plain = self.ANSI.sub("", line)
            if needle in plain:
                return plain
        return ""

    def _items(self):
        return [
            tree_mod.Item(
                key="h",
                desc="focus pane left",
                action="focus_pane_left",
                layer="demo",
                chord="C-a h",
            ),
            tree_mod.Item(
                key="k",
                desc="focus pane up",
                action="focus_pane_up",
                layer="demo",
                chord="C-a k",
            ),
        ]

    def test_the_chord_column_lines_up(self):
        # The description used to be as long as it liked, so the chord
        # started in a different column on every row.
        out = self._render(self._layer(self._items()))
        self.assertEqual(self._column_of(out, "C-a h"), self._column_of(out, "C-a k"))

    def test_no_line_is_wider_than_the_terminal(self):
        # A line exactly as wide as the terminal wraps the cursor, and the
        # next line starts in column 0 of a new row: the whole layout
        # shifts. Every line has to be one column short of the terminal.
        for columns in (20, 30, 45, 80):
            out = self._render(self._layer(self._items()), columns=columns)
            for line in out.split("\r\n"):
                self.assertLessEqual(
                    self.display_width(line),
                    columns - 1,
                    "a line is %d columns wide in a %d column terminal: %r"
                    % (self.display_width(line), columns, line),
                )

    def test_a_narrow_terminal_drops_the_chord_and_keeps_the_description(self):
        out = self._render(self._layer(self._items()), columns=24)
        plain = self.ANSI.sub("", out)
        self.assertNotIn("C-a h", plain)
        self.assertIn("focus pane left", plain)

    def test_a_hint_row_keeps_its_note(self):
        items = self._items()
        items.append(
            tree_mod.Item(
                key="?",
                desc="help",
                action="help",
                layer="demo",
                chord="C-a ?",
            )
        )
        out = self._render(self._layer(items))
        self.assertIn("press this in herdr", out)
        # The note is a column of its own, so it does not move the chords.
        self.assertEqual(self._column_of(out, "C-a h"), self._column_of(out, "C-a ?"))
        self.assertGreater(
            self._column_of(out, "press this in herdr"),
            self._column_of(out, "C-a ?"),
        )

    def test_every_line_ends_with_crlf(self):
        out = self._render(self._layer(self._items()))
        self.assertNotIn("\n", out.replace("\r\n", ""))

    def test_a_hint_row_never_shows_a_note_without_its_chord(self):
        # The note says "press this in herdr", so it means nothing without
        # the chord to press: it is a dependent column and goes before the
        # chord does. Between 51 and 57 columns the session layer used to
        # render "Herdr's own keybind panel  press this in herdr" with no
        # chord at all, and that window includes the default: the popup
        # asks for 70% of the terminal, so an 80 column terminal gives a
        # 53 column pty.
        tree = tree_mod.build(keymap=fake_keymap(), labels={})
        for columns in range(51, 60):
            out = self._render_tree(tree, "session", columns=columns)
            row = self._row_with(out, "Herdr's own keybind panel")
            self.assertTrue(row, "the hint row did not render at %d columns" % columns)
            self.assertIn(
                "C-a ?",
                row,
                "the chord was dropped at %d columns: %r" % (columns, row),
            )
            # Whatever else is dropped, a note never outlives its chord.
            for line in self.ANSI.sub("", out).split("\r\n"):
                if "press this in herdr" in line:
                    self.assertIn(
                        "C-a",
                        line,
                        "a note without its chord at %d columns: %r" % (columns, line),
                    )

    def test_the_input_line_fits_the_budget(self):
        # The prompt and the buffer are two halves of one line, so they
        # share one budget. Truncated separately they give a line up to
        # 2*width+1 columns wide, which wraps the cursor and lands the
        # next line in the middle of a row. A long branch name in
        # `rename_pane` or `new_worktree` reaches it.
        for columns in (24, 56, 80):
            out = self._render_input("space name", "b" * 60, columns=columns)
            for line in out.split("\r\n"):
                self.assertLessEqual(
                    self.display_width(line),
                    columns - 1,
                    "a line is %d columns wide in a %d column terminal: %r"
                    % (self.display_width(line), columns, line),
                )

    def test_control_characters_in_user_data_cannot_break_the_terminal(self):
        # `layers.toml` can be hand-edited, so a label can hold anything.
        # A stray escape makes the terminal wait for a final byte and
        # swallow the rest of the line; a vertical tab or a form feed
        # moves the cursor.
        items = [
            tree_mod.Item(
                key="v",
                desc="split \x1b right \x0b here \x0c now",
                action="split_vertical",
                layer="demo",
                chord="C-a v",
            ),
        ]
        out = self._render(self._layer(items))
        # No bare newline: the overlay writes CRLF and nothing else.
        self.assertNotIn("\n", out.replace("\r\n", ""))
        # No raw escape: what is left after the known sequences is text.
        plain = self.ANSI.sub("", out)
        self.assertNotIn("\x1b", plain, "a raw escape reached the terminal")
        self.assertNotIn("\x0b", out)
        self.assertNotIn("\x0c", out)


class TestLiveCheckStatus(unittest.TestCase):
    """The live check's verdict has to reach its exit status.

    A warning that never changes the exit status is not a check: the
    regression it was written for would pass unnoticed.
    """

    def _live_check(self):
        sys.path.insert(0, os.path.join(ROOT, "scripts"))
        import live_check

        return live_check

    def test_a_silent_no_op_fails(self):
        live_check = self._live_check()
        ok, message = live_check.check_no_op_feedback("p1", "p1", "")
        self.assertFalse(ok, message)

    def test_a_reported_no_op_passes(self):
        live_check = self._live_check()
        ok, _ = live_check.check_no_op_feedback("p1", "p1", "nothing that way")
        self.assertTrue(ok)

    def test_a_moved_focus_passes(self):
        live_check = self._live_check()
        ok, _ = live_check.check_no_op_feedback("p1", "p2", "focus left")
        self.assertTrue(ok)


# -- the navigatr ---------------------------------------------------------

# A session, as the four list calls report it.
#
# The shapes are the ones the live server sends: the workspaces carry
# `focused`, the tabs carry their workspace, the panes carry their tab and
# the agents carry their pane. `w1:t2` is a tab with no panes, and `w1:p2`
# is a pane with no agent.
SESSION = {
    "workspace.list": {
        "workspaces": [
            {
                "workspace_id": "w1",
                "label": "dev",
                "focused": True,
                "pane_count": 2,
                "tab_count": 2,
            },
            {
                "workspace_id": "w2",
                "label": "ops",
                "focused": False,
                "pane_count": 1,
                "tab_count": 1,
            },
        ]
    },
    "tab.list": {
        "tabs": [
            {
                "tab_id": "w1:t1",
                "label": "code",
                "focused": True,
                "workspace_id": "w1",
                "pane_count": 2,
            },
            {
                "tab_id": "w1:t2",
                "label": "logs",
                "focused": False,
                "workspace_id": "w1",
                "pane_count": 0,
            },
            {
                "tab_id": "w2:t1",
                "label": "main",
                "focused": False,
                "workspace_id": "w2",
                "pane_count": 1,
            },
        ]
    },
    "pane.list": {
        "panes": [
            {
                "pane_id": "w1:p1",
                "focused": True,
                "workspace_id": "w1",
                "tab_id": "w1:t1",
                "agent": "opencode",
                "agent_status": "idle",
                "cwd": "/tmp/dev",
                "terminal_title_stripped": "OC | dev",
            },
            {
                "pane_id": "w1:p2",
                "focused": False,
                "workspace_id": "w1",
                "tab_id": "w1:t1",
                "cwd": "/tmp/dev",
                "terminal_title_stripped": "zsh",
            },
            {
                "pane_id": "w2:p1",
                "focused": False,
                "workspace_id": "w2",
                "tab_id": "w2:t1",
                "cwd": "/tmp/ops",
                "terminal_title_stripped": "bash",
            },
        ]
    },
    "agent.list": {
        "agents": [
            {
                "agent": "opencode",
                "agent_status": "idle",
                "pane_id": "w1:p1",
                "terminal_title_stripped": "OC | dev",
            }
        ]
    },
}


# A session whose own counts disagree with its lists. Herdr reports
# `tab_count` and `pane_count`, but they are a snapshot of a session that
# can change between two of the four list calls -- so the counts in the
# rows come from the lists, and a stale count must never be shown.
STALE_COUNTS_SESSION = {
    "workspace.list": {
        "workspaces": [
            {
                "workspace_id": "w1",
                "label": "dev",
                "focused": True,
                # The server says three tabs and five panes. The lists say
                # two tabs and two panes.
                "tab_count": 3,
                "pane_count": 5,
            },
            {
                "workspace_id": "w2",
                "label": "ops",
                "focused": False,
                "tab_count": 0,
                "pane_count": 0,
            },
        ]
    },
    "tab.list": {
        "tabs": [
            {
                "tab_id": "w1:t1",
                "label": "code",
                "focused": True,
                "workspace_id": "w1",
                # Seven panes, according to the server. There are two.
                "pane_count": 7,
            },
            {
                "tab_id": "w1:t2",
                "label": "logs",
                "focused": False,
                "workspace_id": "w1",
                "pane_count": 0,
            },
        ]
    },
    "pane.list": {
        "panes": [
            {
                "pane_id": "w1:p1",
                "focused": True,
                "workspace_id": "w1",
                "tab_id": "w1:t1",
                "terminal_title_stripped": "one",
            },
            {
                "pane_id": "w1:p2",
                "focused": False,
                "workspace_id": "w1",
                "tab_id": "w1:t1",
                "terminal_title_stripped": "two",
            },
        ]
    },
    "agent.list": {"agents": []},
}


# A pane that says it is in one space and names a tab of another. A pane
# belongs to the tab it names, wherever that tab lives: it is drawn under
# the space of that tab, and not again as a loose pane of the space it
# claims.
PANE_IN_TWO_SPACES = {
    "workspace.list": {
        "workspaces": [
            {
                "workspace_id": "w1",
                "label": "dev",
                "focused": True,
                "tab_count": 1,
                "pane_count": 0,
            },
            {
                "workspace_id": "w2",
                "label": "ops",
                "focused": False,
                "tab_count": 1,
                "pane_count": 1,
            },
        ]
    },
    "tab.list": {
        "tabs": [
            {"tab_id": "w1:t1", "label": "code", "focused": True, "workspace_id": "w1"},
            {"tab_id": "w2:t1", "label": "main", "focused": False, "workspace_id": "w2"},
        ]
    },
    "pane.list": {
        "panes": [
            {
                "pane_id": "w2:p1",
                "focused": False,
                # It says it is in w1...
                "workspace_id": "w1",
                # ...and it names a tab of w2.
                "tab_id": "w2:t1",
                "terminal_title_stripped": "stray",
            },
        ]
    },
    "agent.list": {"agents": []},
}


# A session with an agent in every state Herdr reports, so the attention
# order can be checked: the agents list is sorted by how urgent the agent
# is, and the urgent ones are at the top.
AGENTS_SESSION = {
    "workspace.list": {
        "workspaces": [
            {"workspace_id": "w1", "label": "dev", "focused": True},
            {"workspace_id": "w2", "label": "ops", "focused": False},
        ]
    },
    "tab.list": {
        "tabs": [
            {"tab_id": "w1:t1", "label": "code", "focused": True, "workspace_id": "w1"},
            {"tab_id": "w2:t1", "label": "main", "focused": False, "workspace_id": "w2"},
        ]
    },
    "pane.list": {
        "panes": [
            {"pane_id": "w1:p1", "focused": True, "workspace_id": "w1", "tab_id": "w1:t1"},
            {"pane_id": "w2:p1", "focused": False, "workspace_id": "w2", "tab_id": "w2:t1"},
        ]
    },
    "agent.list": {
        "agents": [
            {
                "agent": "idle-one",
                "agent_status": "idle",
                "pane_id": "w1:p1",
                "workspace_id": "w1",
                "tab_id": "w1:t1",
                "focused": True,
            },
            {
                "agent": "working-one",
                "agent_status": "working",
                "pane_id": "w2:p1",
                "workspace_id": "w2",
                "tab_id": "w2:t1",
                # The title of the terminal the agent runs in. It is part of
                # the agent's own text, so the filter finds the agent by it.
                "terminal_title_stripped": "vim notes.md",
            },
            {
                "agent": "blocked-one",
                "agent_status": "blocked",
                "pane_id": "w1:p1",
                "workspace_id": "w1",
                "tab_id": "w1:t1",
            },
            {
                "agent": "done-one",
                "agent_status": "done",
                "pane_id": "w1:p1",
                "workspace_id": "w1",
                "tab_id": "w1:t1",
            },
            {
                "agent": "quiet-one",
                "agent_status": "unknown",
                "pane_id": "w1:p1",
                "workspace_id": "w1",
                "tab_id": "w1:t1",
            },
        ]
    },
}


# Three agents in the same state, in places whose labels are in a
# different order than the list itself: the sort is by the labels, so the
# list does not shuffle itself between two renders of the same session.
SAME_STATE_SESSION = {
    "workspace.list": {
        "workspaces": [
            {"workspace_id": "w2", "label": "dev", "focused": False},
            {"workspace_id": "w1", "label": "aaa", "focused": False},
        ]
    },
    "tab.list": {
        "tabs": [
            {"tab_id": "w2:t2", "label": "zeta", "focused": False, "workspace_id": "w2"},
            {"tab_id": "w2:t1", "label": "alpha", "focused": False, "workspace_id": "w2"},
        ]
    },
    "pane.list": {"panes": []},
    "agent.list": {
        "agents": [
            {
                "agent": "in-zeta",
                "agent_status": "idle",
                "pane_id": "w2:p2",
                "workspace_id": "w2",
                "tab_id": "w2:t2",
            },
            {
                "agent": "in-alpha",
                "agent_status": "idle",
                "pane_id": "w2:p1",
                "workspace_id": "w2",
                "tab_id": "w2:t1",
            },
            {
                "agent": "in-aaa",
                "agent_status": "idle",
                "pane_id": "w1:p1",
                "workspace_id": "w1",
                "tab_id": "w1:t1",
            },
        ]
    },
}


class FakeSession:
    """A client with a session in it, and a record of every call."""

    def __init__(self, session=None, error=None):
        self.session = SESSION if session is None else session
        self.error = error
        self.calls = []

    def call(self, method, params=None):
        self.calls.append((method, params))
        if self.error is not None:
            raise self.error
        return self.session.get(method, {})


class NavigatrTestCase(unittest.TestCase):
    """The navigatr: the tree, the cursor and the jumps."""

    def _overlay(self, session=None, error=None, rows=24, columns=80):
        tree = tree_mod.build(keymap=fake_keymap(), labels={})
        client = FakeSession(session, error)
        context = actions_mod.Context(
            client, workspace_id="w1", tab_id="w1:t1", pane_id="w1:p1"
        )
        overlay = overlay_mod.Overlay(tree, context)
        overlay._size = lambda: (columns, rows)
        return overlay, client

    def _overlay_without_icons(self, session=None, rows=24, columns=80):
        """An overlay with `icons = false`: no Nerd Font glyphs."""
        tree = tree_mod.build(keymap=fake_keymap(), labels={})
        client = FakeSession(session)
        context = actions_mod.Context(
            client, workspace_id="w1", tab_id="w1:t1", pane_id="w1:p1"
        )
        palette = visuals_mod.Palette(visuals_mod.parse_colour("#5fd7ff"))
        visuals = visuals_mod.Visuals(
            {"icons": False, "frame": True}, visuals_mod.load_icons(), palette
        )
        overlay = overlay_mod.Overlay(tree, context, visuals=visuals)
        overlay._size = lambda: (columns, rows)
        return overlay, client

    def _overlay_without_status_glyphs(self, session=None, rows=24, columns=80):
        """An overlay whose icons name no glyph for the agent statuses.

        A user's `icons.toml` may leave a status without a glyph. The row
        must not end in the separator on its own, and the frame must not
        be there to hide it: the frame pads the row, and the padding
        would swallow the two spaces at the end of it.
        """
        tree = tree_mod.build(keymap=fake_keymap(), labels={})
        client = FakeSession(session)
        context = actions_mod.Context(
            client, workspace_id="w1", tab_id="w1:t1", pane_id="w1:p1"
        )
        palette = visuals_mod.Palette(visuals_mod.parse_colour("#5fd7ff"))
        icons = visuals_mod.load_icons()
        icons["status"] = {}
        visuals = visuals_mod.Visuals(
            {"icons": True, "frame": False}, icons, palette
        )
        overlay = overlay_mod.Overlay(tree, context, visuals=visuals)
        overlay._size = lambda: (columns, rows)
        return overlay, client

    def _open(self, overlay):
        overlay.handle(overlay_mod.Key("char", "j"))
        return overlay.navigatr

    def _open_expanded(self, overlay):
        """Open the navigatr with every space unfolded.

        The navigatr opens folded -- one row per space, and the rows under
        it hidden -- so a test about the shape of the tree has to unfold it
        first. `L` unfolds every space.
        """
        overlay.handle(overlay_mod.Key("char", "j"))
        overlay.handle(overlay_mod.Key("char", "L"))
        return overlay.navigatr

    def _open_agents(self, overlay):
        overlay.handle(overlay_mod.Key("char", "a"))
        return overlay.navigatr

    def _screen(self, overlay):
        out = io.StringIO()
        overlay.out = out
        overlay.render()
        return ANSI.sub("", out.getvalue())

    def _raw(self, overlay):
        """What the overlay wrote, escape sequences and all."""
        out = io.StringIO()
        overlay.out = out
        overlay.render()
        return out.getvalue()

    def _lines(self, overlay):
        return [line for line in self._screen(overlay).split("\r\n") if line]

    def _row(self, overlay, index):
        """The label of the row at `index` in the tree."""
        return overlay.navigatr.rows[index].label


class TestNavigatrTree(NavigatrTestCase):
    """The tree is built from the live session."""

    def test_the_tree_is_spaces_tabs_panes(self):
        overlay, _ = self._overlay()
        rows = self._open(overlay).rows
        self.assertEqual(
            [(row.kind, row.label) for row in rows],
            [
                ("workspace", "dev"),
                ("tab", "code"),
                ("pane", "OC | dev"),
                ("pane", "zsh"),
                ("tab", "logs"),
                ("workspace", "ops"),
                ("tab", "main"),
                ("pane", "bash"),
            ],
        )

    def test_the_agent_is_folded_into_the_row_of_its_pane(self):
        # The agent is not a row of its own any more: it is folded into
        # the pane that hosts it. The agent's name is the pane row's
        # description and the agent's status is the pane row's status.
        overlay, _ = self._overlay()
        rows = self._open(overlay).rows
        self.assertEqual(
            [row.label for row in rows if row.kind == "agent"],
            [],
            "the agent is a row of its own again",
        )
        pane = next(
            row for row in rows if row.kind == "pane" and row.label == "OC | dev"
        )
        self.assertEqual(pane.desc, "opencode")
        self.assertEqual(pane.agent, "opencode")
        self.assertEqual(pane.status, "idle")

    def test_a_pane_that_names_a_tab_of_another_space_is_drawn_once(self):
        # A pane belongs to the tab it names, wherever that tab lives. A
        # pane that says it is in space A and names a tab of space B is
        # drawn under space B, with that tab -- and not again as a loose
        # pane of space A. Drawing it twice put the same destination in
        # the tree twice and counted it in two spaces.
        overlay, _ = self._overlay(PANE_IN_TWO_SPACES)
        rows = self._open(overlay).rows
        panes = [row for row in rows if row.kind == "pane"]
        self.assertEqual(len(panes), 1, "the pane is drawn twice: %r" % panes)
        self.assertEqual(
            [row.label for row in rows],
            ["dev", "code", "ops", "main", "stray"],
        )
        # And the space it claims does not count it.
        space = next(row for row in rows if row.kind == "workspace" and row.label == "dev")
        self.assertEqual(space.desc, "1 tab · 0 panes · 0 agents")

    def test_a_pane_with_no_agent_has_no_status_and_no_description(self):
        overlay, _ = self._overlay()
        rows = self._open(overlay).rows
        pane = next(row for row in rows if row.kind == "pane" and row.label == "zsh")
        self.assertEqual(pane.desc, "")
        self.assertEqual(pane.agent, "")
        self.assertEqual(pane.status, "")

    def test_a_tab_with_no_panes_does_not_break_the_tree(self):
        overlay, _ = self._overlay()
        rows = self._open(overlay).rows
        self.assertIn("logs", [row.label for row in rows])
        # The tab is still there, and the space after it still is.
        self.assertEqual(rows[-1].label, "bash")

    def test_a_workspace_with_no_tabs_does_not_break_the_tree(self):
        session = {
            "workspace.list": {
                "workspaces": [
                    {"workspace_id": "w9", "label": "empty", "focused": False},
                ]
            },
            "tab.list": {"tabs": []},
            "pane.list": {"panes": []},
            "agent.list": {"agents": []},
        }
        overlay, _ = self._overlay(session)
        rows = self._open(overlay).rows
        self.assertEqual(
            [(row.kind, row.label) for row in rows], [("workspace", "empty")]
        )
        # And it renders, rather than showing an empty box.
        self.assertIn("empty", self._screen(overlay))

    def test_the_current_workspace_tab_and_pane_are_marked(self):
        overlay, _ = self._overlay()
        rows = self._open(overlay).rows
        current = [row.label for row in rows if row.current]
        self.assertEqual(current, ["dev", "code", "OC | dev"])

    def test_the_current_ones_are_marked_on_the_screen(self):
        overlay, _ = self._overlay()
        self._open(overlay)
        screen = self._screen(overlay)
        self.assertIn("→", screen, "nothing is marked as current")

    def test_the_agent_status_is_shown(self):
        overlay, _ = self._overlay()
        self._open(overlay)
        screen = self._screen(overlay)
        # The vocabulary of icons.toml: idle is a filled circle, and a
        # Nerd Font glyph is one column wide.
        self.assertIn("●", screen)

    def test_no_workspaces_says_so_plainly(self):
        session = {
            "workspace.list": {"workspaces": []},
            "tab.list": {"tabs": []},
            "pane.list": {"panes": []},
            "agent.list": {"agents": []},
        }
        overlay, _ = self._overlay(session)
        self._open(overlay)
        self.assertIn("no workspaces", self._screen(overlay))

    def test_the_four_list_calls_are_the_ones_that_are_made(self):
        overlay, client = self._overlay()
        self._open(overlay)
        methods = [call[0] for call in client.calls]
        self.assertEqual(
            methods,
            ["workspace.list", "tab.list", "pane.list", "agent.list"],
        )


class TestNavigatrDescriptions(NavigatrTestCase):
    """Every row says what is inside it.

    A row that says nothing is a row the user cannot read: the counts are
    what turns a list of names into a map of the session.
    """

    def _row(self, overlay, kind, label):
        for row in overlay.navigatr.rows:
            if row.kind == kind and row.label == label:
                return row
        self.fail("no %s row %r" % (kind, label))

    def test_a_space_says_how_many_tabs_panes_and_agents_it_holds(self):
        overlay, _ = self._overlay()
        self._open(overlay)
        space = self._row(overlay, "workspace", "dev")
        # Two tabs, two panes and the one agent that is in them.
        self.assertEqual(space.desc, "2 tabs · 2 panes · 1 agent")

    def test_a_tab_says_how_many_panes_and_agents_it_holds(self):
        overlay, _ = self._overlay()
        self._open(overlay)
        tab = self._row(overlay, "tab", "code")
        self.assertEqual(tab.desc, "2 panes · 1 agent")

    def test_a_pane_says_which_agent_is_in_it(self):
        overlay, _ = self._overlay()
        self._open(overlay)
        pane = self._row(overlay, "pane", "OC | dev")
        self.assertEqual(pane.desc, "opencode")

    def test_the_counts_are_pluralised(self):
        overlay, _ = self._overlay()
        self._open(overlay)
        # One tab, one pane and no agents at all.
        space = self._row(overlay, "workspace", "ops")
        self.assertEqual(space.desc, "1 tab · 1 pane · 0 agents")

    def test_a_zero_is_still_said(self):
        # A count of zero is still said: a row that says nothing is a row
        # the user cannot read.
        overlay, _ = self._overlay()
        self._open(overlay)
        tab = self._row(overlay, "tab", "logs")
        self.assertEqual(tab.desc, "0 panes · 0 agents")

    def test_the_counts_agree_with_the_rows_that_are_drawn(self):
        # The counts come from the four lists, not from Herdr's own
        # `tab_count` and `pane_count`: the rows drawn and the counts have
        # to agree.
        overlay, _ = self._overlay()
        rows = self._open(overlay).rows
        for index, row in enumerate(rows):
            if row.kind != "workspace":
                continue
            # The rows under the space: everything up to the next space.
            under = []
            for other in rows[index + 1:]:
                if other.kind == "workspace":
                    break
                under.append(other)
            tabs = [r for r in under if r.kind == "tab"]
            panes = [r for r in under if r.kind == "pane"]
            agents = [r for r in under if r.kind == "pane" and r.agent]
            self.assertIn(
                "%d tab%s" % (len(tabs), "" if len(tabs) == 1 else "s"), row.desc
            )
            self.assertIn(
                "%d pane%s" % (len(panes), "" if len(panes) == 1 else "s"), row.desc
            )
            self.assertIn(
                "%d agent%s" % (len(agents), "" if len(agents) == 1 else "s"),
                row.desc,
            )

    def test_the_counts_come_from_the_lists_not_from_the_server(self):
        # Herdr reports a `tab_count` and a `pane_count` for a space, but
        # they are a snapshot of a session that can change between two of
        # the four list calls. The rows drawn and the counts have to
        # agree, so the counts are computed from the lists.
        overlay, _ = self._overlay(STALE_COUNTS_SESSION)
        self._open(overlay)
        space = self._row(overlay, "workspace", "dev")
        self.assertEqual(space.desc, "2 tabs · 2 panes · 0 agents")
        self.assertNotIn("5 panes", space.desc, "the server's count was shown")
        self.assertNotIn("3 tabs", space.desc, "the server's count was shown")
        # And the tab says what the lists say, not what the server says.
        tab = self._row(overlay, "tab", "code")
        self.assertEqual(tab.desc, "2 panes · 0 agents")
        self.assertNotIn("7 panes", tab.desc, "the server's count was shown")

    def test_the_status_of_a_space_and_of_a_tab_is_the_aggregate(self):
        # Herdr reports an aggregate status for a space and for a tab, and
        # that is the row's status.
        session = {
            "workspace.list": {
                "workspaces": [
                    {
                        "workspace_id": "w1",
                        "label": "dev",
                        "focused": True,
                        "agent_status": "working",
                    }
                ]
            },
            "tab.list": {
                "tabs": [
                    {
                        "tab_id": "w1:t1",
                        "label": "code",
                        "focused": True,
                        "workspace_id": "w1",
                        "agent_status": "blocked",
                    }
                ]
            },
            "pane.list": {"panes": []},
            "agent.list": {"agents": []},
        }
        overlay, _ = self._overlay(session)
        self._open(overlay)
        self.assertEqual(self._row(overlay, "workspace", "dev").status, "working")
        self.assertEqual(self._row(overlay, "tab", "code").status, "blocked")
        screen = self._screen(overlay)
        # The working glyph and the blocked one, and not the idle one.
        self.assertIn("◐", screen)
        self.assertIn("◉", screen)

    def test_a_pane_row_is_found_by_its_agent_and_by_its_title(self):
        # The agent is on the pane's row, so the filter has to find the
        # pane by either of them.
        overlay, _ = self._overlay()
        self._open(overlay)
        pane = self._row(overlay, "pane", "OC | dev")
        self.assertIn("opencode", pane.search)
        self.assertIn("OC | dev", pane.search)


# A session that changes between the four list calls: a tab is closed, a
# space is closed. The four calls are four round trips, so the tree has to
# cope with what it is given.
ONE_SPACE_TWO_TABS_AND_A_LOOSE_PANE = {
    "workspace.list": {
        "workspaces": [
            {"workspace_id": "w1", "label": "dev", "focused": True},
        ]
    },
    "tab.list": {
        "tabs": [
            {"tab_id": "w1:t1", "label": "code", "focused": True, "workspace_id": "w1"},
            {"tab_id": "w1:t2", "label": "logs", "focused": False, "workspace_id": "w1"},
        ]
    },
    "pane.list": {
        "panes": [
            # The tab of this pane was closed between the two calls.
            {"pane_id": "w1:p9", "focused": False, "workspace_id": "w1", "tab_id": "w1:t9"},
        ]
    },
    "agent.list": {"agents": []},
}


class TestNavigatrTreeEdges(NavigatrTestCase):
    """The tree when the session changes between the four list calls."""

    def test_only_one_row_is_the_last_child(self):
        # The children of a space are the tabs *and* the loose panes, so
        # exactly one of them is the last child. Deciding it over the tabs
        # alone drew two last branches under one space.
        overlay, _ = self._overlay(ONE_SPACE_TWO_TABS_AND_A_LOOSE_PANE)
        rows = self._open(overlay).rows
        children = [row for row in rows if row.depth == 1]
        self.assertEqual(
            [row.label for row in children], ["code", "logs", "w1:p9"]
        )
        last = [row.label for row in children if row.is_last]
        self.assertEqual(last, ["w1:p9"], "more than one row is the last child")

    def test_only_one_branch_is_drawn_as_the_last_one(self):
        overlay, _ = self._overlay(ONE_SPACE_TWO_TABS_AND_A_LOOSE_PANE)
        self._open(overlay)
        tee = overlay.visuals.marker("tee") or overlay_mod.DEFAULT_TEE
        elbow = overlay.visuals.marker("elbow") or overlay_mod.DEFAULT_ELBOW
        lines = self._lines(overlay)
        loose = next(line for line in lines if "w1:p9" in line)
        last_tab = next(line for line in lines if "logs" in line)
        # The loose pane is the last child of the space, so it is the one
        # drawn with the last-child marker.
        self.assertIn(elbow, loose)
        self.assertNotIn(tee, loose)
        self.assertIn(tee, last_tab)
        self.assertNotIn(elbow, last_tab)
        # And exactly one row is drawn as the last child.
        self.assertEqual(
            sum(1 for line in lines if elbow in line),
            1,
            "two rows are drawn as the last child",
        )

    def test_a_pane_whose_tab_is_gone_is_still_shown(self):
        # The tab was closed between the two calls. The pane is a child of
        # the space like the tabs are, and it is not dropped: a row that
        # is not shown is a place the user cannot go.
        overlay, _ = self._overlay(ONE_SPACE_TWO_TABS_AND_A_LOOSE_PANE)
        rows = self._open(overlay).rows
        pane = next(row for row in rows if row.label == "w1:p9")
        self.assertEqual(pane.kind, "pane")
        self.assertEqual(pane.depth, 1)
        self.assertEqual(pane.method, "pane.focus")
        self.assertEqual(pane.params, {"pane_id": "w1:p9"})

    def test_a_pane_with_no_tab_at_all_is_still_shown(self):
        session = {
            "workspace.list": {
                "workspaces": [
                    {"workspace_id": "w1", "label": "dev", "focused": True},
                ]
            },
            "tab.list": {"tabs": []},
            "pane.list": {
                "panes": [
                    {"pane_id": "w1:p1", "focused": False, "workspace_id": "w1"},
                ]
            },
            "agent.list": {"agents": []},
        }
        overlay, _ = self._overlay(session)
        rows = self._open(overlay).rows
        pane = next(row for row in rows if row.label == "w1:p1")
        self.assertEqual(pane.kind, "pane")
        self.assertEqual(pane.depth, 1)
        self.assertEqual(pane.params, {"pane_id": "w1:p1"})

    def test_a_tab_whose_space_is_gone_is_still_shown(self):
        # The space was closed between the two calls. The tab is still a
        # place the user can go, so it is not dropped.
        session = {
            "workspace.list": {
                "workspaces": [
                    {"workspace_id": "w1", "label": "dev", "focused": True},
                ]
            },
            "tab.list": {
                "tabs": [
                    {"tab_id": "w1:t1", "label": "code", "focused": True, "workspace_id": "w1"},
                    {"tab_id": "w9:t1", "label": "old", "focused": False, "workspace_id": "w9"},
                ]
            },
            "pane.list": {"panes": []},
            "agent.list": {"agents": []},
        }
        overlay, _ = self._overlay(session)
        rows = self._open(overlay).rows
        tab = next(row for row in rows if row.label == "old")
        self.assertEqual(tab.kind, "tab")
        self.assertEqual(tab.method, "tab.focus")
        self.assertEqual(tab.params, {"tab_id": "w9:t1"})
        # And it is on the screen.
        self.assertIn("old", self._screen(overlay))

    def test_a_tab_whose_space_is_gone_can_be_jumped_to(self):
        session = {
            "workspace.list": {
                "workspaces": [
                    {"workspace_id": "w1", "label": "dev", "focused": True},
                ]
            },
            "tab.list": {
                "tabs": [
                    {"tab_id": "w9:t1", "label": "old", "focused": False, "workspace_id": "w9"},
                ]
            },
            "pane.list": {"panes": []},
            "agent.list": {"agents": []},
        }
        overlay, client = self._overlay(session)
        self._open(overlay)
        overlay.handle(overlay_mod.Key("char", "j"))  # the cursor onto the tab
        overlay.handle(overlay_mod.Key("enter"))
        self.assertIn(("tab.focus", {"tab_id": "w9:t1"}), client.calls)

    def test_a_pane_whose_space_and_tab_are_gone_is_still_shown(self):
        session = {
            "workspace.list": {
                "workspaces": [
                    {"workspace_id": "w1", "label": "dev", "focused": True},
                ]
            },
            "tab.list": {"tabs": []},
            "pane.list": {
                "panes": [
                    {"pane_id": "w8:p1", "focused": False, "workspace_id": "w8"},
                ]
            },
            "agent.list": {"agents": []},
        }
        overlay, _ = self._overlay(session)
        rows = self._open(overlay).rows
        pane = next(row for row in rows if row.label == "w8:p1")
        self.assertEqual(pane.kind, "pane")
        self.assertEqual(pane.method, "pane.focus")


class TestNavigatrDrawing(NavigatrTestCase):
    """How the tree is drawn: the colour, the markers and the glyphs."""

    def _row_on_the_band(self, overlay, text=None):
        """The raw line of the row under the cursor.

        The row under the cursor is the one on the band, so the band is
        what picks it out of the lines. `text`, when it is given, is
        what the row has to hold as well.
        """
        band = overlay.palette.selection()
        for line in self._raw(overlay).split("\r\n"):
            if band not in line:
                continue
            if text is None or text in ANSI.sub("", line):
                return line
        self.fail("the row under the cursor does not hold %r" % text)

    def _assert_the_band_covers_the_row(self, overlay, line, text=None):
        """The band must run all the way to the end of the row.

        A band is a *background*, and the reset that closes a colour
        closes the background too. So the row under the cursor is ONE
        run: the band opens it and the band closes it. A reset inside
        the row ends the band there, and the text after it is drawn in
        the panel's background colour on the terminal's own background --
        invisible -- and what is left is a short stripe behind the first
        two columns of the row, with the rest of the row gone.

        The run is counted, not guessed at: the row has exactly one
        reset in it, the one that closes the band at the end.
        """
        band = overlay.palette.selection()
        start = line.find(band)
        self.assertTrue(start >= 0, "the row under the cursor has no band: %r" % line)
        # The frame's right border is the end of the row. Without a frame
        # the row ends with the line.
        border = line.rfind(overlay_mod.FRAME_VERTICAL)
        end = border if border > start else len(line)
        self.assertEqual(
            line[start:end].count(overlay.palette.reset),
            1,
            "the band is not one run: %r" % line,
        )
        if text is None:
            return
        end = line.find(text, start)
        self.assertTrue(end > start, "the row's text is not on the band: %r" % line)
        self.assertNotIn(
            overlay.palette.reset,
            line[start:end],
            "the band was closed before the row's text: %r" % line,
        )

    def test_the_band_is_not_broken_by_the_kind_glyph(self):
        # The kind glyph is painted inside the row, so it must not close
        # the band. The row under the cursor is a pane that is NOT the
        # current one, so the marker of the current row is not in it:
        # this guard is about the kind glyph alone.
        overlay, _ = self._overlay()
        self._open(overlay)
        rows = overlay.navigatr.rows
        index = next(
            i
            for i, row in enumerate(rows)
            if row.kind == navigatr_mod.PANE and not row.current
        )
        overlay.navigatr.cursor = index
        line = self._row_on_the_band(overlay, "zsh")
        self.assertNotIn(
            overlay_mod.DEFAULT_ARROW,
            ANSI.sub("", line),
            "the row under the cursor is the current one: %r" % line,
        )
        self._assert_the_band_covers_the_row(overlay, line, "zsh")

    def test_the_band_is_not_broken_by_the_current_marker(self):
        # The marker of the current row is painted inside the row too,
        # so it must not close the band either. The row under the cursor
        # is the current one here.
        overlay, _ = self._overlay()
        self._open(overlay)
        line = self._row_on_the_band(overlay, "dev")
        self.assertIn(
            overlay_mod.DEFAULT_ARROW,
            ANSI.sub("", line),
            "the current row is not marked: %r" % line,
        )
        self._assert_the_band_covers_the_row(overlay, line, "dev")

    def test_the_band_is_not_broken_in_the_agents_list(self):
        # The same in the agents list, where the row under the cursor is
        # the focused agent: the marker of the focused agent is drawn
        # inside the row, and a reset after it took the whole row off the
        # band.
        overlay, _ = self._overlay(session=AGENTS_SESSION)
        self._open_agents(overlay)
        rows = overlay.navigatr.rows
        index = next(i for i, row in enumerate(rows) if row.current)
        overlay.navigatr.cursor = index
        line = self._row_on_the_band(overlay, "idle-one")
        self._assert_the_band_covers_the_row(overlay, line, "idle-one")

    def test_the_band_survives_a_row_that_is_cut(self):
        # A row that does not fit is cut, and the cut closes the colour
        # run of the text -- which closes the band with it. The band has
        # to survive the cut: it is closed once, at the end of the row.
        #
        # The name is long on purpose: the row has to be cut in the
        # middle of the name, with the ellipsis after the cut, or the
        # cut is not there to be survived.
        session = {
            "workspace.list": {
                "workspaces": [
                    {
                        "workspace_id": "w1",
                        "label": "a-very-long-space-name-here",
                        "focused": True,
                    }
                ]
            },
            "tab.list": {"tabs": []},
            "pane.list": {"panes": []},
            "agent.list": {"agents": []},
        }
        for columns in (12, 13, 14, 17, 20, 24, 30, 40, 80):
            overlay, _ = self._overlay(session=session, columns=columns)
            self._open(overlay)
            line = self._row_on_the_band(overlay)
            self._assert_the_band_covers_the_row(overlay, line)

    def test_the_cursor_row_does_not_leak_its_colour(self):
        # The row under the cursor is drawn on the band: the accent is
        # the band's background and the row's text is the panel's
        # background colour, the way Herdr's own navigation surface
        # draws it. Light text on a light band is unreadable.
        #
        # What must not happen is the accent running on into the rows
        # below it. Every run the overlay opens is closed, so the row
        # after the band is not in the accent -- which is how the accent
        # once ran on into the whole row.
        overlay, _ = self._overlay()
        self._open(overlay)
        lines = self._raw(overlay).split("\r\n")
        for index, line in enumerate(lines):
            if "dev" not in line:
                continue
            runs = styled_runs(line)
            label = [run for run in runs if "dev" in run[0]]
            self.assertTrue(label, "the row under the cursor was not drawn")
            self.assertTrue(
                label[0][1], "the cursor's row is not highlighted: %r" % line
            )
            cursor = [run for run in runs if ">" in run[0]]
            self.assertTrue(cursor, "the cursor was not drawn")
            self.assertTrue(cursor[0][1], "the cursor is not styled")
            # The band is the accent, and the row's text is the panel's
            # background colour.
            self.assertIn(
                overlay.palette.selection(),
                line,
                "the cursor's row is not on the band: %r" % line,
            )
            self.assertIn(
                overlay.palette.on_selection(),
                style_before(line, "dev"),
                "the cursor's row is not in the background colour: %r" % line,
            )
            # The band is closed: the row after it is not in the accent.
            accent = overlay.palette.accent
            after = style_runs(line)
            row = next(i for i, run in enumerate(after) if "dev" in run[0])
            self.assertTrue(row + 1 < len(after), "the row ends after the name")
            self.assertNotIn(
                accent,
                after[row + 1][1],
                "the band leaked into the row: %r" % line,
            )
            return
        self.fail("the row under the cursor was not drawn")

    def test_the_band_under_the_cursor_row_is_closed(self):
        # The band is the accent, and it is a background: a run that is
        # opened and not closed paints what comes after it as well, so
        # the rows below the cursor would all be on the band.
        overlay, _ = self._overlay()
        self._open(overlay)
        for line in self._raw(overlay).split("\r\n"):
            if "dev" not in line:
                continue
            band = overlay.palette.selection()
            start = line.find(band)
            self.assertTrue(start >= 0, "the cursor row has no band")
            border = line.rfind(overlay_mod.FRAME_VERTICAL)
            self.assertIn(
                overlay.palette.reset,
                line[start:border],
                "the band is not closed before the frame's border: %r" % line,
            )
            return
        self.fail("the row under the cursor was not drawn")

    def test_the_current_row_is_marked_when_the_icons_are_off(self):
        # `icons = false` turns the Nerd Font glyphs off, but the marker
        # that says which row is the current one is not decoration.
        overlay, _ = self._overlay_without_icons()
        self._open(overlay)
        lines = self._lines(overlay)
        marked = [line for line in lines if overlay_mod.DEFAULT_ARROW in line]
        self.assertEqual(len(marked), 3, "the current rows are not marked")
        self.assertTrue(any("dev" in line for line in marked))
        # And a row that is not current is not marked.
        self.assertFalse(any("logs" in line for line in marked))

    def test_no_line_is_wider_than_the_terminal_without_the_icons(self):
        for columns in (20, 30, 40, 53, 80):
            overlay, _ = self._overlay_without_icons(columns=columns)
            self._open(overlay)
            for line in self._screen(overlay).split("\r\n"):
                plain = ANSI.sub("", line)
                if not plain:
                    continue
                self.assertLessEqual(
                    len(plain),
                    columns - 1,
                    "a line is %d columns wide in a %d column terminal: %r"
                    % (len(plain), columns, line),
                )


class TestNavigatrColours(NavigatrTestCase):
    """The colours of the navigatr: the status stands out, the rest is quiet.

    Everything else in the overlay is one accent hue, which is calm but
    flat. The agent's status is the exception: it is the thing the eye
    should go to, so it is coloured by how urgent it is, and everything
    else on the row is quiet so that it stands out.
    """

    def _cursor_to_the_end(self, overlay):
        """Move the cursor to the last row.

        The row under the cursor is drawn on the band, in the panel's
        background colour, so it has none of the colours the other rows
        have. The colours are checked on a row that is not under the
        cursor, and the last row is the one that can be spared.
        """
        for _ in range(50):
            before = overlay.navigatr.cursor
            overlay.handle(overlay_mod.Key("char", "j"))
            if overlay.navigatr.cursor == before:
                return

    def _row_line(self, overlay, text):
        """The raw line of the row whose plain text holds `text`.

        The search is on the text the terminal shows: the overlay's own
        colours sit between the words, so a search on the raw line would
        not find a row whose words are drawn in two different colours.

        The row under the cursor is skipped: it is drawn on the band, in
        the panel's background colour, so it has none of the colours the
        other rows have.
        """
        band = overlay.palette.selection()
        for line in self._raw(overlay).split("\r\n"):
            if text in ANSI.sub("", line) and band not in line:
                return line
        self.fail("no row holds %r" % text)

    def test_the_status_is_coloured_by_severity(self):
        # A blocked agent wants the user now: it is in the error colour.
        # A working one is busy: it is not in the muted colour the quiet
        # ones have, and it is not an alarm either.
        overlay, _ = self._overlay(session=AGENTS_SESSION)
        self._open_agents(overlay)
        self._cursor_to_the_end(overlay)
        palette = overlay.palette
        blocked = self._row_line(overlay, "blocked-one")
        working = self._row_line(overlay, "working-one")
        self.assertEqual(run_with(blocked, "blocked"), palette.err)
        self.assertNotEqual(
            run_with(working, "working"),
            palette.hint,
            "a working agent is drawn in the muted colour",
        )
        self.assertNotEqual(
            run_with(working, "working"),
            palette.err,
            "a working agent is drawn in the error colour",
        )

    def test_the_kind_marker_is_coloured_by_depth(self):
        # The deeper the row, the brighter the marker: the leaves are the
        # things you can jump to, so they stand out and the structure
        # recedes.
        overlay, _ = self._overlay()
        self._open(overlay)
        self._cursor_to_the_end(overlay)
        palette = overlay.palette
        space = self._row_line(overlay, "dev")
        tab = self._row_line(overlay, "code")
        pane = self._row_line(overlay, "zsh")
        self.assertEqual(
            run_with(space, overlay.visuals.group_icon("workspace")),
            palette.hint,
            "the space's marker is not muted",
        )
        self.assertEqual(
            run_with(tab, overlay.visuals.group_icon("tab")),
            palette.chord,
            "the tab's marker is not the dimmed accent",
        )
        self.assertEqual(
            run_with(pane, overlay.visuals.group_icon("pane")),
            palette.accent,
            "the pane's marker is not the accent",
        )

    def test_the_description_is_muted_and_the_number_is_dimmed(self):
        # The description is context, not content: it is drawn in the
        # muted colour. The number is in the dimmed accent.
        overlay, _ = self._overlay()
        self._open(overlay)
        self._cursor_to_the_end(overlay)
        palette = overlay.palette
        space = self._row_line(overlay, "dev")
        self.assertEqual(
            run_with(space, "2 tabs"),
            palette.hint,
            "the description is not muted",
        )
        self.assertEqual(
            run_exactly(space, "1 "),
            palette.chord,
            "the number is not the dimmed accent",
        )

    def test_the_three_tiers_are_told_apart_by_their_colour(self):
        # The space is the section title and is in the accent, the tab is
        # the group heading and is muted, and the pane is the item the
        # user jumps to and is in the plain colour. Without that the
        # three of them are a wall of labels in the same colour.
        overlay, _ = self._overlay()
        self._open(overlay)
        self._cursor_to_the_end(overlay)
        palette = overlay.palette
        space = self._row_line(overlay, "dev")
        tab = self._row_line(overlay, "code")
        pane = self._row_line(overlay, "OC | dev")
        self.assertEqual(
            style_before(space, "dev"),
            palette.accent + palette.bold,
            "the space is not in the accent",
        )
        self.assertEqual(
            style_before(tab, "code"),
            palette.hint,
            "the tab is not muted",
        )
        self.assertEqual(
            style_before(pane, "OC | dev"),
            "",
            "the pane is not in the plain colour",
        )


class TestNavigatrLayout(NavigatrTestCase):
    """The navigatr's rows: one composed string, not a set of columns.

    A tree's alignment is its indentation, so every row has a different
    width. The aligned table renderer is for the layers, where the columns
    SHOULD line up; here it flattened the tree into a wall of labels.
    """

    def _marker(self, overlay, row):
        """The branch marker the row is drawn with."""
        if row.depth == 0:
            return overlay.visuals.marker("branch") or overlay_mod.DEFAULT_BRANCH
        if row.is_last:
            return overlay.visuals.marker("elbow") or overlay_mod.DEFAULT_ELBOW
        return overlay.visuals.marker("tee") or overlay_mod.DEFAULT_TEE

    def _tree_lines(self, overlay):
        """The lines of the tree, in the order the rows are in.

        The chrome lines around the tree carry no branch marker, so the
        lines that do are the rows.
        """
        markers = (
            overlay.visuals.marker("branch") or overlay_mod.DEFAULT_BRANCH,
            overlay.visuals.marker("tee") or overlay_mod.DEFAULT_TEE,
            overlay.visuals.marker("elbow") or overlay_mod.DEFAULT_ELBOW,
        )
        return [line for line in self._lines(overlay) if any(m in line for m in markers)]

    def test_the_content_starts_right_after_the_rows_own_marker(self):
        # The marker and the content it marks move together: the content
        # of a row starts one column after that row's own branch marker
        # ends, so the marker is always immediately left of what it marks.
        overlay, _ = self._overlay()
        rows = self._open_expanded(overlay).rows
        lines = self._tree_lines(overlay)
        self.assertEqual(len(lines), len(rows))
        for row, line in zip(rows, lines):
            marker = self._marker(overlay, row)
            self.assertIn(marker, line, "the row has no marker of its own: %r" % line)
            marker_col = line.index(marker)
            kind = overlay.visuals.group_icon(row.kind)
            self.assertTrue(kind, "the row has no kind: %r" % line)
            kind_col = line.index(kind)
            self.assertEqual(
                kind_col,
                marker_col + len(marker) + 1,
                "the content of %r does not start after its own marker: %r"
                % (row.label, line),
            )

    def test_the_content_column_moves_right_with_the_depth(self):
        # The depth of a row is in its indentation. The old layout put
        # the markers in a narrow column on the left and the content in a
        # flat wall on the right, so the tree converged instead of
        # opening outward.
        overlay, _ = self._overlay()
        rows = self._open(overlay).rows
        lines = self._tree_lines(overlay)
        columns = {}
        for row, line in zip(rows, lines):
            kind = overlay.visuals.group_icon(row.kind)
            columns.setdefault(row.depth, set()).add(line.index(kind))
        self.assertGreater(len(columns), 1, "the tree has only one depth")
        firsts = []
        for depth in sorted(columns):
            self.assertEqual(
                len(columns[depth]), 1, "the rows of one depth are not aligned"
            )
            firsts.append(sorted(columns[depth])[0])
        self.assertEqual(
            firsts, sorted(firsts), "the deeper rows are not further right: %r" % firsts
        )
        self.assertEqual(
            len(set(firsts)), len(firsts), "two depths share a content column: %r" % firsts
        )

    def test_the_names_march_with_the_depth(self):
        # The name of a row is further right the deeper the row is. The
        # number used to sit at the front of the LABEL, and it pushed the
        # name of a space two columns to the right of the name of the tab
        # under it: the tree's names did not march with its depth.
        overlay, _ = self._overlay()
        rows = self._open_expanded(overlay).rows
        lines = self._tree_lines(overlay)
        self.assertEqual(len(lines), len(rows))
        columns = {}
        for row, line in zip(rows, lines):
            columns.setdefault(row.depth, set()).add(line.index(row.label))
        self.assertGreater(len(columns), 1, "the tree has only one depth")
        firsts = []
        for depth in sorted(columns):
            self.assertEqual(
                len(columns[depth]), 1, "the rows of one depth are not aligned"
            )
            firsts.append(sorted(columns[depth])[0])
        self.assertEqual(
            firsts,
            sorted(firsts),
            "the deeper rows are not further right: %r" % firsts,
        )
        self.assertEqual(
            len(set(firsts)), len(firsts), "two depths share a name column: %r" % firsts
        )

    def test_the_status_is_a_column_at_the_right_hand_end(self):
        # The status is drawn at the right-hand end of the row, so every
        # status in the list is in the same place: that is what makes the
        # colours a column the eye can scan, instead of a colour it has
        # to find in every line.
        #
        # It used to be drawn next to the name, which was right when the
        # rows were a pile of ragged text and the status was floating in
        # the middle of one. A column says the same thing better: every
        # row has a status in it, so the place they are in is the place
        # the status belongs.
        overlay, _ = self._overlay()
        self._open(overlay)
        lines = self._tree_lines(overlay)
        columns = set()
        for line in lines:
            for glyph in ("●", "○", "◐", "✓"):
                if glyph in line:
                    columns.add(line.index(glyph))
                    break
        self.assertTrue(columns, "no row shows a status")
        self.assertEqual(
            len(columns), 1, "the statuses are not in one column: %r" % columns
        )
        # And it is at the right-hand end of the row.
        agent = next(line for line in lines if "opencode" in line)
        self.assertGreater(
            agent.index("●"),
            agent.index("opencode"),
            "the status is not after the name: %r" % agent,
        )

    def test_a_row_without_a_status_shows_none(self):
        overlay, _ = self._overlay()
        self._open(overlay)
        lines = self._tree_lines(overlay)
        pane = next(line for line in lines if "zsh" in line)
        self.assertNotIn("●", pane)

    def test_the_kind_is_readable_without_the_icons(self):
        # The glyphs are the only thing that says a row is a space, a tab
        # or a pane. With them off, the kind has to be spelled out: the
        # tree's shape does not say it.
        #
        # There are three kinds and not four: the agent is folded into the
        # pane that hosts it, so it is not a row of its own any more.
        overlay, _ = self._overlay_without_icons()
        rows = self._open_expanded(overlay).rows
        lines = self._tree_lines(overlay)
        self.assertEqual(len(lines), len(rows))
        words = set()
        for row, line in zip(rows, lines):
            word = overlay.visuals.kind_label(row.kind)
            self.assertTrue(word, "no word for the kind %r" % row.kind)
            self.assertIn(word, line, "the kind is not drawn: %r" % line)
            words.add(word)
        # And the three kinds are three different words.
        self.assertEqual(len(words), 3, "two kinds share a word: %r" % words)
        self.assertEqual(words, {"space", "tab", "pane"})

    def test_the_kind_is_a_glyph_when_the_icons_are_on(self):
        overlay, _ = self._overlay()
        self._open(overlay)
        lines = self._tree_lines(overlay)
        kinds = set()
        for row in overlay.navigatr.rows:
            kinds.add(overlay.visuals.group_icon(row.kind))
        # Three kinds: the agent is folded into its pane.
        self.assertEqual(len(kinds), 3)
        for kind in kinds:
            self.assertTrue(kind)
        self.assertTrue(any(kind in line for line in lines for kind in kinds))

    def test_the_widest_row_still_fits(self):
        # The tree markers are a different width on every row now, so the
        # widest row is the one to check.
        #
        # The width is measured in columns, not in characters: a CJK title
        # is one character and two columns, so `len()` under-counts a row
        # that holds one.
        for columns in (20, 30, 40, 53, 80):
            overlay, _ = self._overlay(columns=columns)
            self._open(overlay)
            for line in self._screen(overlay).split("\r\n"):
                plain = ANSI.sub("", line)
                if not plain:
                    continue
                width = display_width(plain)
                self.assertLessEqual(
                    width,
                    columns - 1,
                    "a line is %d columns wide in a %d column terminal: %r"
                    % (width, columns, line),
                )

    def test_a_wide_character_in_a_title_is_counted_in_columns(self):
        # The sample has to hold a wide character, or the guard above
        # cannot fail: a CJK title is one character and two columns, so a
        # ruler that counts characters calls a row narrower than it is.
        session = {
            "workspace.list": {
                "workspaces": [
                    {"workspace_id": "w1", "label": "dev", "focused": True},
                ]
            },
            "tab.list": {
                "tabs": [
                    {
                        "tab_id": "w1:t1",
                        "label": "code",
                        "focused": True,
                        "workspace_id": "w1",
                    }
                ]
            },
            "pane.list": {
                "panes": [
                    {
                        "pane_id": "w1:p1",
                        "focused": True,
                        "workspace_id": "w1",
                        "tab_id": "w1:t1",
                        "cwd": "/tmp/dev",
                        "terminal_title_stripped": "日本語" * 10,
                    }
                ]
            },
            "agent.list": {"agents": []},
        }
        overlay, _ = self._overlay(session=session, columns=53)
        self._open(overlay)
        for line in self._screen(overlay).split("\r\n"):
            plain = ANSI.sub("", line)
            if not plain:
                continue
            width = display_width(plain)
            self.assertLessEqual(
                width,
                52,
                "a line is %d columns wide in a 53 column terminal: %r"
                % (width, line),
            )

    def test_the_status_is_dropped_before_the_label_is_cut(self):
        # The status is a dependent element: it is dropped before the
        # label is cut, so the row ends cleanly instead of in a bare
        # ellipsis. A status cut off at the edge of the frame is a status
        # the user cannot read.
        #
        # 53 columns is the default -- the popup asks for 70% of an 80
        # column terminal -- and the title leaves just enough room for
        # the description and not for the status as well.
        title = "pane-" + "x" * 20
        overlay, _ = self._overlay(session=self._session_with_a_pane(title), columns=53)
        self._open(overlay)
        rows = [line for line in self._tree_lines(overlay) if title in line]
        self.assertTrue(rows, "the pane is not on the screen")
        row = rows[0]
        # The status is gone...
        self.assertNotIn("●", row, "the status was not dropped: %r" % row)
        # ...the description is still there...
        self.assertIn("opencode", row, "the description was dropped too: %r" % row)
        # ...and the row ends with the whole title, not with an ellipsis.
        self.assertIn(title, row, "the title was cut: %r" % row)
        self.assertNotIn("...", row, "the row ends in an ellipsis: %r" % row)

    def test_the_description_is_dropped_before_the_label_is_cut(self):
        # The description is a dependent element too: it goes before the
        # label is cut, so the name of the pane survives.
        title = "pane-" + "x" * 29
        overlay, _ = self._overlay(session=self._session_with_a_pane(title), columns=53)
        self._open(overlay)
        rows = [line for line in self._tree_lines(overlay) if title in line]
        self.assertTrue(rows, "the pane is not on the screen")
        row = rows[0]
        self.assertNotIn("opencode", row, "the description was not dropped: %r" % row)
        self.assertIn(title, row, "the title was cut: %r" % row)
        self.assertNotIn("...", row, "the row ends in an ellipsis: %r" % row)

    def _session_with_a_pane(self, title):
        """A session with one pane, one agent in it and the pane's title."""
        return {
            "workspace.list": {
                "workspaces": [
                    {"workspace_id": "w1", "label": "dev", "focused": True},
                ]
            },
            "tab.list": {
                "tabs": [
                    {
                        "tab_id": "w1:t1",
                        "label": "code",
                        "focused": True,
                        "workspace_id": "w1",
                    }
                ]
            },
            "pane.list": {
                "panes": [
                    {
                        "pane_id": "w1:p1",
                        "focused": True,
                        "workspace_id": "w1",
                        "tab_id": "w1:t1",
                        "cwd": "/tmp/dev",
                        "terminal_title_stripped": title,
                    }
                ]
            },
            "agent.list": {
                "agents": [
                    {
                        "agent": "opencode",
                        "agent_status": "idle",
                        "pane_id": "w1:p1",
                    }
                ]
            },
        }

    def test_the_status_is_kept_when_there_is_room_for_it(self):
        # The status is only dropped when the label leaves no room for it.
        overlay, _ = self._overlay(columns=53)
        self._open(overlay)
        rows = [line for line in self._tree_lines(overlay) if "opencode" in line]
        self.assertTrue(rows, "the agent is not on the screen")
        self.assertIn("●", rows[0], "the status was dropped: %r" % rows[0])

    def test_a_status_with_no_glyph_leaves_no_stray_spaces(self):
        # The separator between the name and the status only exists when
        # there is a glyph: a status with no glyph of its own would leave
        # two spaces at the end of the row, and they would be counted in
        # the width budget.
        #
        # The overlay is frameless here: the frame pads the row, and the
        # padding would hide the two spaces.
        overlay, _ = self._overlay_without_status_glyphs()
        self._open(overlay)
        rows = [line for line in self._lines(overlay) if "opencode" in line]
        self.assertTrue(rows, "the pane with the agent is not on the screen")
        row = rows[0]
        self.assertNotIn("●", row, "the status has a glyph after all")
        self.assertFalse(
            row.endswith("  "), "the row ends in two stray spaces: %r" % row
        )

    def test_a_status_with_a_glyph_keeps_its_separator(self):
        # The other half of the rule: with a glyph there is a separator,
        # and the status sits next to the name it belongs to.
        overlay, _ = self._overlay()
        self._open(overlay)
        rows = [line for line in self._lines(overlay) if "opencode" in line]
        self.assertTrue(rows, "the pane with the agent is not on the screen")
        self.assertTrue(
            frame_content(rows[0]).rstrip().endswith("●"),
            "the status is not next to the name: %r" % rows[0],
        )

    def test_a_colour_in_a_pane_title_is_not_applied(self):
        # The tree draws the session's own data now, and a pane title can
        # hold an escape sequence: a program running in a pane could
        # colour the overlay. The trust boundary moved, so the colour runs
        # are stripped out of the label.
        session = {
            "workspace.list": {
                "workspaces": [
                    {"workspace_id": "w1", "label": "dev", "focused": True},
                ]
            },
            "tab.list": {
                "tabs": [
                    {
                        "tab_id": "w1:t1",
                        "label": "code",
                        "focused": True,
                        "workspace_id": "w1",
                    }
                ]
            },
            "pane.list": {
                "panes": [
                    {
                        "pane_id": "w1:p1",
                        "focused": True,
                        "workspace_id": "w1",
                        "tab_id": "w1:t1",
                        "cwd": "/tmp/dev",
                        "terminal_title_stripped": "\x1b[31mred\x1b[0m title",
                    }
                ]
            },
            "agent.list": {"agents": []},
        }
        overlay, _ = self._overlay(session=session)
        self._open(overlay)
        raw = self._raw(overlay)
        self.assertNotIn("\x1b[31m", raw, "the title's colour was applied")
        # The text is still there.
        self.assertIn("red", self._screen(overlay))
        # And the overlay's own colours still work: the sanitiser is
        # applied to the label, not to the row.
        self.assertIn(
            overlay.palette.accent,
            raw,
            "the overlay's own colours are gone",
        )

    def test_a_colour_in_the_description_is_not_applied(self):
        # The description of a row is the session's own data too: the
        # agent's name, and the names of the space and the tab. A colour
        # in it is the same trust boundary as a colour in the label.
        session = {
            "workspace.list": {
                "workspaces": [
                    {"workspace_id": "w1", "label": "dev", "focused": True},
                ]
            },
            "tab.list": {
                "tabs": [
                    {
                        "tab_id": "w1:t1",
                        "label": "code",
                        "focused": True,
                        "workspace_id": "w1",
                    }
                ]
            },
            "pane.list": {
                "panes": [
                    {
                        "pane_id": "w1:p1",
                        "focused": True,
                        "workspace_id": "w1",
                        "tab_id": "w1:t1",
                        "cwd": "/tmp/dev",
                        "terminal_title_stripped": "OC | dev",
                    }
                ]
            },
            "agent.list": {
                "agents": [
                    {
                        "agent": "\x1b[31mevil\x1b[0m",
                        "agent_status": "idle",
                        "pane_id": "w1:p1",
                        "workspace_id": "w1",
                        "tab_id": "w1:t1",
                    }
                ]
            },
        }
        overlay, _ = self._overlay(session=session)
        self._open(overlay)
        raw = self._raw(overlay)
        self.assertNotIn("\x1b[31m", raw, "the agent's colour was applied")
        self.assertIn("evil", self._screen(overlay))

    def test_a_colour_in_the_path_or_the_directory_is_not_applied(self):
        # The detail region is the session's own data too: the path is
        # made of the names of the space, the tab and the pane, and the
        # directory is the pane's. An escape sequence in either of them
        # moves the cursor in the middle of the line, and the frame's
        # border lands in the wrong column.
        session = {
            "workspace.list": {
                "workspaces": [
                    {"workspace_id": "w1", "label": "dev", "focused": True},
                ]
            },
            "tab.list": {
                "tabs": [
                    {
                        "tab_id": "w1:t1",
                        "label": "\x1b[3Ccode",
                        "focused": True,
                        "workspace_id": "w1",
                    }
                ]
            },
            "pane.list": {
                "panes": [
                    {
                        "pane_id": "w1:p1",
                        "focused": True,
                        "workspace_id": "w1",
                        "tab_id": "w1:t1",
                        "cwd": "/tmp/\x1b[10Cboom",
                        "terminal_title_stripped": "OC | dev",
                    }
                ]
            },
            "agent.list": {"agents": []},
        }
        overlay, _ = self._overlay(session=session)
        self._open(overlay)
        # The detail region shows the row under the cursor, and the
        # directory is the pane's: the cursor goes to the pane.
        overlay.navigatr.cursor = len(overlay.navigatr.rows) - 1
        raw = self._raw(overlay)
        self.assertNotIn("\x1b[3C", raw, "the tab's escape sequence was applied")
        self.assertNotIn("\x1b[10C", raw, "the directory's escape sequence was applied")
        # And the text is still there.
        screen = self._screen(overlay)
        self.assertIn("code", screen)
        self.assertIn("boom", screen)

    def test_the_cursor_is_hidden_while_the_overlay_draws_a_list(self):
        # Every line the overlay writes ends with a newline, so the
        # cursor is parked on the row after the frame -- outside the
        # panel, where it blinks as a block under the window.
        overlay, _ = self._overlay()
        self._open(overlay)
        raw = self._raw(overlay)
        self.assertIn(overlay_mod.HIDE_CURSOR, raw, "the cursor is not hidden")
        self.assertNotIn(overlay_mod.SHOW_CURSOR, raw, "the cursor was shown")

    def test_the_cursor_is_shown_while_a_value_is_typed(self):
        # The input state is the one place the cursor is wanted: the
        # user is typing into a line, and the cursor says where they
        # are.
        overlay, _ = self._overlay()
        overlay.handle(overlay_mod.Key("char", "t"))  # rename tab
        overlay.handle(overlay_mod.Key("char", "r"))
        self.assertIsNotNone(overlay.input_prompt)
        raw = self._raw(overlay)
        self.assertIn(overlay_mod.SHOW_CURSOR, raw, "the cursor is not shown")

    def test_the_cursor_is_on_the_line_being_typed_on(self):
        # Every line the overlay writes ends with a newline, so a cursor
        # that is only *shown* blinks under the frame -- outside the
        # panel. It has to be put where the typing is.
        overlay, _ = self._overlay()
        overlay.handle(overlay_mod.Key("char", "t"))  # rename tab
        overlay.handle(overlay_mod.Key("char", "r"))
        overlay.handle(overlay_mod.Key("char", "x"))
        raw = self._raw(overlay)
        self.assertRegex(raw, r"\x1b\[\d+;\d+H", "the cursor was not positioned")

    def test_the_cursor_is_shown_while_the_filter_is_typed(self):
        # The filter is the other state the user types in.
        overlay, _ = self._overlay()
        overlay.handle(overlay_mod.Key("char", "/"))
        self.assertTrue(overlay.filtering)
        raw = self._raw(overlay)
        self.assertIn(overlay_mod.SHOW_CURSOR, raw, "the cursor is not shown")
        self.assertRegex(raw, r"\x1b\[\d+;\d+H", "the cursor was not positioned")

    def test_the_cursor_is_put_back_when_the_overlay_closes(self):
        # The cursor was hidden while the overlay drew its list, and the
        # terminal it was hidden in is the user's own: it has to be given
        # back.
        overlay, _ = self._overlay()
        out = io.StringIO()
        overlay.out = out
        overlay._clear_screen()
        self.assertIn(overlay_mod.SHOW_CURSOR, out.getvalue())

    def test_a_colour_in_the_agents_list_is_not_applied(self):
        # The agents list has its own composer, and its own label and
        # description: the session's data is stripped there too.
        session = {
            "workspace.list": {"workspaces": []},
            "tab.list": {"tabs": []},
            "pane.list": {"panes": []},
            "agent.list": {
                "agents": [
                    {
                        "agent": "\x1b[31mevil\x1b[0m",
                        "agent_status": "idle",
                        "pane_id": "w1:p1",
                        "workspace_id": "w1",
                        "tab_id": "w1:t1",
                    }
                ]
            },
        }
        overlay, _ = self._overlay(session=session)
        self._open_agents(overlay)
        raw = self._raw(overlay)
        self.assertNotIn("\x1b[31m", raw, "the agent's colour was applied")
        self.assertIn("evil", self._screen(overlay))

    def test_a_colour_in_the_error_of_the_server_is_not_applied(self):
        # The error the server sends is text from outside the plugin
        # too: it must not be able to colour the overlay.
        overlay, _ = self._overlay()
        client = overlay.context.client
        client.error = None

        class Failing:
            def call(self, method, params=None):
                raise actions_mod.HerdrError("failed", "\x1b[31mboom\x1b[0m")

        overlay.context.client = Failing()
        overlay.handle(overlay_mod.Key("char", "j"))
        raw = self._raw(overlay)
        self.assertNotIn("\x1b[31m", raw, "the server's colour was applied")
        self.assertIn("boom", self._screen(overlay))

    def test_the_number_of_the_agents_list_has_a_slot_of_its_own(self):
        # The status is a column: it has to be in the same place on
        # every row. A row with no number in it -- the tenth agent and
        # the ones after it -- must not shift the rest of the row, or
        # the column moves two columns to the left after the ninth
        # agent.
        session = {
            "workspace.list": {"workspaces": []},
            "tab.list": {"tabs": []},
            "pane.list": {"panes": []},
            "agent.list": {
                "agents": [
                    {
                        "agent": "agent-%02d" % index,
                        "agent_status": "idle",
                        "pane_id": "w1:p%d" % index,
                        "workspace_id": "w1",
                        "tab_id": "w1:t1",
                    }
                    for index in range(12)
                ]
            },
        }
        overlay, _ = self._overlay(session=session)
        self._open_agents(overlay)
        # The rows, not the detail region: the path under the list holds
        # the name of the agent too.
        lines = [line for line in self._lines(overlay) if "idle" in line]
        self.assertEqual(len(lines), 12, "not every agent is on the screen")
        columns = set()
        for line in lines:
            index = line.index("agent-")
            columns.add(index)
        self.assertEqual(
            len(columns),
            1,
            "the name of the agent is not in one column: %r" % sorted(columns),
        )
        # And the status is in one column too.
        statuses = set()
        for line in lines:
            statuses.add(line.index("idle"))
        self.assertEqual(len(statuses), 1, "the status is not in one column")


class TestNavigatrNumberedSpaces(NavigatrTestCase):
    """The spaces are numbered, and a number jumps to one."""

    def _session(self, spaces):
        return {
            "workspace.list": {
                "workspaces": [
                    {"workspace_id": "w%d" % (i + 1), "label": name, "focused": False}
                    for i, name in enumerate(spaces)
                ]
            },
            "tab.list": {"tabs": []},
            "pane.list": {"panes": []},
            "agent.list": {"agents": []},
        }

    def test_the_spaces_are_numbered_in_the_order_they_are_shown(self):
        overlay, _ = self._overlay(session=self._session(["one", "two", "three"]))
        rows = self._open(overlay).rows
        numbers = overlay._navigatr_numbers(rows)
        numbered = [numbers[i] for i in sorted(numbers)]
        self.assertEqual(numbered, ["1", "2", "3"])

    def test_only_the_spaces_are_numbered(self):
        overlay, _ = self._overlay(session=self._session(["one", "two"]))
        rows = self._open(overlay).rows
        numbers = overlay._navigatr_numbers(rows)
        for index, row in enumerate(rows):
            if row.kind != "workspace":
                self.assertNotIn(index, numbers, "a non-space row was numbered")

    def test_the_number_is_drawn_on_the_space(self):
        overlay, _ = self._overlay(session=self._session(["one", "two"]))
        self._open_expanded(overlay)
        screen = self._screen(overlay)
        # The number is in a column of its own, in front of the tree's
        # markers, so it does not push the name out of step with them.
        self.assertIn("1 ▾", screen.replace("", "▾"))
        self.assertIn("2 ▾", screen.replace("", "▾"))

    def test_the_numbers_are_in_one_column(self):
        # A number is a column: every space's number is in the same
        # column, so the eye can find the second space without reading
        # the row.
        overlay, _ = self._overlay(session=self._session(["one", "two", "three"]))
        self._open(overlay)
        columns = set()
        for line in self._raw(overlay).split("\r\n"):
            column = 0
            for text, escape in style_runs(line):
                # The number is the only thing drawn in the dimmed accent
                # in the tree.
                if escape == overlay.palette.chord and text.strip().isdigit():
                    columns.add(column)
                column += len(text)
        self.assertEqual(len(columns), 1, "the numbers are not in one column")

    def test_the_number_does_not_push_the_content_out_of_step(self):
        # The number is in a column of its own, in front of the tree's
        # markers, so it cannot push the content out of step with them.
        # It used to sit at the front of the LABEL, and it pushed the
        # name of a space two columns to the right of the name of the tab
        # under it: the tree's names no longer marched with its depth.
        overlay, _ = self._overlay(session=self._session(["one", "two"]))
        rows = self._open_expanded(overlay).rows
        lines = [
            line
            for line in self._lines(overlay)
            if any(m in line for m in (overlay_mod.DEFAULT_BRANCH, overlay_mod.DEFAULT_TEE, overlay_mod.DEFAULT_ELBOW))
        ]
        self.assertEqual(len(lines), len(rows))
        for row, line in zip(rows, lines):
            marker = overlay_mod.DEFAULT_BRANCH if row.depth == 0 else (
                overlay_mod.DEFAULT_ELBOW if row.is_last else overlay_mod.DEFAULT_TEE
            )
            marker_col = line.index(marker)
            # the kind glyph sits one column after the marker ends
            after = line[marker_col + len(marker):]
            self.assertTrue(
                after.lstrip().startswith(overlay.visuals.group_icon(row.kind)),
                "the content does not follow the marker: %r" % line,
            )

    def test_a_number_moves_the_cursor_to_that_space(self):
        # The number does NOT jump. It moves the cursor, so the user can see
        # where they are pointing; `enter` is what selects.
        overlay, client = self._overlay(session=self._session(["one", "two", "three"]))
        self._open(overlay)
        overlay.handle(overlay_mod.Key("char", "2"))
        self.assertTrue(overlay.running, "the overlay closed on a number")
        self.assertEqual(
            overlay.navigatr.cursor, 1, "the cursor did not move to the second space"
        )
        self.assertEqual(
            [call for call in client.calls if call[0] == "workspace.focus"],
            [],
            "a number must not focus anything",
        )

    def test_enter_after_a_number_selects_that_space(self):
        overlay, client = self._overlay(session=self._session(["one", "two", "three"]))
        self._open(overlay)
        overlay.handle(overlay_mod.Key("char", "3"))
        overlay.handle(overlay_mod.Key("enter"))
        self.assertFalse(overlay.running, "the overlay did not close")
        self.assertIn(
            ("workspace.focus", {"workspace_id": "w3"}),
            client.calls,
            "the wrong space was focused: %r" % (client.calls,),
        )

    def test_a_number_beyond_the_last_space_says_so(self):
        overlay, _ = self._overlay(session=self._session(["one", "two"]))
        self._open(overlay)
        overlay.handle(overlay_mod.Key("char", "5"))
        self.assertTrue(overlay.running, "the overlay closed on a number with no space")
        self.assertIn("no space numbered 5", overlay.message)

    def test_the_number_is_never_given_up(self):
        # The number is the row's identity, so it keeps its room as long
        # as it can: the dependent elements -- the status and the
        # description -- go first, and the label is cut last.
        #
        # The number is in a column of its own, so it is never given up:
        # the footer promises the keys that name the spaces, and a number
        # that is not on the screen is a promise the overlay cannot keep.
        #
        # The row is measured at the width it really has, so the sample is
        # one column short of the row: the description has to go, and the
        # number and the label stay.
        overlay, _ = self._overlay(columns=200)
        self._open(overlay)
        branch = overlay.visuals.marker("branch") or overlay_mod.DEFAULT_BRANCH
        row = next(
            line for line in self._lines(overlay)
            if branch in line and "dev" in line
        )
        natural = display_width(frame_content(row).rstrip())
        # One column less than the row needs.
        overlay, _ = self._overlay(columns=natural + 2)
        self._open(overlay)
        row = next(
            line for line in self._lines(overlay)
            if branch in line and "dev" in line
        )
        self.assertIn("1 ", row, "the number gave up its room: %r" % row)
        self.assertNotIn(
            "2 tabs", row, "the description outlived the number: %r" % row
        )
        # And the label is whole: it is cut last of all.
        self.assertIn("dev", row)

    def test_the_filter_keeps_the_structure(self):
        # A space or a tab row is a heading, and it is shown when one of
        # the rows under it survived the filter: a tree without its
        # headings is a list, and the list is what the agents navigatr
        # is for.
        overlay, _ = self._overlay()
        self._open(overlay)
        overlay._start_filter()
        for char in "OC":
            overlay.handle(overlay_mod.Key("char", char))
        overlay.handle(overlay_mod.Key("enter"))
        rows = overlay._navigatr_visible()
        kinds = [row.kind for row in rows]
        self.assertIn("pane", kinds, "the pane that matched is not shown")
        self.assertIn("workspace", kinds, "the space above it is not shown")
        self.assertIn("tab", kinds, "the tab above it is not shown")

    def test_the_filter_keeps_the_structure_of_the_agents_list(self):
        # The agents list is flat: there is no structure to keep.
        overlay, _ = self._overlay(session=AGENTS_SESSION)
        self._open_agents(overlay)
        overlay._start_filter()
        for char in "idle":
            overlay.handle(overlay_mod.Key("char", char))
        overlay.handle(overlay_mod.Key("enter"))
        rows = overlay._navigatr_visible()
        self.assertTrue(rows, "the filter hid everything")
        for row in rows:
            self.assertEqual(row.kind, "agent")

    def test_a_filter_that_matched_only_a_tab_says_so(self):
        # A tab with no pane under it is a heading with nothing in it,
        # and there is nowhere to go. The overlay says so instead of
        # quietly sending the user somewhere else.
        session = {
            "workspace.list": {
                "workspaces": [
                    {"workspace_id": "w1", "label": "dev", "focused": True},
                ]
            },
            "tab.list": {
                "tabs": [
                    {
                        "tab_id": "w1:t1",
                        "label": "logs",
                        "focused": False,
                        "workspace_id": "w1",
                    }
                ]
            },
            "pane.list": {"panes": []},
            "agent.list": {"agents": []},
        }
        overlay, client = self._overlay(session=session)
        self._open(overlay)
        overlay._start_filter()
        for char in "logs":
            overlay.handle(overlay_mod.Key("char", char))
        overlay.handle(overlay_mod.Key("enter"))
        self.assertIn("heading", overlay.message)
        # And the jump does not go to a place the user did not ask for.
        overlay.handle(overlay_mod.Key("enter"))
        self.assertNotIn(
            "tab.focus",
            [call[0] for call in client.calls],
            "the tab was focused",
        )

    def test_the_numbers_follow_the_filter(self):
        # A number must always name a row the user can see.
        overlay, _ = self._overlay(session=self._session(["alpha", "beta", "gamma"]))
        self._open(overlay)
        overlay._start_filter()
        for char in "beta":
            overlay.handle(overlay_mod.Key("char", char))
        overlay.handle(overlay_mod.Key("enter"))
        rows = overlay._navigatr_visible()
        numbers = overlay._navigatr_numbers(rows)
        self.assertEqual([numbers[i] for i in sorted(numbers)], ["1"])

    def test_a_digit_typed_in_the_filter_is_query_text(self):
        overlay, _ = self._overlay(session=self._session(["one", "two"]))
        self._open(overlay)
        overlay._start_filter()
        overlay.handle(overlay_mod.Key("char", "2"))
        self.assertEqual(overlay.filter_draft, "2")
        self.assertTrue(overlay.running, "the filter jumped instead of taking the text")

    def test_more_than_nine_spaces_number_only_the_first_nine(self):
        overlay, _ = self._overlay(session=self._session(["s%d" % i for i in range(12)]))
        rows = self._open(overlay).rows
        numbers = overlay._navigatr_numbers(rows)
        self.assertEqual(len(numbers), 9)
        self.assertEqual(numbers[0], "1")
        self.assertEqual(numbers[8], "9")


    def test_the_footer_mentions_the_numbers_when_there_is_room(self):
        overlay, _ = self._overlay(session=self._session(["one", "two"]), columns=100)
        self._open(overlay)
        footer = [line for line in self._lines(overlay) if "esc back" in line]
        self.assertTrue(footer, "there is no footer")
        self.assertIn("1-9 space", footer[0])

    def test_the_footer_is_never_left_half_a_hint(self):
        # When the numbers do not fit, the footer drops them rather than being
        # cut in half: a hint that ends in "..." tells the user less than a
        # complete one.
        #
        # What is left is the fold hint: it is the one key the user cannot
        # guess, and a hint that is not there is a feature nobody finds.
        overlay, _ = self._overlay(session=self._session(["one", "two"]), columns=56)
        self._open(overlay)
        footer = [line for line in self._lines(overlay) if "esc back" in line]
        self.assertTrue(footer, "there is no footer")
        self.assertNotIn("1-9 space", footer[0], "the numbers do not fit")
        self.assertIn("h/l fold", footer[0])
        self.assertNotIn("...", footer[0])

    def test_the_footer_is_never_left_half_a_hint_with_a_badge(self):
        # The position badge and the key hints share the room in the border.
        # A tree long enough to show the badge, at a width where the numbers
        # DO fit, must still leave the whole hint - not "… /fi... 1/20".
        overlay, _ = self._overlay(
            session=self._session(["one", "two"]), rows=8, columns=64
        )
        self._open(overlay)
        # A long tree, so the badge appears.
        for index in range(40):
            overlay.navigatr.rows.append(
                navigatr_mod.Row(
                    kind="pane", label="pane %02d" % index, depth=2
                )
            )
        footer = [line for line in self._lines(overlay) if "esc back" in line]
        self.assertTrue(footer, "there is no footer")
        self.assertNotIn("...", footer[0], "the footer was cut: %r" % footer[0])
        self.assertIn("h/l fold", footer[0])

    def test_the_filtered_footer_mentions_the_numbers(self):
        # The spaces are still numbered while a filter is in force, and a digit
        # still jumps, so the footer has to say so.
        overlay, _ = self._overlay(session=self._session(["one", "two"]), columns=100)
        self._open(overlay)
        overlay._start_filter()
        for char in "one":
            overlay.handle(overlay_mod.Key("char", char))
        overlay.handle(overlay_mod.Key("enter"))
        footer = [line for line in self._lines(overlay) if "esc clear filter" in line]
        self.assertTrue(footer, "there is no footer")
        self.assertIn("1-9 space", footer[0])


class TestNavigatrKeys(NavigatrTestCase):
    """The keys, the cursor and the jumps."""

    def test_j_opens_the_navigatr_from_the_root(self):
        overlay, _ = self._overlay()
        self._open(overlay)
        self.assertIsNotNone(overlay.navigatr)
        self.assertEqual(overlay.layer_id, "root")

    def test_the_j_key_is_free_in_the_shipped_layers(self):
        # The root lists the layers by their key, so the keys the layers
        # declare are the keys the root binds. "j" has to be free there,
        # or the navigatr's key would shadow a layer's own.
        path = os.path.join(ROOT, "src", "managr", "data", "layers.toml")
        with open(path, "r", encoding="utf-8") as handle:
            data = tomlite.loads(handle.read())
        for entry in data.get("layer", []) or []:
            self.assertNotEqual(entry.get("key"), "j", entry)
        # And the root binds it for the navigatr and for nothing else.
        tree = tree_mod.build(keymap=fake_keymap(), labels={})
        owners = [item.action for item in tree.root.items if item.key == "j"]
        self.assertEqual(owners, [tree_mod.NAVIGATR_ACTION])

    def test_a_layer_that_binds_j_keeps_it(self):
        # The navigatr only takes the key when nothing else wants it.
        tree = tree_mod.build(keymap=fake_keymap(), labels={})
        client = FakeSession()
        overlay = overlay_mod.Overlay(tree, actions_mod.Context(client))
        overlay.layer_id = "spaces"
        overlay.stack = ["spaces"]
        overlay.handle(overlay_mod.Key("char", "j"))
        self.assertIsNone(overlay.navigatr, "the layer's own key was taken")

    def test_escape_returns_to_the_root_layer(self):
        overlay, _ = self._overlay()
        self._open(overlay)
        overlay.handle(overlay_mod.Key("esc"))
        self.assertIsNone(overlay.navigatr)
        self.assertEqual(overlay.layer_id, "root")
        self.assertTrue(overlay.running, "escape closed the overlay")

    def test_escape_clears_an_applied_filter_too(self):
        # A filter in force is the last thing the user asked for, so
        # Escape clears it and the navigatr stays -- exactly as it does
        # in a layer.
        overlay, _ = self._overlay()
        self._open(overlay)
        overlay.handle(overlay_mod.Key("char", "/"))
        for char in "ops":
            overlay.handle(overlay_mod.Key("char", char))
        overlay.handle(overlay_mod.Key("enter"))  # the filter is in force
        self.assertEqual(overlay.filter_query, "ops")
        overlay.handle(overlay_mod.Key("esc"))
        self.assertEqual(overlay.filter_query, "", "the filter was not cleared")
        self.assertIsNotNone(overlay.navigatr, "escape left the navigatr")
        self.assertTrue(overlay.running, "escape closed the overlay")
        # And the tree is whole again.
        self.assertIn("dev", self._screen(overlay))

    def test_escape_with_no_filter_leaves_the_navigatr(self):
        # Only an Escape with no filter in force leaves the navigatr: the
        # first one clears the filter, the second one leaves.
        overlay, _ = self._overlay()
        self._open(overlay)
        overlay.handle(overlay_mod.Key("char", "/"))
        for char in "ops":
            overlay.handle(overlay_mod.Key("char", char))
        overlay.handle(overlay_mod.Key("enter"))
        overlay.handle(overlay_mod.Key("esc"))  # clears the filter
        self.assertIsNotNone(overlay.navigatr)
        overlay.handle(overlay_mod.Key("esc"))  # leaves the navigatr
        self.assertIsNone(overlay.navigatr)
        self.assertEqual(overlay.layer_id, "root")
        self.assertTrue(overlay.running, "escape closed the overlay")

    def test_the_footer_says_that_escape_clears_the_filter(self):
        # A hint that says "esc back" when Escape clears the filter is a
        # hint that lies, and the footer is the only place the user reads.
        overlay, _ = self._overlay()
        self._open(overlay)
        self.assertIn("esc back", self._screen(overlay))
        overlay.handle(overlay_mod.Key("char", "/"))
        for char in "ops":
            overlay.handle(overlay_mod.Key("char", char))
        overlay.handle(overlay_mod.Key("enter"))
        screen = self._screen(overlay)
        self.assertIn("esc clear filter", screen)
        self.assertNotIn("esc back", screen)

    def test_the_cursor_moves_with_the_arrows(self):
        overlay, _ = self._overlay()
        self._open(overlay)
        overlay.handle(overlay_mod.Key("down"))
        # The tab row is a heading, so the cursor skips it and lands on
        # the first pane.
        self.assertEqual(overlay.navigatr.cursor, 2)
        overlay.handle(overlay_mod.Key("up"))
        self.assertEqual(overlay.navigatr.cursor, 0)

    def test_the_cursor_moves_with_j_and_k(self):
        # There are no layer actions in the navigatr, and the user
        # expects j and k to move the cursor: they move it everywhere else.
        overlay, _ = self._overlay()
        self._open(overlay)
        overlay.handle(overlay_mod.Key("char", "j"))
        self.assertEqual(overlay.navigatr.cursor, 2)
        overlay.handle(overlay_mod.Key("char", "k"))
        self.assertEqual(overlay.navigatr.cursor, 0)

    def test_the_cursor_never_stops_on_a_tab(self):
        # A tab is a heading, not a destination: the cursor skips it, so
        # a pane to a pane is one key.
        overlay, _ = self._overlay()
        navigatr = self._open(overlay)
        seen = []
        for _ in range(10):
            seen.append(navigatr.rows[navigatr.cursor].kind)
            overlay.handle(overlay_mod.Key("char", "j"))
        self.assertNotIn("tab", seen, "the cursor stopped on a tab row")
        self.assertIn("pane", seen)
        self.assertIn("workspace", seen)

    def test_the_footer_says_that_j_and_k_move_the_cursor(self):
        overlay, _ = self._overlay()
        self._open(overlay)
        self.assertIn("j/k move", self._screen(overlay))

    def test_the_cursor_is_visible(self):
        overlay, _ = self._overlay()
        self._open(overlay)
        overlay.handle(overlay_mod.Key("down"))
        markers = [line for line in self._lines(overlay) if ">" in line]
        self.assertEqual(len(markers), 1, "the cursor is not visible")
        self.assertIn(self._row(overlay, 1), markers[0])

    def test_the_cursor_cannot_leave_the_list(self):
        overlay, _ = self._overlay()
        navigatr = self._open_expanded(overlay)
        for _ in range(50):
            overlay.handle(overlay_mod.Key("char", "j"))
        self.assertEqual(navigatr.cursor, len(navigatr.rows) - 1)
        for _ in range(50):
            overlay.handle(overlay_mod.Key("char", "k"))
        self.assertEqual(navigatr.cursor, 0)

    def test_the_cursor_cannot_leave_the_filtered_list(self):
        overlay, _ = self._overlay()
        navigatr = self._open(overlay)
        for _ in range(50):
            overlay.handle(overlay_mod.Key("char", "j"))
        overlay.handle(overlay_mod.Key("char", "/"))
        for char in "opencode":
            overlay.handle(overlay_mod.Key("char", char))
        # The filter keeps the structure: the space and the tab above the
        # pane that matched are shown as well, so the user can see where
        # the pane is. The cursor cannot be past the list.
        visible = overlay._navigatr_visible()
        self.assertEqual(len(visible), 3)
        self.assertLessEqual(navigatr.cursor, len(visible) - 1)
        # And Enter jumps to the row under the cursor, not to nothing.
        overlay.handle(overlay_mod.Key("enter"))  # accept the filter
        overlay.handle(overlay_mod.Key("enter"))  # jump
        # The filter matched the agent's name, and the row it found is the
        # pane that hosts the agent.
        self.assertIn(
            ("pane.focus", {"pane_id": "w1:p1"}), overlay.context.client.calls
        )

    def test_enter_jumps_to_the_row_under_the_cursor_and_closes(self):
        overlay, client = self._overlay()
        self._open(overlay)
        overlay.handle(overlay_mod.Key("enter"))
        self.assertFalse(overlay.running, "the overlay stayed open")
        # The parameter names were read off the live server: a call with the
        # wrong one is rejected with `missing field`.
        self.assertIn(("workspace.focus", {"workspace_id": "w1"}), client.calls)

    def test_enter_on_a_tab_is_refused(self):
        # A tab is a heading, not a place: the cursor never stops on one
        # and `enter` never goes to one. The tab always has at least one
        # pane, and focusing a tab lands on a pane anyway.
        overlay, client = self._overlay()
        self._open(overlay)
        rows = overlay.navigatr.rows
        index = next(i for i, row in enumerate(rows) if row.kind == "tab")
        overlay._jump_to_row(rows, index)
        self.assertNotIn(
            "tab.focus",
            [call[0] for call in client.calls],
            "the tab was focused",
        )
        self.assertIn("heading", overlay.message)
        self.assertTrue(overlay.running, "the overlay closed on a heading")

    def test_enter_on_a_pane_calls_pane_focus(self):
        overlay, client = self._overlay()
        self._open(overlay)
        # The cursor skips the tab row, so one `down` from the space is
        # the first pane.
        overlay.handle(overlay_mod.Key("down"))
        overlay.handle(overlay_mod.Key("enter"))
        self.assertIn(("pane.focus", {"pane_id": "w1:p1"}), client.calls)

    def test_enter_on_a_pane_with_an_agent_calls_pane_focus(self):
        # The agent is folded into the pane's row, and the row is the
        # pane: `agent.focus` and `pane.focus` land on the same pane, so
        # the pane row focuses the pane and the agent is right there.
        overlay, client = self._overlay()
        self._open(overlay)
        overlay.handle(overlay_mod.Key("down"))
        overlay.handle(overlay_mod.Key("enter"))
        self.assertIn(("pane.focus", {"pane_id": "w1:p1"}), client.calls)

    def test_a_row_whose_target_is_gone_is_reported(self):
        class Gone(FakeSession):
            def call(self, method, params=None):
                if method == "pane.focus":
                    from managr.client import HerdrError

                    raise HerdrError("pane_not_found", "pane w1:p1 not found")
                return FakeSession.call(self, method, params)

        tree = tree_mod.build(keymap=fake_keymap(), labels={})
        client = Gone()
        overlay = overlay_mod.Overlay(tree, actions_mod.Context(client))
        overlay._size = lambda: (80, 24)
        self._open(overlay)
        for _ in range(2):
            overlay.handle(overlay_mod.Key("down"))
        overlay.handle(overlay_mod.Key("enter"))
        self.assertTrue(overlay.running, "the overlay closed on a failed jump")
        self.assertIn("cannot go there", overlay.message)
        # The reason Herdr gave has to be in there: "cannot go there" on
        # its own says nothing about why.
        self.assertIn("not found", overlay.message)

    def test_the_filter_narrows_the_tree(self):
        overlay, _ = self._overlay()
        self._open(overlay)
        overlay.handle(overlay_mod.Key("char", "/"))
        for char in "ops":
            overlay.handle(overlay_mod.Key("char", char))
        screen = self._screen(overlay)
        self.assertIn("ops", screen)
        self.assertNotIn("dev", screen)

    def test_the_filter_matches_the_whole_row(self):
        # The filter matches the space name, the tab name, the pane name
        # and the agent name -- not one field of the row. Each of these is
        # in a different field, and "ops" is in none of them.
        overlay, _ = self._overlay()
        self._open(overlay)
        for query, row in (
            ("dev", "dev"),
            ("code", "code"),
            ("zsh", "zsh"),
            ("opencode", "opencode"),
        ):
            overlay.handle(overlay_mod.Key("char", "/"))
            for char in query:
                overlay.handle(overlay_mod.Key("char", char))
            screen = self._screen(overlay)
            self.assertNotIn("no match for %s" % query, screen, query)
            self.assertIn(row, screen, "the row that matches %r is gone" % query)
            self.assertNotIn("ops", screen, "%r matched a row it should not" % query)
            # One escape throws the query away and leaves the navigatr
            # open; a second one would leave the navigatr itself.
            overlay.handle(overlay_mod.Key("esc"))
            self.assertIsNotNone(overlay.navigatr, query)

    def test_the_query_is_visible(self):
        overlay, _ = self._overlay()
        self._open(overlay)
        overlay.handle(overlay_mod.Key("char", "/"))
        for char in "ops":
            overlay.handle(overlay_mod.Key("char", char))
        self.assertIn("/ ops", self._screen(overlay))

    def test_enter_on_a_filtered_list_jumps_to_the_match(self):
        overlay, client = self._overlay()
        self._open(overlay)
        overlay.handle(overlay_mod.Key("char", "/"))
        for char in "main":
            overlay.handle(overlay_mod.Key("char", char))
        overlay.handle(overlay_mod.Key("enter"))
        overlay.handle(overlay_mod.Key("enter"))
        # The filter matched the tab "main" -- and the pane under it,
        # whose own text holds the tab's name. The tab is a heading, so
        # the cursor goes to the pane: the place the tab stands for.
        self.assertIn(("pane.focus", {"pane_id": "w2:p1"}), client.calls)

    def test_the_position_is_shown_in_the_frame(self):
        overlay, _ = self._overlay(rows=10)
        self._open_expanded(overlay)
        screen = self._screen(overlay)
        self.assertRegex(screen, r"\d+/\d+", "the frame does not say where we are")

    def test_the_data_is_fetched_when_the_navigatr_is_opened(self):
        overlay, client = self._overlay()
        self._open(overlay)
        self.assertEqual(len(client.calls), 4)

    def test_the_data_is_not_fetched_on_every_render(self):
        # A render happens on every keystroke. A socket round trip per
        # keystroke would make the overlay feel like it is lagging.
        overlay, client = self._overlay()
        self._open(overlay)
        for _ in range(10):
            self._screen(overlay)
            overlay.handle(overlay_mod.Key("down"))
            self._screen(overlay)
        self.assertEqual(len(client.calls), 4, "the session was fetched again")

    def test_re_entering_the_navigatr_fetches_the_session_again(self):
        overlay, client = self._overlay()
        self._open(overlay)
        overlay.handle(overlay_mod.Key("esc"))
        self._open(overlay)
        self.assertEqual(len(client.calls), 8)

    def test_a_failing_list_call_shows_a_message_and_does_not_crash(self):
        from managr.client import HerdrUnavailable

        overlay, _ = self._overlay(error=HerdrUnavailable("no socket"))
        navigatr = self._open(overlay)
        self.assertTrue(navigatr.error, "the failure was not reported")
        self.assertIn("herdr unreachable", navigatr.error)
        screen = self._screen(overlay)
        self.assertIn("herdr unreachable", screen)

    def test_the_overlay_is_still_usable_after_a_failure(self):
        from managr.client import HerdrUnavailable

        overlay, _ = self._overlay(error=HerdrUnavailable("no socket"))
        self._open(overlay)
        overlay.handle(overlay_mod.Key("esc"))
        self.assertIsNone(overlay.navigatr)
        self.assertEqual(overlay.layer_id, "root")
        self.assertTrue(overlay.running)

    def test_a_rejected_list_call_shows_a_message(self):
        from managr.client import HerdrError

        overlay, _ = self._overlay(error=HerdrError("server_busy", "busy"))
        navigatr = self._open(overlay)
        self.assertIn("cannot read the session", navigatr.error)

    def test_no_line_is_wider_than_the_terminal(self):
        for columns in (20, 30, 40, 53, 80):
            overlay, _ = self._overlay(columns=columns)
            self._open(overlay)
            for line in self._screen(overlay).split("\r\n"):
                plain = ANSI.sub("", line)
                if not plain:
                    continue
                self.assertLessEqual(
                    len(plain),
                    columns - 1,
                    "a line is %d columns wide in a %d column terminal: %r"
                    % (len(plain), columns, line),
                )

    def test_every_line_ends_with_crlf(self):
        overlay, _ = self._overlay()
        self._open(overlay)
        out = io.StringIO()
        overlay.out = out
        overlay.render()
        self.assertNotIn("\n", out.getvalue().replace("\r\n", ""))


class TestNavigatrFolds(NavigatrTestCase):
    """The folds: a space with its rows hidden, and the keys that open it.

    The navigatr opens as a list of spaces, with the one the session is in
    open. The tree of every space is a lot of rows to read past when all
    the user wants is to go somewhere else.
    """

    def _two_spaces(self):
        """Two spaces, the second one focused, each with a tab and a pane."""
        return {
            "workspace.list": {
                "workspaces": [
                    {"workspace_id": "w1", "label": "one", "focused": False},
                    {"workspace_id": "w2", "label": "two", "focused": True},
                ]
            },
            "tab.list": {
                "tabs": [
                    {"tab_id": "w1:t1", "label": "a", "focused": False, "workspace_id": "w1"},
                    {"tab_id": "w2:t1", "label": "b", "focused": True, "workspace_id": "w2"},
                ]
            },
            "pane.list": {
                "panes": [
                    {
                        "pane_id": "w1:p1",
                        "focused": False,
                        "workspace_id": "w1",
                        "tab_id": "w1:t1",
                        "cwd": "/tmp/one",
                        "terminal_title_stripped": "pane one",
                    },
                    {
                        "pane_id": "w2:p1",
                        "focused": True,
                        "workspace_id": "w2",
                        "tab_id": "w2:t1",
                        "cwd": "/tmp/two",
                        "terminal_title_stripped": "pane two",
                    },
                ]
            },
            "agent.list": {"agents": []},
        }

    def _labels(self, overlay):
        return [row.label for row in overlay._navigatr_visible()]

    def test_the_navigatr_opens_folded_except_the_focused_space(self):
        # The space the session is in is open, so the user can see where
        # they are. Every other space is one row, and its tab and its pane
        # are not shown.
        overlay, _ = self._overlay(session=self._two_spaces())
        self._open(overlay)
        self.assertEqual(self._labels(overlay), ["one", "two", "b", "pane two"])

    def test_h_folds_the_space_the_cursor_is_in(self):
        overlay, _ = self._overlay(session=self._two_spaces())
        self._open(overlay)
        overlay.handle(overlay_mod.Key("char", "2"))
        overlay.handle(overlay_mod.Key("char", "h"))
        self.assertEqual(self._labels(overlay), ["one", "two"])

    def test_l_unfolds_the_space_the_cursor_is_in(self):
        overlay, _ = self._overlay(session=self._two_spaces())
        self._open(overlay)
        overlay.handle(overlay_mod.Key("char", "2"))
        overlay.handle(overlay_mod.Key("char", "h"))
        overlay.handle(overlay_mod.Key("char", "l"))
        self.assertEqual(self._labels(overlay), ["one", "two", "b", "pane two"])

    def test_H_folds_every_space(self):
        overlay, _ = self._overlay(session=self._two_spaces())
        self._open(overlay)
        overlay.handle(overlay_mod.Key("char", "H"))
        self.assertEqual(self._labels(overlay), ["one", "two"])

    def test_L_unfolds_every_space(self):
        overlay, _ = self._overlay(session=self._two_spaces())
        self._open(overlay)
        overlay.handle(overlay_mod.Key("char", "L"))
        self.assertEqual(
            self._labels(overlay),
            ["one", "a", "pane one", "two", "b", "pane two"],
        )

    def test_a_folded_space_is_marked(self):
        overlay, _ = self._overlay(session=self._two_spaces())
        self._open(overlay)
        screen = self._screen(overlay)
        self.assertIn(overlay_mod.DEFAULT_COLLAPSED, screen)

    def test_a_fold_survives_the_navigatr_being_opened_again(self):
        # The state lives on the overlay and not in the rows, because the
        # rows are rebuilt every time the navigatr is opened.
        overlay, _ = self._overlay(session=self._two_spaces())
        self._open(overlay)
        overlay.handle(overlay_mod.Key("char", "2"))
        overlay.handle(overlay_mod.Key("char", "h"))
        overlay.handle(overlay_mod.Key("esc"))
        self._open(overlay)
        self.assertEqual(self._labels(overlay), ["one", "two"])

    def test_a_filter_suspends_the_folds(self):
        # A filter is an explicit request to see something, and a place the
        # filter cannot reach is a place the user cannot go to.
        overlay, _ = self._overlay(session=self._two_spaces())
        self._open(overlay)
        overlay._start_filter()
        for char in "pane one":
            overlay.handle(overlay_mod.Key("char", char))
        overlay.handle(overlay_mod.Key("enter"))
        self.assertIn("pane one", self._labels(overlay))

    def test_a_fold_is_refused_while_a_filter_is_in_force(self):
        overlay, _ = self._overlay(session=self._two_spaces())
        self._open(overlay)
        overlay._start_filter()
        for char in "two":
            overlay.handle(overlay_mod.Key("char", char))
        overlay.handle(overlay_mod.Key("enter"))
        overlay.handle(overlay_mod.Key("char", "h"))
        self.assertIn("filter", overlay.message)

    def test_the_cursor_goes_to_the_space_that_was_folded(self):
        # The cursor was on a pane under the space. The space folds, and
        # the pane is not there any more -- so the cursor goes to the space.
        overlay, _ = self._overlay(session=self._two_spaces())
        self._open(overlay)
        overlay.handle(overlay_mod.Key("char", "2"))
        overlay.handle(overlay_mod.Key("char", "j"))
        self.assertEqual(self._labels(overlay)[overlay.navigatr.cursor], "pane two")
        overlay.handle(overlay_mod.Key("char", "h"))
        self.assertEqual(self._labels(overlay)[overlay.navigatr.cursor], "two")

    def test_the_cursor_goes_to_its_space_when_every_space_is_folded(self):
        overlay, _ = self._overlay(session=self._two_spaces())
        self._open(overlay)
        overlay.handle(overlay_mod.Key("char", "2"))
        overlay.handle(overlay_mod.Key("char", "j"))     # the pane under it
        overlay.handle(overlay_mod.Key("char", "H"))     # fold every space
        self.assertEqual(self._labels(overlay), ["one", "two"])
        self.assertEqual(self._labels(overlay)[overlay.navigatr.cursor], "two")

    def test_the_cursor_stays_on_its_row_when_another_space_is_folded(self):
        overlay, _ = self._overlay(session=self._two_spaces())
        self._open(overlay)
        overlay.handle(overlay_mod.Key("char", "2"))
        overlay.handle(overlay_mod.Key("char", "j"))
        overlay.handle(overlay_mod.Key("char", "h"))
        # The space under the cursor is folded; the cursor is on the space.
        overlay.handle(overlay_mod.Key("char", "l"))
        overlay.handle(overlay_mod.Key("char", "j"))
        self.assertEqual(self._labels(overlay)[overlay.navigatr.cursor], "pane two")


class TestNavigatrRearrange(NavigatrTestCase):
    """The rearranging: a tab or a pane picked up and put down elsewhere.

    A tab can be reordered inside its own space -- that is the one thing
    Herdr's `tab.move` does -- and a pane can go into any tab, or into a
    new tab of any space.
    """

    def _one_space(self):
        """One space with two tabs, each with a pane."""
        return {
            "workspace.list": {
                "workspaces": [
                    {"workspace_id": "w1", "label": "one", "focused": True},
                ]
            },
            "tab.list": {
                "tabs": [
                    {"tab_id": "w1:t1", "label": "a", "focused": True, "workspace_id": "w1"},
                    {"tab_id": "w1:t2", "label": "b", "focused": False, "workspace_id": "w1"},
                ]
            },
            "pane.list": {
                "panes": [
                    {
                        "pane_id": "w1:p1",
                        "focused": True,
                        "workspace_id": "w1",
                        "tab_id": "w1:t1",
                        "cwd": "/tmp/one",
                        "terminal_title_stripped": "pane a",
                    },
                    {
                        "pane_id": "w1:p2",
                        "focused": False,
                        "workspace_id": "w1",
                        "tab_id": "w1:t2",
                        "cwd": "/tmp/one",
                        "terminal_title_stripped": "pane b",
                    },
                ]
            },
            "agent.list": {"agents": []},
        }

    def _two_spaces(self):
        """Two spaces, each with a tab and a pane."""
        session = self._one_space()
        session["workspace.list"]["workspaces"].append(
            {"workspace_id": "w2", "label": "two", "focused": False}
        )
        session["tab.list"]["tabs"].append(
            {"tab_id": "w2:t1", "label": "c", "focused": False, "workspace_id": "w2"}
        )
        session["pane.list"]["panes"].append(
            {
                "pane_id": "w2:p1",
                "focused": False,
                "workspace_id": "w2",
                "tab_id": "w2:t1",
                "cwd": "/tmp/two",
                "terminal_title_stripped": "pane c",
            }
        )
        return session

    def test_r_starts_the_rearranging(self):
        overlay, _ = self._overlay(session=self._one_space())
        self._open(overlay)
        overlay.handle(overlay_mod.Key("char", "r"))
        self.assertTrue(overlay._rearranging)

    def test_escape_leaves_the_rearranging(self):
        overlay, _ = self._overlay(session=self._one_space())
        self._open(overlay)
        overlay.handle(overlay_mod.Key("char", "r"))
        overlay.handle(overlay_mod.Key("esc"))
        self.assertFalse(overlay._rearranging)

    def test_the_cursor_can_stop_on_a_tab_while_rearranging(self):
        # A tab is a heading, so the cursor never stops on one -- except
        # while rearranging, when a tab is exactly what to aim at.
        overlay, _ = self._overlay(session=self._one_space())
        self._open(overlay)
        overlay.handle(overlay_mod.Key("char", "j"))
        self.assertNotEqual(
            overlay._navigatr_visible()[overlay.navigatr.cursor].kind, "tab"
        )
        overlay.handle(overlay_mod.Key("char", "r"))
        overlay.handle(overlay_mod.Key("char", "k"))
        self.assertEqual(
            overlay._navigatr_visible()[overlay.navigatr.cursor].kind, "tab"
        )

    def test_a_tab_is_put_down_on_another_tab(self):
        overlay, client = self._overlay(session=self._one_space())
        self._open(overlay)
        overlay.handle(overlay_mod.Key("char", "r"))
        overlay.handle(overlay_mod.Key("char", "j"))   # the tab "a"
        overlay.handle(overlay_mod.Key("enter"))       # pick it up
        overlay.handle(overlay_mod.Key("char", "j"))   # the pane under it
        overlay.handle(overlay_mod.Key("char", "j"))   # the tab "b"
        overlay.handle(overlay_mod.Key("enter"))       # put it down
        self.assertIn(
            ("tab.move", {"tab_id": "w1:t1", "insert_index": 2}), client.calls
        )

    def test_a_tab_put_down_on_the_tab_before_it(self):
        # The index Herdr wants is the place BEFORE the tab at that index,
        # and it counts the moved tab as still there. So moving the second
        # tab onto the first one is index 0, and moving the first tab onto
        # the second one is index 2 -- not 1.
        overlay, client = self._overlay(session=self._one_space())
        self._open(overlay)
        overlay.handle(overlay_mod.Key("char", "r"))
        overlay.handle(overlay_mod.Key("char", "j"))   # the tab "a"
        overlay.handle(overlay_mod.Key("char", "j"))   # the pane
        overlay.handle(overlay_mod.Key("char", "j"))   # the tab "b"
        overlay.handle(overlay_mod.Key("enter"))       # pick "b" up
        overlay.handle(overlay_mod.Key("char", "k"))   # the pane
        overlay.handle(overlay_mod.Key("char", "k"))   # the tab "a"
        overlay.handle(overlay_mod.Key("enter"))       # put it down
        self.assertIn(
            ("tab.move", {"tab_id": "w1:t2", "insert_index": 0}), client.calls
        )

    def test_a_tab_cannot_be_put_in_another_space(self):
        # Herdr cannot move a tab into another space at all: `tab.move`
        # counts its index inside the tab's own space.
        overlay, client = self._overlay(session=self._two_spaces())
        self._open(overlay)
        overlay.handle(overlay_mod.Key("char", "r"))
        overlay.handle(overlay_mod.Key("char", "j"))   # the tab "a"
        overlay.handle(overlay_mod.Key("enter"))       # pick it up
        for _ in range(3):
            overlay.handle(overlay_mod.Key("char", "j"))
        overlay.handle(overlay_mod.Key("enter"))       # put it down
        self.assertNotIn("tab.move", [call[0] for call in client.calls])
        self.assertIn("space", overlay.message)

    def test_a_pane_is_put_down_on_a_tab(self):
        overlay, client = self._overlay(session=self._one_space())
        self._open(overlay)
        overlay.handle(overlay_mod.Key("char", "r"))
        overlay.handle(overlay_mod.Key("char", "j"))   # the tab "a"
        overlay.handle(overlay_mod.Key("char", "j"))   # the pane "a"
        overlay.handle(overlay_mod.Key("enter"))       # pick the pane up
        overlay.handle(overlay_mod.Key("char", "k"))   # back to the tab
        overlay.handle(overlay_mod.Key("enter"))       # put it down
        self.assertIn(
            (
                "pane.move",
                {
                    "pane_id": "w1:p1",
                    "destination": {
                        "type": "tab",
                        "tab_id": "w1:t1",
                        "split": "right",
                    },
                    "focus": False,
                },
            ),
            client.calls,
        )

    def test_a_pane_cannot_be_put_down_on_a_pane(self):
        overlay, client = self._overlay(session=self._one_space())
        self._open(overlay)
        overlay.handle(overlay_mod.Key("char", "r"))
        overlay.handle(overlay_mod.Key("char", "j"))   # the tab "a"
        overlay.handle(overlay_mod.Key("char", "j"))   # the pane "a"
        overlay.handle(overlay_mod.Key("enter"))       # pick the pane up
        overlay.handle(overlay_mod.Key("char", "j"))   # the tab "b"
        overlay.handle(overlay_mod.Key("char", "j"))   # the pane "b"
        overlay.handle(overlay_mod.Key("enter"))       # put it down
        self.assertNotIn("pane.move", [call[0] for call in client.calls])
        self.assertIn("tab", overlay.message)

    def test_the_split_direction_is_asked_for_with_d(self):
        # Herdr needs to know which way the pane is split into the tab it
        # is dropped into, so the key that carries it says which.
        overlay, client = self._overlay(session=self._one_space())
        self._open(overlay)
        overlay.handle(overlay_mod.Key("char", "r"))
        overlay.handle(overlay_mod.Key("char", "j"))   # the tab "a"
        overlay.handle(overlay_mod.Key("char", "j"))   # the pane "a"
        overlay.handle(overlay_mod.Key("enter"))       # pick the pane up
        overlay.handle(overlay_mod.Key("char", "d"))
        self.assertEqual(overlay._split, "down")
        overlay.handle(overlay_mod.Key("char", "k"))   # back to the tab
        overlay.handle(overlay_mod.Key("enter"))       # put it down
        self.assertTrue(
            any(
                call[0] == "pane.move" and call[1]["destination"]["split"] == "down"
                for call in client.calls
            ),
            "the pane was not split down: %r" % (client.calls,),
        )

    def test_the_tree_is_fetched_again_after_a_move(self):
        # The tree is a picture of the session, and the session has just
        # changed.
        overlay, client = self._overlay(session=self._one_space())
        self._open(overlay)
        before = len(client.calls)
        overlay.handle(overlay_mod.Key("char", "r"))
        overlay.handle(overlay_mod.Key("char", "j"))
        overlay.handle(overlay_mod.Key("char", "j"))
        overlay.handle(overlay_mod.Key("enter"))
        overlay.handle(overlay_mod.Key("char", "k"))
        overlay.handle(overlay_mod.Key("enter"))
        self.assertGreater(len(client.calls), before + 4)

    def test_nothing_is_moved_when_the_rearranging_is_cancelled(self):
        overlay, client = self._overlay(session=self._one_space())
        self._open(overlay)
        overlay.handle(overlay_mod.Key("char", "r"))
        overlay.handle(overlay_mod.Key("char", "j"))
        overlay.handle(overlay_mod.Key("enter"))       # pick the tab up
        overlay.handle(overlay_mod.Key("esc"))         # and change your mind
        self.assertNotIn("tab.move", [call[0] for call in client.calls])

    def test_a_refused_drop_leaves_the_row_carried(self):
        # The user aimed at the wrong row. The row is still carried, so
        # they can aim somewhere else instead of starting over.
        overlay, client = self._overlay(session=self._one_space())
        self._open(overlay)
        overlay.handle(overlay_mod.Key("char", "r"))
        overlay.handle(overlay_mod.Key("char", "j"))   # the tab "a"
        overlay.handle(overlay_mod.Key("char", "j"))   # the pane "a"
        overlay.handle(overlay_mod.Key("enter"))       # pick the pane up
        overlay.handle(overlay_mod.Key("char", "j"))   # the tab "b"
        overlay.handle(overlay_mod.Key("char", "j"))   # the pane "b"
        overlay.handle(overlay_mod.Key("enter"))       # refused
        self.assertIsNotNone(overlay._carried)
        self.assertTrue(overlay._rearranging)

    def test_a_space_is_not_something_you_can_move(self):
        overlay, client = self._overlay(session=self._one_space())
        self._open(overlay)
        overlay.handle(overlay_mod.Key("char", "r"))
        overlay.handle(overlay_mod.Key("enter"))       # on the space
        self.assertIsNone(overlay._carried)
        self.assertNotIn("tab.move", [call[0] for call in client.calls])


class TestNavigatrFooter(NavigatrTestCase):
    """The hints at the bottom of the navigatr: what the keys do.

    The footer is one line and it does not have room for everything, so
    the hints are a ladder and the first one that fits is shown. The keys
    that cannot be guessed come first in the ladder, because they are the
    ones nobody finds any other way.
    """

    def _footer(self, overlay):
        out = io.StringIO()
        overlay.out = out
        overlay.render()
        lines = [
            line
            for line in ANSI.sub("", out.getvalue()).split("\r\n")
            if "esc back" in line
        ]
        return lines[0] if lines else ""

    def test_the_places_footer_names_the_fold_keys(self):
        overlay, _ = self._overlay(columns=56)
        self._open(overlay)
        self.assertIn("h/l fold", self._footer(overlay))

    def test_the_places_footer_names_the_rearranging(self):
        overlay, _ = self._overlay(columns=56)
        self._open(overlay)
        self.assertIn("r move", self._footer(overlay))

    def test_the_new_keys_are_in_the_footer_at_every_width(self):
        # The fold keys and the rearranging are the ones the user cannot
        # guess, so they are in the footer wherever the tree is drawn.
        for columns in (56, 60, 70, 80, 100):
            overlay, _ = self._overlay(columns=columns)
            self._open(overlay)
            footer = self._footer(overlay)
            self.assertIn("h/l fold", footer, "at %d columns" % columns)
            self.assertIn("r move", footer, "at %d columns" % columns)

    def test_the_agents_footer_names_no_fold_keys(self):
        # There are no spaces in the agents list to fold and nothing in it
        # to rearrange, so the footer does not offer the keys that do
        # those things.
        overlay, _ = self._overlay(columns=100)
        self._open_agents(overlay)
        footer = self._footer(overlay)
        self.assertNotIn("h/l", footer)
        self.assertNotIn("H/L", footer)
        self.assertNotIn("r move", footer)

    def test_the_footer_is_never_cut_in_half(self):
        # A hint that ends in "..." tells the user less than a complete
        # one, and the footer is the only place they can read the keys.
        for columns in range(44, 130, 2):
            overlay, _ = self._overlay(columns=columns, rows=12)
            self._open(overlay)
            footer = self._footer(overlay)
            self.assertNotIn(
                "...", footer, "the footer was cut at %d columns" % columns
            )


class TestTheNavigatrEntry(unittest.TestCase):
    """The root lists the navigatr, so the key that opens it is findable.

    The navigatr used to be a hidden key: a constant in the overlay that
    the root did not show. A key that does something and is nowhere on the
    screen is a key the user cannot find.
    """

    def _root_item(self, tree):
        return next(
            (
                item
                for item in tree.root.items
                if item.action == tree_mod.NAVIGATR_ACTION
            ),
            None,
        )

    def test_the_root_lists_the_navigatr(self):
        tree = tree_mod.build(keymap=fake_keymap(), labels={})
        item = self._root_item(tree)
        self.assertIsNotNone(item, "the root does not list the navigatr")
        self.assertEqual(item.key, "j")
        self.assertEqual(item.desc, "navigatr")

    def test_the_navigatr_entry_is_with_the_other_layers(self):
        tree = tree_mod.build(keymap=fake_keymap(), labels={})
        descs = [item.desc for item in tree.root.items]
        self.assertIn("navigatr", descs)
        # Between the layers and the reference view: it is a root entry,
        # and it reads like the others.
        self.assertLess(descs.index("custom"), descs.index("navigatr"))
        self.assertLess(descs.index("navigatr"), descs.index("all bindings"))

    def test_the_navigatr_entry_is_not_a_hint(self):
        # It is not an action Herdr owns, so it is not a hint row either:
        # a hint row says "press this in herdr", and this is pressed here.
        tree = tree_mod.build(keymap=fake_keymap(), labels={})
        item = self._root_item(tree)
        self.assertTrue(item.executable)
        self.assertFalse(item.is_hint)
        self.assertEqual(item.chord, "")

    def test_every_row_in_the_root_has_a_glyph(self):
        # The custom layer was the one row in the root drawn without one.
        tree = tree_mod.build(keymap=fake_keymap(), labels={})
        layers = visuals_mod.load_icons().get("layer", {})
        for item in tree.root.items:
            if not item.is_layer and not item.is_mode:
                continue
            self.assertIn(item.action.split(":", 1)[1], layers, item.action)

    def test_the_navigatr_key_is_the_one_from_the_definition_file(self):
        # Not a constant in the overlay: a user who wants it somewhere
        # else moves it in layers.toml.
        path = os.path.join(ROOT, "src", "managr", "data", "layers.toml")
        with open(path, "r", encoding="utf-8") as handle:
            data = tomlite.loads(handle.read())
        self.assertEqual(data.get("navigatr_key"), "j")

    def test_the_root_entry_opens_the_navigatr(self):
        tree = tree_mod.build(keymap=fake_keymap(), labels={})
        client = FakeSession()
        overlay = overlay_mod.Overlay(tree, actions_mod.Context(client))
        overlay.handle(overlay_mod.Key("char", "j"))
        self.assertIsNotNone(overlay.navigatr, "the root entry did not open it")

    def test_a_key_that_is_not_the_navigatrs_says_so(self):
        tree = tree_mod.build(keymap=fake_keymap(), labels={})
        client = FakeSession()
        overlay = overlay_mod.Overlay(tree, actions_mod.Context(client))
        overlay.handle(overlay_mod.Key("char", "z"))
        self.assertIsNone(overlay.navigatr)
        self.assertIn("no binding", overlay.message)


class TestTheAgentsNavigatr(NavigatrTestCase):
    """The agents list: every agent in the session, the urgent ones first.

    It is a flat list: no tree, no indentation, no branch markers. Every
    row is an agent, so a kind glyph on every row would be noise.
    """

    def _agents(self, overlay, session=None):
        navigatr = self._open_agents(overlay)
        self.assertIsNotNone(navigatr, "the agents list did not open")
        return navigatr.rows

    def test_a_at_the_root_opens_the_agents_list(self):
        overlay, _ = self._overlay(session=AGENTS_SESSION)
        navigatr = self._open_agents(overlay)
        self.assertIsNotNone(navigatr)
        self.assertEqual(navigatr.kind, "agents")
        self.assertEqual(overlay.layer_id, "root")

    def test_the_list_is_flat(self):
        overlay, _ = self._overlay(session=AGENTS_SESSION)
        rows = self._agents(overlay)
        for row in rows:
            self.assertEqual(row.kind, "agent")
            self.assertEqual(row.depth, 0)

    def test_the_rows_are_sorted_by_attention(self):
        # A blocked agent wants the user now, so it is at the top; then
        # the working ones, the done ones, the idle ones and the unknown
        # ones.
        overlay, _ = self._overlay(session=AGENTS_SESSION)
        rows = self._agents(overlay)
        self.assertEqual(
            [row.label for row in rows],
            [
                "blocked-one",
                "working-one",
                "done-one",
                "idle-one",
                "quiet-one",
            ],
        )

    def test_the_sort_within_one_level_is_by_the_labels(self):
        # The sort has to be deterministic: the space's label, then the
        # tab's label, then the agent's name.
        overlay, _ = self._overlay(session=SAME_STATE_SESSION)
        rows = self._agents(overlay)
        self.assertEqual(
            [row.label for row in rows],
            ["in-aaa", "in-alpha", "in-zeta"],
        )

    def test_the_row_says_where_the_agent_is(self):
        overlay, _ = self._overlay(session=AGENTS_SESSION)
        rows = self._agents(overlay)
        row = next(row for row in rows if row.label == "working-one")
        self.assertEqual(row.desc, "ops › main")

    def test_the_row_shows_the_status_glyph_and_the_status_word(self):
        # The glyph alone needs the vocabulary, and the word alone needs
        # the eye to read seven characters on every row.
        overlay, _ = self._overlay(session=AGENTS_SESSION)
        self._open_agents(overlay)
        lines = self._lines(overlay)
        blocked = next(line for line in lines if "blocked-one" in line)
        self.assertIn("◉", blocked)
        self.assertIn("blocked", blocked)
        working = next(line for line in lines if "working-one" in line)
        self.assertIn("◐", working)
        self.assertIn("working", working)

    def test_a_working_agent_is_not_drawn_as_an_unknown_one(self):
        # "working" is a category of its own: the glyph is the working
        # one, not the unknown one.
        self.assertEqual(navigatr_mod.status_category("working"), "working")
        self.assertEqual(navigatr_mod.status_category("busy"), "working")
        self.assertEqual(navigatr_mod.status_category("running"), "working")
        overlay, _ = self._overlay(session=AGENTS_SESSION)
        self._open_agents(overlay)
        lines = self._lines(overlay)
        working = next(line for line in lines if "working-one" in line)
        self.assertIn("◐", working)
        self.assertNotIn("○", working)
        unknown = next(line for line in lines if "quiet-one" in line)
        self.assertIn("○", unknown)

    def test_the_first_nine_agents_are_numbered_and_the_tenth_is_not(self):
        session = {
            "workspace.list": {"workspaces": []},
            "tab.list": {"tabs": []},
            "pane.list": {"panes": []},
            "agent.list": {
                "agents": [
                    {
                        "agent": "agent-%02d" % index,
                        "agent_status": "idle",
                        "pane_id": "w1:p%d" % index,
                    }
                    for index in range(12)
                ]
            },
        }
        overlay, _ = self._overlay(session=session)
        rows = self._agents(overlay)
        numbers = overlay._navigatr_numbers(rows)
        self.assertEqual(len(numbers), 9)
        self.assertEqual(numbers[0], "1")
        self.assertEqual(numbers[8], "9")
        self.assertNotIn(9, numbers)

    def test_the_number_is_drawn_on_the_agent(self):
        # The number goes at the front of the row's content, in the same
        # place as in the tree: in the tree it is at the front of the
        # label, and here the status comes between it and the name.
        overlay, _ = self._overlay(session=AGENTS_SESSION)
        self._open_agents(overlay)
        screen = self._screen(overlay)
        self.assertIn("1 ◉ blocked blocked-one", screen)
        self.assertIn("2 ◐ working working-one", screen)

    def test_a_number_moves_the_cursor_to_that_agent(self):
        # The number does NOT jump: it moves the cursor, and `enter` is
        # what selects.
        overlay, client = self._overlay(session=AGENTS_SESSION)
        self._open_agents(overlay)
        overlay.handle(overlay_mod.Key("char", "2"))
        self.assertTrue(overlay.running, "the overlay closed on a number")
        self.assertEqual(overlay.navigatr.cursor, 1)
        self.assertEqual(
            [call for call in client.calls if call[0] == "agent.focus"],
            [],
            "a number must not focus anything",
        )

    def test_enter_after_a_number_jumps_to_that_agent(self):
        overlay, client = self._overlay(session=AGENTS_SESSION)
        self._open_agents(overlay)
        overlay.handle(overlay_mod.Key("char", "2"))
        overlay.handle(overlay_mod.Key("enter"))
        self.assertFalse(overlay.running, "the overlay did not close")
        # `agent.focus` takes the agent's pane, not the agent's name.
        self.assertIn(
            ("agent.focus", {"target": "w2:p1"}),
            client.calls,
            "the wrong agent was focused: %r" % (client.calls,),
        )

    def test_a_number_beyond_the_last_agent_says_so(self):
        overlay, _ = self._overlay(session=AGENTS_SESSION)
        self._open_agents(overlay)
        overlay.handle(overlay_mod.Key("char", "9"))
        self.assertTrue(overlay.running)
        self.assertIn("no agent numbered 9", overlay.message)

    def test_the_numbers_follow_the_filter(self):
        overlay, _ = self._overlay(session=AGENTS_SESSION)
        self._open_agents(overlay)
        overlay._start_filter()
        for char in "done":
            overlay.handle(overlay_mod.Key("char", char))
        overlay.handle(overlay_mod.Key("enter"))
        rows = overlay._navigatr_visible()
        numbers = overlay._navigatr_numbers(rows)
        self.assertEqual([numbers[i] for i in sorted(numbers)], ["1"])

    def test_the_footer_says_that_the_numbers_are_the_agents(self):
        overlay, _ = self._overlay(session=AGENTS_SESSION, columns=100)
        self._open_agents(overlay)
        footer = [line for line in self._lines(overlay) if "esc back" in line]
        self.assertTrue(footer, "there is no footer")
        self.assertIn("1-9 agent", footer[0])

    def test_the_breadcrumb_says_agents(self):
        overlay, _ = self._overlay(session=AGENTS_SESSION)
        self._open_agents(overlay)
        self.assertIn("layers › agents", self._screen(overlay))

    def test_the_hint_says_what_the_list_is(self):
        overlay, _ = self._overlay(session=AGENTS_SESSION)
        self._open_agents(overlay)
        self.assertIn("every agent in this session", self._screen(overlay))

    def test_the_data_is_fetched_when_the_list_is_opened(self):
        overlay, client = self._overlay(session=AGENTS_SESSION)
        self._open_agents(overlay)
        self.assertEqual(len(client.calls), 4)

    def test_the_data_is_not_fetched_on_every_render(self):
        overlay, client = self._overlay(session=AGENTS_SESSION)
        self._open_agents(overlay)
        for _ in range(10):
            self._screen(overlay)
            overlay.handle(overlay_mod.Key("down"))
            self._screen(overlay)
        self.assertEqual(len(client.calls), 4, "the session was fetched again")

    def test_re_entering_the_list_fetches_the_session_again(self):
        overlay, client = self._overlay(session=AGENTS_SESSION)
        self._open_agents(overlay)
        overlay.handle(overlay_mod.Key("esc"))
        self._open_agents(overlay)
        self.assertEqual(len(client.calls), 8)

    def test_a_failing_list_call_says_so_and_does_not_crash(self):
        from managr.client import HerdrUnavailable

        overlay, _ = self._overlay(session=AGENTS_SESSION, error=HerdrUnavailable("no socket"))
        navigatr = self._open_agents(overlay)
        self.assertTrue(navigatr.error, "the failure was not reported")
        self.assertIn("herdr unreachable", navigatr.error)
        self.assertIn("herdr unreachable", self._screen(overlay))
        # And the overlay is still usable: the user still has to be able to
        # press Escape.
        overlay.handle(overlay_mod.Key("esc"))
        self.assertIsNone(overlay.navigatr)
        self.assertTrue(overlay.running)

    def test_escape_leaves_the_list_and_the_overlay_stays_open(self):
        overlay, _ = self._overlay(session=AGENTS_SESSION)
        self._open_agents(overlay)
        overlay.handle(overlay_mod.Key("esc"))
        self.assertIsNone(overlay.navigatr)
        self.assertEqual(overlay.layer_id, "root")
        self.assertTrue(overlay.running)

    def test_escape_clears_the_filter_first(self):
        overlay, _ = self._overlay(session=AGENTS_SESSION)
        self._open_agents(overlay)
        overlay.handle(overlay_mod.Key("char", "/"))
        for char in "blocked":
            overlay.handle(overlay_mod.Key("char", char))
        overlay.handle(overlay_mod.Key("enter"))
        self.assertEqual(overlay.filter_query, "blocked")
        overlay.handle(overlay_mod.Key("esc"))
        self.assertEqual(overlay.filter_query, "")
        self.assertIsNotNone(overlay.navigatr, "escape left the list")

    def test_the_filter_finds_an_agent_by_its_name_space_tab_or_title(self):
        overlay, _ = self._overlay(session=AGENTS_SESSION)
        self._open_agents(overlay)
        for query, row in (
            ("blocked-one", "blocked-one"),
            ("dev", "blocked-one"),
            ("main", "working-one"),
        ):
            overlay.handle(overlay_mod.Key("char", "/"))
            for char in query:
                overlay.handle(overlay_mod.Key("char", char))
            screen = self._screen(overlay)
            self.assertNotIn("no match for %s" % query, screen, query)
            self.assertIn(row, screen, "the row that matches %r is gone" % query)
            overlay.handle(overlay_mod.Key("esc"))

    def test_the_location_goes_before_the_name_is_cut(self):
        # The location is a dependent element: it goes before the name of
        # the agent is cut, because the name is the row.
        #
        # The location is drawn at the right-hand end of the row, so it
        # does not push the name along: at a width that fits the name but
        # not the two of them, the location is the one that goes.
        overlay, _ = self._overlay(session=AGENTS_SESSION, columns=40)
        self._open_agents(overlay)
        row = next(
            line for line in self._lines(overlay) if "blocked-one" in line
        )
        self.assertNotIn("›", row, "the location was kept: %r" % row)
        self.assertIn(
            "blocked-one", row, "the name of the agent was cut: %r" % row
        )
        self.assertNotIn("...", row, "the row ends in an ellipsis: %r" % row)

    def test_the_rows_have_no_kind_glyph(self):
        # Every row of the list is an agent, so saying so on every row is
        # noise: the list has no kind glyph at all.
        overlay, _ = self._overlay(session=AGENTS_SESSION)
        self._open_agents(overlay)
        glyph = overlay.visuals.group_icon("agent")
        self.assertTrue(glyph, "the agent glyph is missing")
        for line in self._lines(overlay):
            self.assertNotIn(
                glyph, line, "a kind glyph on an agents row: %r" % line
            )

    def test_the_rows_have_no_branch_markers(self):
        # The list is flat: no tree, so no branch markers either.
        overlay, _ = self._overlay(session=AGENTS_SESSION)
        self._open_agents(overlay)
        markers = (
            overlay.visuals.marker("branch") or overlay_mod.DEFAULT_BRANCH,
            overlay.visuals.marker("tee") or overlay_mod.DEFAULT_TEE,
            overlay.visuals.marker("elbow") or overlay_mod.DEFAULT_ELBOW,
        )
        for line in self._lines(overlay):
            for marker in markers:
                self.assertNotIn(
                    marker, line, "a branch marker on an agents row: %r" % line
                )

    def test_the_focused_agent_is_marked_with_the_same_arrow(self):
        # The marker is not decoration: it says which agent you are on.
        # It is the same arrow the tree uses for the current space, tab
        # and pane.
        overlay, _ = self._overlay(session=AGENTS_SESSION)
        self._open_agents(overlay)
        arrow = overlay.visuals.marker("arrow") or overlay_mod.DEFAULT_ARROW
        marked = [line for line in self._lines(overlay) if arrow in line]
        self.assertEqual(len(marked), 1, "the focused agent is not marked")
        self.assertIn("idle-one", marked[0])

    def test_the_filter_finds_an_agent_by_its_title(self):
        # The terminal title is part of the agent's own text, so the filter
        # finds the agent by the title of the terminal it runs in.
        overlay, _ = self._overlay(session=AGENTS_SESSION)
        self._open_agents(overlay)
        overlay.handle(overlay_mod.Key("char", "/"))
        for char in "notes":
            overlay.handle(overlay_mod.Key("char", char))
        screen = self._screen(overlay)
        self.assertNotIn("no match for notes", screen)
        self.assertIn("working-one", screen, "the agent was not found by its title")
        self.assertNotIn("blocked-one", screen, "the filter matched too much")

    def test_an_agent_whose_space_is_gone_is_shown_with_a_question_mark(self):
        # The four lists are four round trips: the space can be closed in
        # between two of them, and the agent is still there.
        session = {
            "workspace.list": {"workspaces": []},
            "tab.list": {"tabs": []},
            "pane.list": {"panes": []},
            "agent.list": {
                "agents": [
                    {
                        "agent": "orphan",
                        "agent_status": "idle",
                        "pane_id": "w9:p1",
                        "workspace_id": "w9",
                        "tab_id": "w9:t1",
                    }
                ]
            },
        }
        overlay, client = self._overlay(session=session)
        rows = self._agents(overlay)
        self.assertEqual(len(rows), 1, "the agent was dropped")
        self.assertEqual(rows[0].desc, "? › ?")
        # And it can still be jumped to: `agent.focus` takes the pane id.
        overlay.handle(overlay_mod.Key("enter"))
        self.assertIn(("agent.focus", {"target": "w9:p1"}), client.calls)

    def test_an_agent_with_no_name_is_called_agent(self):
        session = {
            "workspace.list": {"workspaces": []},
            "tab.list": {"tabs": []},
            "pane.list": {"panes": []},
            "agent.list": {
                "agents": [
                    {"agent_status": "idle", "pane_id": "w1:p1"},
                ]
            },
        }
        overlay, _ = self._overlay(session=session)
        rows = self._agents(overlay)
        self.assertEqual(rows[0].label, "agent")

    def test_no_agents_says_so_plainly(self):
        session = {
            "workspace.list": {"workspaces": []},
            "tab.list": {"tabs": []},
            "pane.list": {"panes": []},
            "agent.list": {"agents": []},
        }
        overlay, _ = self._overlay(session=session)
        self._open_agents(overlay)
        self.assertIn("no agents", self._screen(overlay))

    def test_no_line_is_wider_than_the_terminal(self):
        for columns in (20, 30, 40, 53, 80):
            overlay, _ = self._overlay(session=AGENTS_SESSION, columns=columns)
            self._open_agents(overlay)
            for line in self._screen(overlay).split("\r\n"):
                plain = ANSI.sub("", line)
                if not plain:
                    continue
                self.assertLessEqual(
                    display_width(plain),
                    columns - 1,
                    "a line is %d columns wide in a %d column terminal: %r"
                    % (display_width(plain), columns, line),
                )


class TestTheAgentsEntry(unittest.TestCase):
    """The root lists the agents list, so the key that opens it is findable.

    A key that does something and is nowhere on the screen is a key the
    user cannot find.
    """

    def _root_item(self, tree):
        return next(
            (
                item
                for item in tree.root.items
                if item.action == tree_mod.AGENTS_ACTION
            ),
            None,
        )

    def test_the_root_lists_the_agents(self):
        tree = tree_mod.build(keymap=fake_keymap(), labels={})
        item = self._root_item(tree)
        self.assertIsNotNone(item, "the root does not list the agents list")
        self.assertEqual(item.key, "a")
        self.assertEqual(item.desc, "agents")

    def test_the_agents_entry_is_with_the_other_layers(self):
        tree = tree_mod.build(keymap=fake_keymap(), labels={})
        descs = [item.desc for item in tree.root.items]
        self.assertIn("agents", descs)
        # Right after the navigatr: they are the two ways of browsing the
        # session, and the reference view comes last.
        self.assertLess(descs.index("navigatr"), descs.index("agents"))
        self.assertLess(descs.index("agents"), descs.index("all bindings"))

    def test_the_agents_entry_is_not_a_hint(self):
        # It is not an action Herdr owns, so it is not a hint row either:
        # a hint row says "press this in herdr", and this is pressed here.
        tree = tree_mod.build(keymap=fake_keymap(), labels={})
        item = self._root_item(tree)
        self.assertTrue(item.executable)
        self.assertFalse(item.is_hint)
        self.assertEqual(item.chord, "")

    def test_the_root_entry_opens_the_agents_list(self):
        tree = tree_mod.build(keymap=fake_keymap(), labels={})
        client = FakeSession()
        overlay = overlay_mod.Overlay(tree, actions_mod.Context(client))
        overlay.handle(overlay_mod.Key("char", "a"))
        self.assertIsNotNone(overlay.navigatr, "the root entry did not open it")
        self.assertEqual(overlay.navigatr.kind, "agents")

    def test_the_agents_key_is_the_one_from_the_definition_file(self):
        # Not a constant in the overlay: a user who wants it somewhere
        # else moves it in layers.toml.
        path = os.path.join(ROOT, "src", "managr", "data", "layers.toml")
        with open(path, "r", encoding="utf-8") as handle:
            data = tomlite.loads(handle.read())
        self.assertEqual(data.get("agents_key"), "a")

    def test_the_shipped_agents_key_is_one_character(self):
        path = os.path.join(ROOT, "src", "managr", "data", "layers.toml")
        with open(path, "r", encoding="utf-8") as handle:
            data = tomlite.loads(handle.read())
        self.assertEqual(len(data.get("agents_key", "")), 1)

    def test_every_row_in_the_root_has_a_glyph(self):
        # The agents list is a mode, and the root lists it, so it needs a
        # glyph of its own.
        tree = tree_mod.build(keymap=fake_keymap(), labels={})
        layers = visuals_mod.load_icons().get("layer", {})
        self.assertIn("agents", layers)


# -- the overlay's context ---------------------------------------------------

# A client that reports the focused pane. The overlay's context is a
# snapshot of where it was opened, and it is re-read after every action:
# the fake has to be able to say that the focus moved.
class FakeCurrent:
    def __init__(self, current=None, session=None):
        self.session = SESSION if session is None else session
        self.current = current or {}
        self.calls = []

    def call(self, method, params=None):
        self.calls.append((method, params))
        if method == "pane.current":
            return {"pane": self.current}
        return self.session.get(method, {})


class TestTheOverlayContext(unittest.TestCase):
    """The overlay's context: a snapshot, re-read after every action.

    A popup is session-modal: it takes every keystroke wherever it was
    opened. So an action that moves the workspace and leaves the overlay
    open strands the popup on the old one -- the user is in the new
    workspace and the keyboard is not -- and the plugin cannot be opened
    again until the overlay is closed by hand.

    So the context is re-read after every action, and the overlay closes
    itself when the workspace or the tab changed. The pane is not part of
    this: moving the focus between panes is the whole point of the
    navigation layer, and the overlay stays open for it.
    """

    def _overlay(self, current=None):
        tree = tree_mod.build(keymap=fake_keymap(), labels={})
        client = FakeCurrent(current)
        context = actions_mod.Context(
            client, workspace_id="w1", tab_id="w1:t1", pane_id="w1:p1"
        )
        overlay = overlay_mod.Overlay(tree, context)
        overlay._size = lambda: (80, 24)
        return overlay, client

    def _focus_right(self, overlay):
        """Run the navigation layer's focus action, the way a key does."""
        overlay.handle(overlay_mod.Key("char", "n"))
        overlay.handle(overlay_mod.Key("char", "l"))

    def test_the_context_is_re_read_after_an_action(self):
        # "split right" after "focus right" split the pane that was
        # focused when the overlay opened, because the context was a
        # snapshot of it.
        overlay, _ = self._overlay(
            {"pane_id": "w1:p2", "tab_id": "w1:t1", "workspace_id": "w1"}
        )
        self._focus_right(overlay)
        self.assertEqual(overlay.context.pane_id, "w1:p2")
        self.assertEqual(overlay.context.tab_id, "w1:t1")
        self.assertEqual(overlay.context.workspace_id, "w1")

    def test_a_workspace_change_closes_the_overlay(self):
        overlay, _ = self._overlay(
            {"pane_id": "w2:p1", "tab_id": "w2:t1", "workspace_id": "w2"}
        )
        self._focus_right(overlay)
        self.assertFalse(
            overlay.running, "the overlay stayed open on the old space"
        )

    def test_a_tab_change_closes_the_overlay(self):
        overlay, _ = self._overlay(
            {"pane_id": "w1:p3", "tab_id": "w1:t2", "workspace_id": "w1"}
        )
        self._focus_right(overlay)
        self.assertFalse(
            overlay.running, "the overlay stayed open on the old tab"
        )

    def test_a_pane_change_does_not_close_the_overlay(self):
        # Moving the focus between panes is what the navigation layer is
        # for, so the overlay stays open for it.
        overlay, _ = self._overlay(
            {"pane_id": "w1:p2", "tab_id": "w1:t1", "workspace_id": "w1"}
        )
        self._focus_right(overlay)
        self.assertTrue(overlay.running, "the overlay closed on a pane move")
        self.assertEqual(overlay.context.pane_id, "w1:p2")


# -- the custom layer -------------------------------------------------------

# The plugins' actions, as `plugin.action.list` and `plugin.list` report
# them. The shapes are the ones the live server sends.
PLUGINS = {
    "plugin.action.list": {
        "actions": [
            {
                "plugin_id": "managr",
                "action_id": "open",
                "title": "Open the layered keymap",
                "platforms": ["linux", "macos"],
            },
            {
                "plugin_id": "herdr-notes",
                "action_id": "bar",
                "title": "Print the keybinding hint line",
                "platforms": ["linux", "macos"],
            },
        ]
    },
    "plugin.list": {
        "plugins": [
            {"plugin_id": "managr", "enabled": True},
            {"plugin_id": "herdr-notes", "enabled": True},
        ]
    },
    # The shape the live server sends for an invoke: the action's process
    # has been launched and the log record says "running". The outcome
    # only turns up later, in `plugin.log.list`.
    "plugin.action.invoke": {
        "type": "plugin_action_invoked",
        "action": {
            "plugin_id": "managr",
            "action_id": "open",
            "title": "Open the layered keymap",
        },
        "log": {
            "log_id": "plugin-log-1",
            "plugin_id": "managr",
            "action_id": "open",
            "status": "running",
            "started_unix_ms": 0,
        },
    },
}


class FakePlugins:
    """A client with the plugins' actions in it, and a record of the calls."""

    def __init__(self, results=None, errors=None):
        self.results = PLUGINS if results is None else results
        self.errors = errors or {}
        self.calls = []

    def call(self, method, params=None):
        self.calls.append((method, params))
        if method in self.errors:
            raise self.errors[method]
        return self.results.get(method, {})

    def methods(self):
        return [method for method, _ in self.calls]


class TestCustomLayer(unittest.TestCase):
    """The custom layer: the user's own commands, and the plugins' actions.

    Its rows are not in the tree: they are the user's `[[keys.command]]`
    entries and the actions of every installed plugin, and they are fetched
    when the layer is opened.
    """

    def _command(self, kind="popup", command="/x/overlay.sh", description="layered keymap"):
        return keymap_mod.CustomCommand(
            key="prefix+space",
            type=kind,
            command=command,
            description=description,
            chord="space",
            direct=False,
        )

    def _overlay(self, custom=(), results=None, errors=None, rows=24, columns=100):
        km = keymap_mod.Keymap(prefix="C-a", bindings={}, custom=list(custom))
        tree = tree_mod.build(keymap=km, labels={})
        client = FakePlugins(results, errors)
        context = actions_mod.Context(client)
        overlay = overlay_mod.Overlay(tree, context)
        overlay._size = lambda: (columns, rows)
        return overlay, client

    def _open(self, overlay):
        overlay.handle(overlay_mod.Key("char", "c"))
        return overlay

    def _screen(self, overlay):
        out = io.StringIO()
        overlay.out = out
        overlay.render()
        return ANSI.sub("", out.getvalue())

    def test_the_custom_layer_is_reached_from_the_root(self):
        overlay, _ = self._overlay()
        keys = [item.key for item in overlay.tree.root.items]
        self.assertIn("c", keys, "the root does not bind the custom layer")
        self._open(overlay)
        self.assertEqual(overlay.layer_id, "custom")

    def test_the_layer_lists_the_users_commands(self):
        overlay, _ = self._overlay(custom=[self._command()])
        self._open(overlay)
        screen = self._screen(overlay)
        self.assertIn("layered keymap", screen)

    def test_a_command_without_a_description_shows_the_command(self):
        overlay, _ = self._overlay(
            custom=[self._command(command="lazygit", description="")]
        )
        self._open(overlay)
        self.assertIn("lazygit", self._screen(overlay))

    def test_the_layer_lists_the_installed_plugins_actions(self):
        overlay, _ = self._overlay()
        self._open(overlay)
        screen = self._screen(overlay)
        self.assertIn("Open the layered keymap", screen)
        self.assertIn("managr", screen)
        self.assertIn("Print the keybinding hint line", screen)
        self.assertIn("herdr-notes", screen)

    def test_a_plugin_action_is_invoked_with_the_right_id(self):
        overlay, client = self._overlay()
        self._open(overlay)
        overlay.handle(overlay_mod.Key("char", "a"))
        self.assertIn(
            (
                "plugin.action.invoke",
                {"action_id": "open", "plugin_id": "managr"},
            ),
            client.calls,
        )

    def test_a_shell_command_is_run_detached(self):
        marker = os.path.join(tempfile.mkdtemp(), "ran")
        self.addCleanup(shutil.rmtree, os.path.dirname(marker), True)
        overlay, _ = self._overlay(
            custom=[
                self._command(
                    kind="shell", command="touch %s" % marker, description="touch it"
                )
            ]
        )
        self._open(overlay)
        overlay.handle(overlay_mod.Key("char", "a"))
        # The command runs in the background: the overlay does not wait for
        # it, so the file may not be there yet.
        for _ in range(100):
            if os.path.exists(marker):
                break
            time.sleep(0.05)
        self.assertTrue(os.path.exists(marker), "the shell command did not run")
        self.assertIn("ran", overlay.message)
        self.assertTrue(overlay.running, "the overlay closed on the shell command")

    def test_a_shell_command_runs_from_the_focused_panes_directory(self):
        # Herdr runs a shell command from the focused pane's working
        # directory, and so does the overlay.
        work = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, work, True)
        km = keymap_mod.Keymap(
            prefix="C-a",
            bindings={},
            custom=[
                keymap_mod.CustomCommand(
                    key="prefix+l",
                    type="shell",
                    command="touch marker",
                    description="where am i",
                    chord="l",
                    direct=False,
                )
            ],
        )
        tree = tree_mod.build(keymap=km, labels={})
        context = actions_mod.Context(FakePlugins(), cwd=work)
        overlay = overlay_mod.Overlay(tree, context)
        overlay.handle(overlay_mod.Key("char", "c"))
        overlay.handle(overlay_mod.Key("char", "a"))
        for _ in range(100):
            if os.path.exists(os.path.join(work, "marker")):
                break
            time.sleep(0.05)
        self.assertTrue(
            os.path.exists(os.path.join(work, "marker")),
            "the command did not run in the focused pane's directory",
        )

    def test_the_shell_command_gets_herdr_context(self):
        # Herdr's docs promise a custom command six variables. The overlay
        # is the server's child, so it has to set them itself: the
        # working directory is the one the command runs from, and the
        # binary is found rather than inherited by accident.
        from managr import actions as actions_mod

        work = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, work, True)
        fake_bin = os.path.join(work, "herdr")
        with open(fake_bin, "w") as handle:
            handle.write("#!/bin/sh\nexit 0\n")
        os.chmod(fake_bin, 0o755)
        out = os.path.join(work, "context.txt")

        saved = dict(os.environ)

        def restore():
            os.environ.clear()
            os.environ.update(saved)

        self.addCleanup(restore)
        # The binary is not in the environment: the overlay has to find it.
        os.environ["PATH"] = work + os.pathsep + saved.get("PATH", "")
        os.environ.pop("HERDR_BIN_PATH", None)

        km = keymap_mod.Keymap(
            prefix="C-a",
            bindings={},
            custom=[
                keymap_mod.CustomCommand(
                    key="prefix+l",
                    type="shell",
                    command=(
                        'printf "%s\\n" "$HERDR_SOCKET_PATH" '
                        '"$HERDR_BIN_PATH" "$HERDR_ACTIVE_WORKSPACE_ID" '
                        '"$HERDR_ACTIVE_TAB_ID" "$HERDR_ACTIVE_PANE_ID" '
                        '"$HERDR_ACTIVE_PANE_CWD" > ' + out
                    ),
                    description="dump the context",
                    chord="l",
                    direct=False,
                )
            ],
        )
        tree = tree_mod.build(keymap=km, labels={})
        client = FakePlugins()
        client.socket_path = "/tmp/herdr-test.sock"
        context = actions_mod.Context(
            client,
            workspace_id="w1",
            tab_id="w1:t1",
            pane_id="w1:p1",
            cwd=work,
        )
        overlay = overlay_mod.Overlay(tree, context)
        overlay.handle(overlay_mod.Key("char", "c"))
        overlay.handle(overlay_mod.Key("char", "a"))
        for _ in range(100):
            if os.path.exists(out):
                break
            time.sleep(0.05)
        self.assertTrue(os.path.exists(out), "the shell command did not run")
        with open(out) as handle:
            lines = handle.read().splitlines()
        self.assertEqual(lines[0], "/tmp/herdr-test.sock")
        self.assertEqual(lines[1], fake_bin)
        self.assertEqual(lines[2], "w1")
        self.assertEqual(lines[3], "w1:t1")
        self.assertEqual(lines[4], "w1:p1")
        self.assertEqual(lines[5], work)

    def test_a_popup_command_reports_that_it_cannot_be_run(self):
        overlay, client = self._overlay(custom=[self._command(kind="popup")])
        self._open(overlay)
        overlay.handle(overlay_mod.Key("char", "a"))
        self.assertIn("cannot be run from here", overlay.message)
        # The key to press in Herdr is named.
        self.assertIn("C-a space", overlay.message)
        self.assertEqual(overlay.message_kind, "err")
        # Nothing was run: the overlay is a popup, and a popup is a
        # singleton, so opening another one would be refused.
        self.assertNotIn("plugin.action.invoke", client.methods())

    def test_a_pane_command_reports_that_it_cannot_be_run(self):
        overlay, client = self._overlay(custom=[self._command(kind="pane")])
        self._open(overlay)
        overlay.handle(overlay_mod.Key("char", "a"))
        self.assertIn("cannot be run from here", overlay.message)
        self.assertIn("C-a space", overlay.message)
        self.assertEqual(overlay.message_kind, "err")
        self.assertNotIn("plugin.action.invoke", client.methods())

    def test_a_plugin_action_is_reported_as_started_not_as_ran(self):
        # `plugin.action.invoke` launches the action's own process and
        # returns: the log record it hands back says "running", and the
        # outcome only turns up later in the plugin log. So whatever
        # happens inside that process -- a popup of its own, say -- is not
        # in the invoke's answer, and the overlay cannot claim the action
        # ran.
        overlay, _ = self._overlay()
        self._open(overlay)
        overlay.handle(overlay_mod.Key("char", "a"))
        self.assertTrue(overlay.message.startswith("started "), overlay.message)
        self.assertNotIn("ran ", overlay.message)

    def test_a_refused_plugin_action_is_reported(self):
        # The server does refuse an invoke -- for an action id that does
        # not exist, say -- and then the overlay says so.
        from managr.client import HerdrError

        overlay, _ = self._overlay(
            errors={
                "plugin.action.invoke": HerdrError(
                    "invalid_plugin_action_id", "invalid action id"
                )
            }
        )
        self._open(overlay)
        overlay.handle(overlay_mod.Key("char", "a"))
        self.assertIn("invalid action id", overlay.message)
        self.assertEqual(overlay.message_kind, "err")
        self.assertTrue(overlay.running, "the overlay closed on the refusal")

    def test_a_ui_busy_from_the_invoke_is_a_plain_failure(self):
        # `plugin.action.invoke` cannot answer `ui_busy`: in 0.9.5 that
        # error comes from `plugin.pane.open`, and the refusal of a plugin
        # action that wants a popup of its own happens inside the action's
        # own process, while the invoke itself succeeds. So there is no
        # special case for it here: an error is an error, and the overlay
        # does not read a meaning into the code.
        from managr.client import HerdrError

        overlay, _ = self._overlay(
            errors={"plugin.action.invoke": HerdrError("ui_busy", "ui busy")}
        )
        self._open(overlay)
        overlay.handle(overlay_mod.Key("char", "a"))
        self.assertIn("ui busy", overlay.message)
        self.assertNotIn("close the overlay first", overlay.message)
        self.assertEqual(overlay.message_kind, "err")

    def test_a_dotted_plugin_id_is_split_at_the_right_dot(self):
        # Herdr's plugin ids may hold dots and its action ids may not, so
        # "example.worktree-bootstrap.bootstrap" is the plugin
        # "example.worktree-bootstrap" and the action "bootstrap". The
        # server refuses an action id with a dot in it, so splitting at
        # the first dot breaks the binding.
        results = {
            "plugin.action.list": {"actions": []},
            "plugin.list": {
                "plugins": [
                    {"plugin_id": "example", "enabled": True},
                    {"plugin_id": "example.worktree-bootstrap", "enabled": True},
                ]
            },
        }
        overlay, client = self._overlay(
            custom=[
                self._command(
                    kind="plugin_action",
                    command="example.worktree-bootstrap.bootstrap",
                    description="bootstrap",
                )
            ],
            results=results,
        )
        self._open(overlay)
        overlay.handle(overlay_mod.Key("char", "a"))
        self.assertIn(
            (
                "plugin.action.invoke",
                {
                    "action_id": "bootstrap",
                    "plugin_id": "example.worktree-bootstrap",
                },
            ),
            client.calls,
        )

    def test_a_dotted_plugin_id_splits_at_the_last_dot_when_the_plugin_is_not_listed(self):
        # With no installed plugin to say where the separator is, the last
        # dot is the only one that can be it: an action id cannot hold a
        # dot.
        results = {
            "plugin.action.list": {"actions": []},
            "plugin.list": {"plugins": []},
        }
        overlay, client = self._overlay(
            custom=[
                self._command(
                    kind="plugin_action",
                    command="example.worktree-bootstrap.bootstrap",
                    description="bootstrap",
                )
            ],
            results=results,
        )
        self._open(overlay)
        overlay.handle(overlay_mod.Key("char", "a"))
        self.assertIn(
            (
                "plugin.action.invoke",
                {
                    "action_id": "bootstrap",
                    "plugin_id": "example.worktree-bootstrap",
                },
            ),
            client.calls,
        )

    def test_a_qualified_id_is_matched_against_the_installed_actions(self):
        # The whole id, matched against the qualified ids of the
        # installed plugins' actions: that is what Herdr's own keybind
        # path does, and it is the surest answer of the three.
        results = {
            "plugin.action.list": {
                "actions": [
                    {
                        "plugin_id": "example.worktree-bootstrap",
                        "action_id": "bootstrap",
                        "title": "Bootstrap the worktree",
                        "platforms": ["linux", "macos"],
                    }
                ]
            },
            "plugin.list": {
                "plugins": [
                    {"plugin_id": "example.worktree-bootstrap", "enabled": True}
                ]
            },
        }
        overlay, client = self._overlay(
            custom=[
                self._command(
                    kind="plugin_action",
                    command="example.worktree-bootstrap.bootstrap",
                    description="bootstrap",
                )
            ],
            results=results,
        )
        self._open(overlay)
        overlay.handle(overlay_mod.Key("char", "a"))
        self.assertIn(
            (
                "plugin.action.invoke",
                {
                    "action_id": "bootstrap",
                    "plugin_id": "example.worktree-bootstrap",
                },
            ),
            client.calls,
        )

    def test_the_list_is_fetched_when_the_layer_is_opened(self):
        overlay, client = self._overlay()
        self._open(overlay)
        self.assertEqual(client.methods().count("plugin.action.list"), 1)

    def test_re_entering_the_layer_does_not_duplicate_the_rows(self):
        overlay, _ = self._overlay()
        self._open(overlay)
        overlay.handle(overlay_mod.Key("esc"))
        self._open(overlay)
        descs = [item.desc for item in overlay.tree.layers["custom"].items]
        self.assertEqual(descs.count("Open the layered keymap"), 1)

    def test_the_list_is_not_fetched_on_every_render(self):
        # A render happens on every keystroke. A socket round trip per
        # keystroke would make the overlay feel like it is lagging.
        overlay, client = self._overlay()
        self._open(overlay)
        for _ in range(10):
            self._screen(overlay)
            overlay.handle(overlay_mod.Key("down"))
            self._screen(overlay)
        self.assertEqual(
            client.methods().count("plugin.action.list"), 1, "the list was fetched again"
        )

    def test_the_layer_scrolls_like_the_others(self):
        results = dict(PLUGINS)
        results["plugin.action.list"] = {
            "actions": [
                {
                    "plugin_id": "plug%02d" % index,
                    "action_id": "act%02d" % index,
                    "title": "Action number %02d" % index,
                    "platforms": ["linux", "macos"],
                }
                for index in range(40)
            ]
        }
        overlay, _ = self._overlay(results=results, rows=12)
        self._open(overlay)
        for _ in range(200):
            overlay.handle(overlay_mod.Key("down"))
        screen = self._screen(overlay)
        self.assertIn("Action number 39", screen)
        self.assertNotIn("Action number 00", screen)

    def test_the_last_row_is_reachable_when_the_layer_has_lines_of_its_own(self):
        # The custom layer's own lines -- here the "left out" line -- are
        # rows on the screen too. The scroll's budget has to count them,
        # or the window is a row too large and the last row can never be
        # scrolled into view: the badge says "39/39" and row 39 is not
        # there.
        results = dict(PLUGINS)
        results["plugin.action.list"] = {
            "actions": [
                {
                    "plugin_id": "plug%02d" % index,
                    "action_id": "act%02d" % index,
                    "title": "Action number %02d" % index,
                    "platforms": ["linux", "macos"],
                }
                for index in range(40)
            ]
        }
        # One plugin is disabled, so the layer has a line of its own.
        results["plugin.list"] = {
            "plugins": [
                {"plugin_id": "plug%02d" % index, "enabled": index != 7}
                for index in range(40)
            ]
        }
        overlay, _ = self._overlay(results=results, rows=12)
        self._open(overlay)
        # The line of its own is there, so the count is not the trivial one.
        self.assertIn("left out", self._screen(overlay))
        for _ in range(200):
            overlay.handle(overlay_mod.Key("down"))
        screen = self._screen(overlay)
        self.assertIn("Action number 39", screen)
        self.assertNotIn("Action number 00", screen)

    def test_the_layer_filters_like_the_others(self):
        overlay, _ = self._overlay()
        self._open(overlay)
        overlay.handle(overlay_mod.Key("char", "/"))
        for char in "notes":
            overlay.handle(overlay_mod.Key("char", char))
        screen = self._screen(overlay)
        self.assertIn("Print the keybinding hint line", screen)
        self.assertNotIn("Open the layered keymap", screen)

    def test_the_position_is_shown_in_the_frame(self):
        results = dict(PLUGINS)
        results["plugin.action.list"] = {
            "actions": [
                {
                    "plugin_id": "plug%02d" % index,
                    "action_id": "act%02d" % index,
                    "title": "Action number %02d" % index,
                    "platforms": ["linux", "macos"],
                }
                for index in range(40)
            ]
        }
        overlay, _ = self._overlay(results=results, rows=10)
        self._open(overlay)
        screen = self._screen(overlay)
        # The custom layer is on screen: the position is the layer's, not
        # the root's.
        self.assertIn("Action number 00", screen)
        self.assertRegex(screen, r"\d+/\d+")

    def test_nothing_at_all_says_so(self):
        results = {
            "plugin.action.list": {"actions": []},
            "plugin.list": {"plugins": []},
        }
        overlay, _ = self._overlay(results=results)
        self._open(overlay)
        screen = self._screen(overlay)
        self.assertIn("no custom commands and no plugin actions", screen)

    def test_a_disabled_plugin_is_left_out(self):
        results = dict(PLUGINS)
        results["plugin.list"] = {
            "plugins": [
                {"plugin_id": "managr", "enabled": True},
                {"plugin_id": "herdr-notes", "enabled": False},
            ]
        }
        overlay, _ = self._overlay(results=results)
        self._open(overlay)
        screen = self._screen(overlay)
        descs = [item.desc for item in overlay.tree.layers["custom"].items]
        self.assertNotIn("Print the keybinding hint line", descs)
        # The reason is reported: a row that is not shown is a thing the
        # user cannot find.
        self.assertIn("left out", screen)

    def test_an_action_for_another_platform_is_left_out(self):
        results = dict(PLUGINS)
        results["plugin.action.list"] = {
            "actions": [
                {
                    "plugin_id": "managr",
                    "action_id": "open",
                    "title": "Only on another platform",
                    "platforms": ["plan9"],
                }
            ]
        }
        overlay, _ = self._overlay(results=results)
        self._open(overlay)
        screen = self._screen(overlay)
        descs = [item.desc for item in overlay.tree.layers["custom"].items]
        self.assertNotIn("Only on another platform", descs)
        self.assertIn("left out", screen)

    def test_the_entry_keys_do_not_take_the_navigation_keys(self):
        # The pool itself, not the sample of keys that happened to be
        # assigned. With one command the only key assigned is "a", so a
        # pool that held "/" would pass a test that looks at the assigned
        # keys -- and entry 63 would own the filter's key, and the
        # overlay looks the item up before the filter, so pressing "/"
        # would run that entry instead of opening the filter.
        from managr import custom as custom_mod

        self.assertNotIn("/", custom_mod.ENTRY_KEYS, "the pool took the filter's key")
        self.assertNotIn("?", custom_mod.ENTRY_KEYS, "the pool took the reference key")
        for key in custom_mod.ENTRY_KEYS:
            self.assertEqual(len(key), 1, "the entry key %r is not one character" % key)

    def test_the_entry_key_pool_has_no_duplicates(self):
        # A key used twice makes one of the two entries unreachable, and
        # the overlay looks the item up before the filter.
        from managr import custom as custom_mod

        keys = custom_mod.ENTRY_KEYS
        self.assertEqual(len(set(keys)), len(keys), "the pool has a duplicate")

    def test_more_entries_than_keys_leave_the_overflow_without_one(self):
        from managr import custom as custom_mod

        extra = 5
        results = dict(PLUGINS)
        results["plugin.action.list"] = {
            "actions": [
                {
                    "plugin_id": "plug%02d" % index,
                    "action_id": "act%02d" % index,
                    "title": "Action number %02d" % index,
                    "platforms": ["linux", "macos"],
                }
                for index in range(len(custom_mod.ENTRY_KEYS) + extra)
            ]
        }
        overlay, _ = self._overlay(results=results)
        self._open(overlay)
        items = overlay.tree.layers["custom"].items
        self.assertEqual(len(items), len(custom_mod.ENTRY_KEYS) + extra)
        keys = [item.key for item in items]
        # Every key that is there is unique.
        given = [key for key in keys if key]
        self.assertEqual(len(set(given)), len(given), "a key is used twice")
        self.assertEqual(len(given), len(custom_mod.ENTRY_KEYS))
        # And the entries past the pool have no key at all.
        self.assertEqual(len([key for key in keys if not key]), extra)

    def test_the_entries_past_the_pool_are_said_so(self):
        # They are listed -- the filter finds them -- but no key runs
        # them, and the layer has to say so rather than leave rows that
        # look runnable and are not.
        from managr import custom as custom_mod

        extra = 5
        results = dict(PLUGINS)
        results["plugin.action.list"] = {
            "actions": [
                {
                    "plugin_id": "plug%02d" % index,
                    "action_id": "act%02d" % index,
                    "title": "Action number %02d" % index,
                    "platforms": ["linux", "macos"],
                }
                for index in range(len(custom_mod.ENTRY_KEYS) + extra)
            ]
        }
        overlay, _ = self._overlay(results=results)
        self._open(overlay)
        screen = self._screen(overlay)
        self.assertIn("have no key", screen)
        self.assertIn("%d entries" % extra, screen)

    def test_every_entry_runs_an_action_from_the_map(self):
        overlay, _ = self._overlay(custom=[self._command(kind="shell")])
        self._open(overlay)
        items = overlay.tree.layers["custom"].items
        self.assertTrue(items)
        for item in items:
            self.assertIn(item.action, actions_mod.EXECUTORS, item.action)

    def test_a_failing_list_call_says_so_and_does_not_crash(self):
        from managr.client import HerdrUnavailable

        overlay, _ = self._overlay(
            errors={"plugin.action.list": HerdrUnavailable("no socket")}
        )
        self._open(overlay)
        screen = self._screen(overlay)
        self.assertIn("herdr unreachable", screen)
        # The overlay is still usable: the user has to be able to leave.
        overlay.handle(overlay_mod.Key("esc"))
        self.assertEqual(overlay.layer_id, "root")
        self.assertTrue(overlay.running)

    def test_no_line_is_wider_than_the_terminal(self):
        for columns in (20, 30, 40, 53, 80):
            overlay, _ = self._overlay(columns=columns)
            self._open(overlay)
            screen = self._screen(overlay)
            # The custom layer is on screen: the widths are the layer's.
            # The title is cut short in a narrow terminal, but its beginning
            # is always there.
            self.assertIn("Open the", screen)
            for line in screen.split("\r\n"):
                plain = ANSI.sub("", line)
                if not plain:
                    continue
                self.assertLessEqual(
                    len(plain),
                    columns - 1,
                    "a line is %d columns wide in a %d column terminal: %r"
                    % (len(plain), columns, line),
                )

    def test_every_line_ends_with_crlf(self):
        overlay, _ = self._overlay()
        self._open(overlay)
        out = io.StringIO()
        overlay.out = out
        overlay.render()
        # The custom layer is on screen: the lines are the layer's.
        self.assertIn("Open the layered keymap", ANSI.sub("", out.getvalue()))
        self.assertNotIn("\n", out.getvalue().replace("\r\n", ""))

    def test_the_overflow_line_is_not_wider_than_the_terminal(self):
        # The overflow line is a line of the frame like any other, and a
        # line as wide as the terminal wraps the cursor: every line after
        # it lands a column to the left.
        from managr import custom as custom_mod

        results = dict(PLUGINS)
        results["plugin.action.list"] = {
            "actions": [
                {
                    "plugin_id": "plug%02d" % index,
                    "action_id": "act%02d" % index,
                    "title": "Action number %02d" % index,
                    "platforms": ["linux", "macos"],
                }
                for index in range(len(custom_mod.ENTRY_KEYS) + 5)
            ]
        }
        for columns in (20, 30, 40, 53, 80):
            overlay, _ = self._overlay(results=results, columns=columns)
            self._open(overlay)
            screen = self._screen(overlay)
            # The line is there. In a narrow terminal it is cut short,
            # but its beginning is always there.
            self.assertIn("entries", screen)
            for line in screen.split("\r\n"):
                plain = ANSI.sub("", line)
                if not plain:
                    continue
                self.assertLessEqual(
                    len(plain),
                    columns - 1,
                    "a line is %d columns wide in a %d column terminal: %r"
                    % (len(plain), columns, line),
                )


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
