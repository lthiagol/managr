"""Tests for the v2 config mutator (the `[[keys.command]]` binding).

Same reason as v1: this edits a file the user cares about, so "does it put
their other custom commands back exactly" needs a test, not a hope.

Run: python3 tests/test_configure.py
"""

import contextlib
import io
import os
import shutil
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "scripts"))
sys.path.insert(0, os.path.join(ROOT, "src"))

import configure  # noqa: E402

ORIGINAL = """onboarding = false

[keys]
prefix = "ctrl+a"

[[keys.command]]
key = "prefix+alt+g"
type = "popup"
command = "lazygit"
description = "run lazygit"

[ui]
sidebar_collapsed_mode = "compact"

[theme]
name = "dracula"
"""


# The binding an earlier version of this plugin wrote: a `type = "popup"`
# entry naming a generated launcher. The upgrade has to replace it, and
# the uninstall has to remove it, so it is kept here as a fixture.
LEGACY = """
[[keys.command]]
key = "prefix+space"
type = "popup"
command = "%s"
width = "70%%"
height = "70%%"
description = "layered keymap"
"""


# The binding an earlier *name* of this plugin wrote. The plugin has been
# renamed more than once, and a binding left behind by an earlier name
# names the action of a plugin that does not exist any more. The upgrade
# has to replace it, and the uninstall has to remove it, so it is kept
# here as a fixture.
LEGACY_ID = """
[[keys.command]]
key = "prefix+space"
type = "plugin_action"
command = "herdr-chord.open"
description = "layered keymap"
"""


class ConfigCase(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.config = os.path.join(self.dir, "config.toml")
        self._env("HERDR_CONFIG_PATH", self.config)
        # The legacy binding named a launcher in the plugin's config dir,
        # so the path has to be predictable for the tests.
        self._env("HERDR_PLUGIN_CONFIG_DIR", self.dir)
        self.launcher = os.path.join(self.dir, "overlay.sh")

    def _env(self, name, value):
        previous = os.environ.get(name)
        os.environ[name] = value
        self.addCleanup(
            lambda: os.environ.__setitem__(name, previous)
            if previous
            else os.environ.pop(name, None)
        )

    def write(self, text):
        with open(self.config, "w", encoding="utf-8") as handle:
            handle.write(text)

    def read(self):
        with open(self.config, "r", encoding="utf-8") as handle:
            return handle.read()


class TestInstall(ConfigCase):
    def test_adds_the_binding(self):
        self.write(ORIGINAL)
        self.assertEqual(configure.do_install(), 0)
        text = self.read()
        # The binding invokes the plugin's own action, so the popup is the
        # plugin's pane -- and Herdr can put a title on that one.
        self.assertIn('type = "plugin_action"', text)
        self.assertIn('command = "managr.open"', text)
        self.assertIn('key = "prefix+space"', text)

    def test_the_binding_names_no_path(self):
        # A path inside the plugin directory moves when the plugin is
        # updated, and a path to a generated launcher needs a startup hook
        # to keep it in step. An action id is neither.
        self.write(ORIGINAL)
        configure.do_install()
        self.assertNotIn(self.launcher, self.read())

    def test_the_size_is_not_in_the_binding(self):
        # The size of the popup is in the manifest, next to the pane
        # entry, where Herdr reads it from.
        self.write(ORIGINAL)
        configure.do_install()
        self.assertNotIn("width =", self.read())
        self.assertNotIn("height =", self.read())

    def test_an_old_popup_binding_is_replaced(self):
        # An earlier version opened the popup with a `type = "popup"`
        # binding naming a generated launcher. Leaving it behind would
        # give the user two bindings for the same key.
        self.write(ORIGINAL + (LEGACY % self.launcher))
        configure.do_install()
        text = self.read()
        self.assertNotIn("overlay.sh", text)
        self.assertEqual(text.count("prefix+space"), 1)
        self.assertIn('type = "plugin_action"', text)

    def test_the_trigger_needs_no_shift(self):
        # A "/?" layout would make "prefix+?" ambiguous, so the default
        # trigger is a key that is the same on every layout.
        self.assertEqual(configure.TRIGGER_KEY, "prefix+space")
        self.assertNotIn("shift", configure.TRIGGER_KEY)

    def test_keeps_the_users_own_custom_command(self):
        self.write(ORIGINAL)
        configure.do_install()
        self.assertIn("lazygit", self.read())

    def test_is_idempotent(self):
        self.write(ORIGINAL)
        configure.do_install()
        first = self.read()
        configure.do_install()
        configure.do_install()
        self.assertEqual(first, self.read())
        self.assertEqual(self.read().count("managr.open"), 1)

    def test_takes_a_backup_once(self):
        self.write(ORIGINAL)
        configure.do_install()
        backup = self.config + configure.BACKUP_SUFFIX
        self.assertTrue(os.path.exists(backup))
        with open(backup, "r", encoding="utf-8") as handle:
            self.assertEqual(handle.read(), ORIGINAL)


class TestUninstall(ConfigCase):
    def test_restores_the_original_exactly(self):
        self.write(ORIGINAL)
        configure.do_install()
        self.assertEqual(configure.do_uninstall(), 0)
        self.assertEqual(self.read(), ORIGINAL)

    def test_keeps_other_custom_commands(self):
        self.write(ORIGINAL)
        configure.do_install()
        configure.do_uninstall()
        self.assertIn("lazygit", self.read())
        self.assertNotIn("managr.open", self.read())

    def test_an_old_popup_binding_is_removed_too(self):
        # The uninstall has to clean up the binding an earlier version
        # wrote, or the user is left with a binding for a key the plugin
        # no longer answers.
        self.write(ORIGINAL + (LEGACY % self.launcher))
        configure.do_uninstall()
        self.assertNotIn("overlay.sh", self.read())
        self.assertIn("lazygit", self.read())

    def test_uninstall_without_a_binding_is_fine(self):
        self.write(ORIGINAL)
        self.assertEqual(configure.do_uninstall(), 0)
        self.assertEqual(self.read(), ORIGINAL)

    def test_repeated_uninstall_is_safe(self):
        self.write(ORIGINAL)
        configure.do_install()
        configure.do_uninstall()
        once = self.read()
        configure.do_uninstall()
        self.assertEqual(once, self.read())


class TestTheRenamedBinding(ConfigCase):
    """A rename must not leave two bindings for the same key.

    The plugin id changed, so the binding the old id wrote points at the
    action of a plugin that does not exist. The install has to replace it
    rather than add a second one beside it, and the uninstall has to take
    it away.
    """

    def test_a_binding_of_an_earlier_id_is_replaced(self):
        self.write(ORIGINAL + LEGACY_ID)
        self.assertEqual(configure.do_install(), 0)
        text = self.read()
        self.assertNotIn("herdr-chord.open", text)
        self.assertIn('command = "%s"' % configure.ACTION_ID, text)
        self.assertEqual(text.count("prefix+space"), 1)

    def test_the_install_is_idempotent_after_a_rename(self):
        self.write(ORIGINAL + LEGACY_ID)
        configure.do_install()
        first = self.read()
        configure.do_install()
        self.assertEqual(first, self.read())
        self.assertEqual(self.read().count("%s" % configure.ACTION_ID), 1)

    def test_the_uninstall_removes_a_binding_of_an_earlier_id(self):
        self.write(ORIGINAL + LEGACY_ID)
        self.assertEqual(configure.do_uninstall(), 0)
        self.assertNotIn("herdr-chord.open", self.read())
        self.assertNotIn(configure.ACTION_ID, self.read())
        self.assertIn("lazygit", self.read())

    def test_the_status_names_a_stale_binding(self):
        # A binding of an earlier id is installed but dead, and the status
        # is where the user looks to find out.
        self.write(ORIGINAL + LEGACY_ID)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            configure.do_status()
        self.assertIn("stale binding", out.getvalue())
        self.assertIn("herdr-chord.open", out.getvalue())


class TestParsing(unittest.TestCase):
    def test_finds_the_table_by_command_not_position(self):
        blocks = configure.tables(
            [
                "[[keys.command]]",
                'key = "prefix+a"',
                'command = "other"',
                "[[keys.command]]",
                'key = "prefix+b"',
                'command = "%s"' % configure.ACTION_ID,
            ]
        )
        ours = configure.find_ours(blocks)
        self.assertIsNotNone(ours)
        self.assertEqual(configure._field(ours, "key"), "prefix+b")

    def test_finds_the_old_popup_binding_too(self):
        blocks = configure.tables(
            [
                "[[keys.command]]",
                'key = "prefix+space"',
                'command = "%s"' % configure.legacy_launcher_path(),
            ]
        )
        self.assertIsNotNone(configure.find_ours(blocks))

    def test_ignores_a_different_plugin(self):
        blocks = configure.tables(
            ["[[keys.command]]", 'command = "someone-else.open"']
        )
        self.assertIsNone(configure.find_ours(blocks))


class TestTheManifest(unittest.TestCase):
    """The manifest says what the code does.

    The manifest's comment used to describe a design that was reversed: a
    `prefix+?` trigger with the native panel moved to `prefix+shift+?`. A
    comment that describes a design the code does not have is worse than
    no comment, because it is read as the truth.
    """

    def setUp(self):
        path = os.path.join(ROOT, "herdr-plugin.toml")
        with open(path, "r", encoding="utf-8") as handle:
            self.text = handle.read()

    def test_the_manifest_documents_the_trigger_the_code_uses(self):
        self.assertIn('key = "%s"' % configure.TRIGGER_KEY, self.text)

    def test_the_manifest_does_not_document_the_reversed_design(self):
        # The design that was reversed was a `prefix+?` trigger with the
        # native panel moved to `prefix+shift+?`. The manifest may say
        # that it happened -- the history is worth keeping -- but it must
        # not tell the reader to do it.
        self.assertNotIn('key = "prefix+?"', self.text)
        self.assertNotIn('help = "prefix+shift+?"', self.text)

    def test_the_manifest_documents_the_binding_the_code_writes(self):
        # The manifest says what the code does. The binding invokes the
        # plugin's own action, so the popup is the plugin's pane and it
        # can be titled.
        self.assertIn('type = "plugin_action"', self.text)
        self.assertIn('command = "%s"' % configure.ACTION_ID, self.text)

    def test_the_manifest_titles_the_popup(self):
        # The title is what Herdr puts on the popup's frame. Without it
        # the floating window is called "popup".
        self.assertIn('title = "Managr"', self.text)
        self.assertIn('placement = "popup"', self.text)

    def test_the_popup_title_is_the_short_name(self):
        # The frame title is a label, not a sentence: the window is called
        # "Managr" and not "Managr for Herdr". The plugin's name in the
        # manifest is the longer one; the window's is the short one.
        self.assertNotIn('title = "Managr for Herdr"', self.text)

    def test_the_manifest_has_no_launcher_left(self):
        # Nothing writes a launcher any more: the plugin runtime runs the
        # pane's command with the plugin root as the working directory.
        self.assertNotIn("overlay.sh", self.text)
        self.assertNotIn("[[startup]]", self.text)


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
