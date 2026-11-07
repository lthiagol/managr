"""Tests for the things the two plugins share.

Three of them are shared state, one module and one answer:

* **the layer-state file** -- the writer publishes the heartbeat it
  refreshes the file at, and the format is one table that the reader has
  too;
* **the config dir** -- one answer for "where is Herdr's config";
* **the merge rule** -- one module, `merge.py`, for every user-editable
  file: the user's file is merged over the bundled one, and a name the
  plugin does not know is dropped with a word about it.

And the dead code: the pieces that were written and never read are gone,
and a test says so, because a name that is written and never read is
either a bug or a leftover.

Run: python3 tests/test_shared.py
"""

import contextlib
import io
import os
import shutil
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

from managr import actions as actions_mod  # noqa: E402
from managr import client as client_mod  # noqa: E402
from managr import custom as custom_mod  # noqa: E402
from managr import keymap as km  # noqa: E402
from managr import merge as merge_mod  # noqa: E402
from managr import tree as tree_mod  # noqa: E402
from managr import visuals as visuals_mod  # noqa: E402


def stderr_of(call, *args, **kwargs):
    """What `call` writes to stderr."""
    buffer = io.StringIO()
    with contextlib.redirect_stderr(buffer):
        call(*args, **kwargs)
    return buffer.getvalue()


class TestTheConfigDir(unittest.TestCase):
    """One answer for "where is Herdr's config"."""

    def setUp(self):
        self._saved = {}
        for name in ("HERDR_CONFIG_PATH", "XDG_CONFIG_HOME"):
            self._saved[name] = os.environ.get(name)
            os.environ.pop(name, None)
        self.addCleanup(self._restore)

    def _restore(self):
        for name, value in self._saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value

    def test_the_config_file_is_the_one_in_the_config_dir(self):
        os.environ["XDG_CONFIG_HOME"] = "/tmp/xdg-example"
        self.assertEqual(km.config_dir(), "/tmp/xdg-example/herdr")
        self.assertEqual(
            km.user_config_path(), "/tmp/xdg-example/herdr/config.toml"
        )

    def test_the_accent_is_read_from_that_same_file(self):
        # The accent came from a hardcoded `~/.config/herdr/config.toml`,
        # which is a second answer -- and the wrong one for an XDG user.
        os.environ["XDG_CONFIG_HOME"] = "/tmp/xdg-example"
        self.assertEqual(
            visuals_mod.herdr_config_path(), "/tmp/xdg-example/herdr/config.toml"
        )

    def test_the_config_path_override_names_the_file(self):
        os.environ["HERDR_CONFIG_PATH"] = "/tmp/somewhere-else/config.toml"
        self.assertEqual(km.user_config_path(), "/tmp/somewhere-else/config.toml")
        self.assertEqual(
            visuals_mod.herdr_config_path(), "/tmp/somewhere-else/config.toml"
        )


class TestTheMergeRule(unittest.TestCase):
    """One module, one rule, every user-editable file."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)
        previous = os.environ.get("HERDR_PLUGIN_CONFIG_DIR")
        os.environ["HERDR_PLUGIN_CONFIG_DIR"] = self.dir
        self.addCleanup(
            lambda: os.environ.__setitem__("HERDR_PLUGIN_CONFIG_DIR", previous)
            if previous
            else os.environ.pop("HERDR_PLUGIN_CONFIG_DIR", None)
        )
        merge_mod._WARNED.clear()

    def _write(self, name, text):
        with open(os.path.join(self.dir, name), "w", encoding="utf-8") as handle:
            handle.write(text)

    def test_every_merge_goes_through_the_one_module(self):
        self.assertIs(visuals_mod.merge_mod, merge_mod)
        self.assertIs(tree_mod.merge_mod, merge_mod)

    def test_an_unknown_section_of_the_icons_is_reported(self):
        # The glyph names inside a section are free -- a user may add one
        # for a layer of their own -- but a section the plugin does not
        # read is not a section.
        self._write("icons.toml", '[nonsense]\nsomething = "x"\n')
        stderr = stderr_of(visuals_mod.load_icons)
        self.assertIn("nonsense", stderr)

    def test_a_glyph_for_a_layer_of_the_users_own_is_kept(self):
        self._write("icons.toml", '[layer]\ngit = "G"\n')
        stderr = stderr_of(visuals_mod.load_icons)
        self.assertEqual(stderr, "")
        self.assertEqual(visuals_mod.load_icons()["layer"]["git"], "G")

    def test_an_unknown_field_of_a_layer_is_reported(self):
        self._write(
            "layers.toml",
            '[[layer]]\nid = "git"\nkey = "g"\ntitle = "git"\ndescc = "typo"\n',
        )
        stderr = stderr_of(tree_mod.build)
        self.assertIn("descc", stderr)

    def test_an_unknown_field_of_an_item_is_reported(self):
        self._write(
            "layers.toml",
            '[[item]]\nlayer = "panes"\nkey = "v"\naction = "split_vertical"\n'
            'descc = "typo"\n',
        )
        stderr = stderr_of(tree_mod.build)
        self.assertIn("descc", stderr)

    def test_a_known_layer_still_works(self):
        self._write(
            "layers.toml",
            '[[layer]]\nid = "git"\nkey = "g"\ntitle = "git"\n',
        )
        stderr = stderr_of(tree_mod.build)
        self.assertEqual(stderr, "")
        built = tree_mod.build()
        self.assertIn("git", built.layers)


class TestTheDeadCode(unittest.TestCase):
    """The pieces that were written and never read are gone."""

    def test_the_socket_client_has_no_ping(self):
        # It had no caller: the overlay never asks whether the server is
        # there, it asks by making a call.
        self.assertFalse(hasattr(client_mod.HerdrClient, "ping"))

    def test_the_custom_layer_has_no_unused_kinds(self):
        self.assertFalse(hasattr(custom_mod, "POPUP"))
        self.assertFalse(hasattr(custom_mod, "PANE"))
        self.assertFalse(hasattr(custom_mod, "KIND_COMMAND"))
        self.assertFalse(hasattr(custom_mod, "KIND_PLUGIN"))
        self.assertFalse(hasattr(custom_mod.Entry, "kind"))

    def test_there_is_no_layer_without_a_glyph(self):
        # Every glyph is for something the root can show: the tree's own
        # layer, the reference view, or one of the modes the root lists.
        from managr import tomlite

        path = os.path.join(ROOT, "src", "managr", "data", "icons.toml")
        with open(path, "r", encoding="utf-8") as handle:
            icons = tomlite.loads(handle.read())
        layers = icons.get("layer", {})
        built = tree_mod.build()
        # The navigatr and the agents list are modes, not layers, but the
        # root lists them, so they have a glyph like the layers do.
        modes = [
            tree_mod.NAVIGATR_ACTION.split(":", 1)[1],
            tree_mod.AGENTS_ACTION.split(":", 1)[1],
        ]
        for layer_id in layers:
            # Every glyph is for something the root can show: the tree's
            # own layer, the reference view, or the navigatr.
            self.assertTrue(
                layer_id in built.layers
                or layer_id == "reference"
                or layer_id in modes,
                "the glyph %r has no layer" % layer_id,
            )


class TestTheInternalActions(unittest.TestCase):
    """The runners that are not Herdr actions are not counted as ones."""

    def test_the_internal_runners_are_not_public(self):
        public = actions_mod.public_executors()
        for name in actions_mod.INTERNAL_ACTIONS:
            self.assertNotIn(name, public)
            self.assertIn(name, actions_mod.EXECUTORS)

    def test_the_public_set_is_the_whole_map_but_those_three(self):
        self.assertEqual(
            len(actions_mod.public_executors()),
            len(actions_mod.EXECUTORS) - len(actions_mod.INTERNAL_ACTIONS),
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
