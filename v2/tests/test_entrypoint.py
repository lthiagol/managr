"""The entry point must actually run.

`live_check.py` imports the library directly, so it never exercises
`bin/managr` -- the file Herdr launches. A rename that missed the entry
point (it has no `.py` extension, so a `--include='*.py'` sweep skips it) left
the whole plugin unable to start while every library test stayed green.

This runs the entry point as a subprocess, the way Herdr does.

Run: python3 tests/test_entrypoint.py
"""

import importlib.machinery
import importlib.util
import os
import subprocess
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))
ENTRY = os.path.join(ROOT, "bin", "managr")


def load_entry_point():
    """The entry point as a module.

    It has no `.py` extension, so it is loaded the way Python loads a
    script: by path.
    """
    loader = importlib.machinery.SourceFileLoader("managr_entry", ENTRY)
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


class TestEntryPoint(unittest.TestCase):
    def test_the_entry_point_exists_and_is_executable(self):
        self.assertTrue(os.path.exists(ENTRY), "%s is missing" % ENTRY)
        self.assertTrue(os.access(ENTRY, os.X_OK), "%s is not executable" % ENTRY)

    def _run(self, *args):
        return subprocess.run(
            [sys.executable, ENTRY] + list(args),
            capture_output=True,
            text=True,
            cwd=ROOT,
        )

    def test_tree_prints_the_layers(self):
        result = self._run("tree")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("navigation", result.stdout)
        self.assertIn("panes", result.stdout)
        self.assertNotIn("ModuleNotFoundError", result.stderr)

    def test_explain_lists_the_executors(self):
        result = self._run("explain")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("executable actions", result.stdout)
        self.assertNotIn("ModuleNotFoundError", result.stderr)

    def test_the_package_imports_under_its_current_name(self):
        # The entry point must import the package that actually exists. A
        # rename that misses either side fails here loudly.
        here = os.path.join(ROOT, "src")
        proc = subprocess.run(
            [
                sys.executable,
                "-c",
                "import managr, managr.overlay, managr.table, "
                "managr.actions, managr.client, managr.tree; "
                "print('ok')",
            ],
            capture_output=True,
            text=True,
            cwd=ROOT,
            env=dict(os.environ, PYTHONPATH=here),
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("ok", proc.stdout)

    def test_the_manifests_command_resolves_the_package(self):
        # The plugin runtime runs the manifest's pane command with the
        # plugin root as the working directory. That command is what
        # opens the popup, so it is a second thing that can break -- and
        # it is a relative path, so the working directory matters.
        with open(os.path.join(ROOT, "herdr-plugin.toml"), "r", encoding="utf-8") as handle:
            manifest = handle.read()
        self.assertIn('command = ["python3", "bin/managr", "overlay"]', manifest)
        proc = subprocess.run(
            [sys.executable, os.path.join("bin", "managr"), "overlay"],
            capture_output=True,
            text=True,
            cwd=ROOT,
            stdin=subprocess.DEVNULL,
        )
        output = proc.stdout + proc.stderr
        # The overlay needs a terminal. Reaching that message proves every
        # import resolved; a rename that missed the package dies with
        # ModuleNotFoundError long before it.
        self.assertNotIn("ModuleNotFoundError", output)
        self.assertNotIn("ImportError", output)
        self.assertIn("needs a terminal", output)

    def test_the_manifest_has_no_startup_hook(self):
        # The startup hook used to write a launcher for the popup binding.
        # The binding invokes the plugin's own action now, so the plugin
        # runtime runs the pane's command and there is nothing to write.
        with open(os.path.join(ROOT, "herdr-plugin.toml"), "r", encoding="utf-8") as handle:
            manifest = handle.read()
        self.assertNotIn("[[startup]]", manifest)


class TestThePluginId(unittest.TestCase):
    """The entry point asks Herdr for the plugin that exists.

    The id used to be `herdr-chord`, left over from a rename: with the
    variable unset, the `open` action asked Herdr for a plugin that does
    not exist. This plugin has been renamed more than once -- `herdr-nextkey`,
    `herdr-layers`, `herdr-keybar`, `herdr-chord` -- and every one of those
    names would break it the same way, so every one of them is refused.
    """

    # Every name this plugin has been published under.
    LEGACY_IDS = (
        "herdr-nextkey",
        "herdr-layers",
        "herdr-keybar",
        "herdr-chord",
    )

    def setUp(self):
        self.manifest = os.path.join(ROOT, "herdr-plugin.toml")
        with open(self.manifest, "r", encoding="utf-8") as handle:
            self.text = handle.read()

    def _manifest_id(self):
        for line in self.text.splitlines():
            if line.startswith("id = "):
                return line.split("=", 1)[1].strip().strip('"')
        return ""

    def test_the_default_plugin_id_is_the_manifest_id(self):
        previous = os.environ.pop("HERDR_PLUGIN_ID", None)
        try:
            module = load_entry_point()
        finally:
            if previous is not None:
                os.environ["HERDR_PLUGIN_ID"] = previous
        self.assertEqual(module.PLUGIN_ID, self._manifest_id())

    def test_the_entry_point_calls_itself_by_the_right_name(self):
        with open(ENTRY, "r", encoding="utf-8") as handle:
            text = handle.read()
        for legacy in self.LEGACY_IDS:
            self.assertNotIn(legacy, text)


class TestTheExplainCount(unittest.TestCase):
    """`make explain` counts the actions, not the runners.

    Three of the executors are not Herdr actions: they run a plugin's
    action, a shell command and a window command. Counting them as
    actions said the overlay could execute forty Herdr actions when it
    can execute thirty-seven.
    """

    def test_the_count_is_of_the_public_actions(self):
        from managr import actions as actions_mod

        result = subprocess.run(
            [sys.executable, ENTRY, "explain"],
            capture_output=True,
            text=True,
            cwd=ROOT,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        expected = len(actions_mod.public_executors())
        self.assertIn("executable actions (%d):" % expected, result.stdout)
        self.assertNotIn("executable actions (40):", result.stdout)

    def test_the_internal_runners_are_listed_apart(self):
        from managr import actions as actions_mod

        result = subprocess.run(
            [sys.executable, ENTRY, "explain"],
            capture_output=True,
            text=True,
            cwd=ROOT,
        )
        self.assertIn("internal runners", result.stdout)
        for name in actions_mod.INTERNAL_ACTIONS:
            self.assertIn(name, result.stdout)


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
