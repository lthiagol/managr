"""Tests for the overlay's look: the accent, the palette and the glyphs.

The accent is the user's own: it comes from Herdr's theme, and the overlay
has to agree with the terminal the user is looking at. A colour that cannot
be read on either a dark or a light background is refused, because the
overlay cannot know what the popup is drawn over.

The glyphs are Nerd Font icons, and they are one column wide: an icon that
took two would push every row one column to the right.

Run: python3 tests/test_visuals.py
"""

import contextlib
import io
import os
import re
import shutil
import sys
import tempfile
import unicodedata
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

from managr import table as table_mod  # noqa: E402
from managr import tomlite  # noqa: E402
from managr import tree as tree_mod  # noqa: E402
from managr import visuals as visuals_mod  # noqa: E402


# The escape sequences a cell may hold: CSI (colours, cursor moves), OSC
# (window titles) and the two-byte ones.
#
# Written here rather than imported from the module: the width of a line is
# measured by this test, because a test that asks the code under test how
# wide its own output is proves nothing.
ANSI = re.compile(
    r"\x1b\[[0-?]*[ -/]*[@-~]"
    r"|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)"
    r"|\x1b[@-Z\\-_]"
)


def display_width(text):
    """The number of terminal columns `text` occupies, measured here.

    The module has `visible_width` for this, and it is tested separately in
    `test_table.py`, but the *width* assertions must not depend on it: a
    broken `visible_width` would then hide itself.
    """
    plain = ANSI.sub("", text)
    width = 0
    for char in plain:
        if unicodedata.combining(char):
            # A combining mark rides on the character before it.
            continue
        width += 2 if unicodedata.east_asian_width(char) in ("W", "F") else 1
    return width


def stderr_of(call):
    """What `call` writes to stderr."""
    buffer = io.StringIO()
    with contextlib.redirect_stderr(buffer):
        call()
    return buffer.getvalue()


class TestColourParsing(unittest.TestCase):
    def test_a_hex_colour(self):
        self.assertEqual(visuals_mod.parse_colour("#89b4fa"), (0x89, 0xB4, 0xFA))

    def test_a_short_hex_colour(self):
        self.assertEqual(visuals_mod.parse_colour("#abc"), (0xAA, 0xBB, 0xCC))

    def test_a_named_colour(self):
        self.assertIsNotNone(visuals_mod.parse_colour("cyan"))

    def test_an_rgb_colour(self):
        self.assertEqual(visuals_mod.parse_colour("rgb(1, 2, 3)"), (1, 2, 3))

    def test_nonsense_is_not_a_colour(self):
        self.assertIsNone(visuals_mod.parse_colour("chartreuse-ish"))
        self.assertIsNone(visuals_mod.parse_colour(""))
        self.assertIsNone(visuals_mod.parse_colour(None))

    def test_the_contrast_of_black_and_white(self):
        self.assertAlmostEqual(
            visuals_mod.contrast((0, 0, 0), (255, 255, 255)), 21.0, places=2
        )

    def test_the_contrast_of_a_colour_with_itself_is_one(self):
        self.assertAlmostEqual(
            visuals_mod.contrast((0x12, 0x34, 0x56), (0x12, 0x34, 0x56)), 1.0, places=5
        )


class TestAccentFromHerdrConfig(unittest.TestCase):
    """The accent comes from the user's own Herdr config."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.path = os.path.join(self.dir, "config.toml")
        previous = os.environ.get("HERDR_CONFIG_PATH")
        os.environ["HERDR_CONFIG_PATH"] = self.path
        self.addCleanup(
            lambda: os.environ.__setitem__("HERDR_CONFIG_PATH", previous)
            if previous
            else os.environ.pop("HERDR_CONFIG_PATH", None)
        )
        # The warnings are printed once per process, so the counter starts
        # at zero for every test.
        visuals_mod._WARNED.clear()

    def _write(self, text):
        with open(self.path, "w", encoding="utf-8") as handle:
            handle.write(text)

    def _accent(self, settings=None):
        return visuals_mod.resolve_palette(
            settings or visuals_mod.DEFAULT_SETTINGS
        )["accent"]

    def test_the_theme_accent_is_used(self):
        self._write('[theme.custom]\naccent = "#89b4fa"\n')
        self.assertEqual(self._accent(), (0x89, 0xB4, 0xFA))

    def test_the_ui_accent_is_used_when_there_is_no_theme_accent(self):
        self._write('[ui]\naccent = "cyan"\n')
        accent = self._accent()
        self.assertEqual(accent, visuals_mod.parse_colour("cyan"))

    def test_the_theme_accent_wins_over_the_ui_accent(self):
        self._write('[theme.custom]\naccent = "#89b4fa"\n\n[ui]\naccent = "cyan"\n')
        self.assertEqual(self._accent(), (0x89, 0xB4, 0xFA))

    def test_the_theme_name_gives_that_theme_accent(self):
        # A user who runs dracula gets dracula's own accent, without
        # configuring anything.
        self._write('[theme]\nname = "dracula"\n')
        self.assertEqual(self._accent(), (0xBD, 0x93, 0xF8))

    def test_the_plugin_setting_wins_over_everything(self):
        self._write('[theme.custom]\naccent = "#89b4fa"\n\n[theme]\nname = "dracula"\n')
        settings = dict(visuals_mod.DEFAULT_SETTINGS)
        settings["accent"] = "#123456"
        self.assertEqual(self._accent(settings), (0x12, 0x34, 0x56))

    def test_an_unreadable_accent_falls_back(self):
        # A colour in the narrow band that fails against *both* a dark and a
        # light background is unreadable on either, so it is refused.
        candidate = (0x7C, 0x7C, 0x7C)
        self.assertLess(
            visuals_mod.contrast(candidate, visuals_mod.DARK_BACKGROUND),
            visuals_mod.MINIMUM_CONTRAST,
            "the test's own premise is wrong: the colour reads on a dark background",
        )
        self.assertLess(
            visuals_mod.contrast(candidate, visuals_mod.LIGHT_BACKGROUND),
            visuals_mod.MINIMUM_CONTRAST,
            "the test's own premise is wrong: the colour reads on a light background",
        )
        self._write('[theme.custom]\naccent = "#7c7c7c"\n')
        colours = visuals_mod.resolve_palette(visuals_mod.DEFAULT_SETTINGS)
        self.assertEqual(
            colours["accent"], visuals_mod.parse_colour(visuals_mod.FALLBACK_ACCENT)
        )
        self.assertIn("built-in", colours["source"])

    def test_a_readable_accent_is_used_as_it_is(self):
        # It only has to work on one of the two backgrounds: the popup is
        # drawn over whatever the terminal shows.
        self._write('[theme.custom]\naccent = "#ff79c6"\n')
        colours = visuals_mod.resolve_palette(visuals_mod.DEFAULT_SETTINGS)
        self.assertEqual(colours["accent"], (0xFF, 0x79, 0xC6))

    def test_an_unreadable_accent_does_not_stop_the_chain(self):
        # The candidates are a chain. One that cannot be read is refused,
        # but the chain goes on: a readable candidate later in the list is
        # better than the built-in accent, and the user should get it.
        self._write(
            '[theme.custom]\naccent = "#7c7c7c"\n\n[theme]\nname = "dracula"\n'
        )
        buffer = io.StringIO()
        with contextlib.redirect_stderr(buffer):
            colours = visuals_mod.resolve_palette(visuals_mod.DEFAULT_SETTINGS)
        self.assertEqual(colours["accent"], (0xBD, 0x93, 0xF8))
        self.assertIn("dracula", colours["source"])
        # The user is told which candidate was refused.
        self.assertIn("#7c7c7c", buffer.getvalue())

    def test_a_colour_we_cannot_parse_is_skipped(self):
        self._write('[theme.custom]\naccent = "not a colour"\n\n[ui]\naccent = "#89b4fa"\n')
        self.assertEqual(self._accent(), (0x89, 0xB4, 0xFA))

    def test_the_loaded_palette_uses_the_config_accent(self):
        # The whole path, not just the resolver: this is what the overlay
        # actually loads.
        self._write('[theme.custom]\naccent = "#89b4fa"\n')
        loaded = visuals_mod.load()
        self.assertEqual(loaded.palette.accent_rgb, (0x89, 0xB4, 0xFA))
        self.assertIn("38;2;137;180;250", loaded.palette.accent)

    def test_a_missing_config_leaves_the_built_in_accent(self):
        self.assertEqual(
            self._accent(), visuals_mod.parse_colour(visuals_mod.FALLBACK_ACCENT)
        )


class TestPalette(unittest.TestCase):
    def test_the_palette_is_derived_from_the_accent(self):
        palette = visuals_mod.Palette((0xFF, 0x79, 0xC6))
        self.assertEqual(palette.accent_rgb, (0xFF, 0x79, 0xC6))
        self.assertIn("38;2;255;121;198", palette.accent)
        # The chords are a dimmed accent and the hints a muted one: one hue,
        # three brightnesses.
        self.assertLess(
            visuals_mod.luminance(palette.chord_rgb),
            visuals_mod.luminance(palette.accent_rgb),
        )
        self.assertNotEqual(palette.chord, palette.accent)
        self.assertNotEqual(palette.hint, palette.accent)

    def test_a_derived_colour_stays_readable(self):
        palette = visuals_mod.Palette((0xFF, 0x79, 0xC6))
        for rgb in (palette.chord_rgb, palette.hint_rgb):
            self.assertTrue(
                visuals_mod.contrast(rgb, visuals_mod.DARK_BACKGROUND) >= 3.0
                or visuals_mod.contrast(rgb, visuals_mod.LIGHT_BACKGROUND) >= 3.0,
                "%r cannot be read on either background" % (rgb,),
            )


class TestSettingsMerge(unittest.TestCase):
    """The user's file merges over the bundled one."""

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

    def _write(self, name, text):
        with open(os.path.join(self.dir, name), "w", encoding="utf-8") as handle:
            handle.write(text)

    def test_the_bundled_defaults_apply_without_a_user_file(self):
        settings = visuals_mod.load_settings()
        self.assertEqual(settings["icons"], True)
        self.assertEqual(settings["frame"], True)
        self.assertEqual(settings["accent"], "")
        self.assertEqual(settings["theme"], "")

    def test_one_setting_can_be_overridden_on_its_own(self):
        self._write("visuals.toml", "frame = false\n")
        settings = visuals_mod.load_settings()
        self.assertEqual(settings["frame"], False)
        # The rest of the bundled file still applies.
        self.assertEqual(settings["icons"], True)

    def test_one_glyph_can_be_overridden_on_its_own(self):
        self._write("icons.toml", '[layer]\npanes = "P"\n')
        icons = visuals_mod.load_icons()
        self.assertEqual(icons["layer"]["panes"], "P")
        # The other glyphs are still the bundled ones.
        self.assertTrue(icons["layer"]["tabs"])

    def test_the_icons_can_be_turned_off(self):
        self._write("visuals.toml", "icons = false\n")
        loaded = visuals_mod.load()
        self.assertFalse(loaded.use_icons)
        self.assertEqual(loaded.layer_icon("panes"), "")
        self.assertEqual(loaded.group_icon("pane"), "")
        self.assertEqual(loaded.marker("branch"), "")

    def test_the_icons_are_on_by_default(self):
        loaded = visuals_mod.load()
        self.assertTrue(loaded.use_icons)
        self.assertTrue(loaded.layer_icon("panes"))
        self.assertTrue(loaded.group_icon("pane"))
        self.assertEqual(loaded.marker("branch"), "▾")

    def test_a_folded_space_has_a_marker_of_its_own(self):
        # The marker of a folded space points the other way, so the space
        # says that its rows are hidden without a word.
        loaded = visuals_mod.load()
        self.assertEqual(loaded.marker("collapsed"), "▸")
        self.assertNotEqual(loaded.marker("collapsed"), loaded.marker("branch"))


class TestLayerGlyphs(unittest.TestCase):
    """Every layer the tree declares has a glyph.

    The custom layer is the odd one out -- its rows are fetched, not
    declared -- and it used to have no glyph, so its row in the root was
    the only layer row drawn without one.
    """

    def test_every_layer_has_a_glyph(self):
        icons = visuals_mod.load_icons()["layer"]
        path = os.path.join(ROOT, "src", "managr", "data", "layers.toml")
        with open(path, "r", encoding="utf-8") as handle:
            definition = tomlite.loads(handle.read())
        layers = [
            entry.get("id")
            for entry in definition.get("layer", [])
            if isinstance(entry, dict)
        ]
        self.assertTrue(layers, "the tree declares no layers at all")
        for layer_id in layers:
            self.assertTrue(icons.get(layer_id), "the layer %r has no glyph" % layer_id)

    def test_the_custom_layer_has_a_glyph(self):
        self.assertTrue(visuals_mod.load_icons()["layer"].get("custom"))


class TestGlyphWidth(unittest.TestCase):
    """A Nerd Font glyph is one column wide."""

    def test_a_glyph_is_one_column(self):
        glyph = visuals_mod.load_icons()["layer"]["panes"]
        self.assertTrue(glyph)
        # Measured here, not with the module's own function: a test that
        # asks the code under test how wide its own output is proves
        # nothing.
        self.assertEqual(display_width(glyph), 1)

    def test_a_row_with_a_glyph_is_not_wider_than_it_should_be(self):
        glyph = visuals_mod.load_icons()["layer"]["panes"]
        columns = [
            table_mod.Column(name="key"),
            table_mod.Column(name="icon"),
            table_mod.Column(name="desc"),
        ]
        rows = [["n", glyph, "navigation"]]
        lines = table_mod.render(rows, columns, 80, indent="  ")
        # 2 (indent) + 1 (key) + 2 (gap) + 1 (glyph) + 2 (gap) + 10 (desc)
        self.assertEqual(display_width(lines[0]), 18)


class TestBundledDefaults(unittest.TestCase):
    """The bundled file is where the defaults come from."""

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
        visuals_mod._WARNED.clear()

    def test_every_bundled_setting_survives_into_the_merged_settings(self):
        # The bundled `visuals.toml` is the single source of truth for the
        # defaults: it is read, not duplicated in the code. A setting added
        # there has to reach the overlay, or the file is decoration -- and
        # this test is what says so.
        path = visuals_mod.bundled_path(visuals_mod.SETTINGS_FILE)
        with open(path, "r", encoding="utf-8") as handle:
            bundled = tomlite.loads(handle.read())
        self.assertTrue(bundled, "the bundled file is empty")
        settings = visuals_mod.load_settings()
        for key, value in bundled.items():
            self.assertIn(key, settings, "%r is in the bundled file" % key)
            self.assertEqual(settings[key], value, "%r" % key)

    def test_the_defaults_are_the_bundled_values(self):
        # The same thing, from the other side: the defaults are not written
        # out in the code as well.
        path = visuals_mod.bundled_path(visuals_mod.SETTINGS_FILE)
        with open(path, "r", encoding="utf-8") as handle:
            bundled = tomlite.loads(handle.read())
        for key, value in bundled.items():
            self.assertEqual(visuals_mod.DEFAULT_SETTINGS.get(key), value, "%r" % key)


class TestUnknownSettings(unittest.TestCase):
    """A setting the plugin does not know is reported, not ignored quietly."""

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
        # The warnings are printed once per process, so the counter starts
        # at zero for every test.
        visuals_mod._WARNED.clear()

    def _write(self, text):
        with open(os.path.join(self.dir, "visuals.toml"), "w", encoding="utf-8") as handle:
            handle.write(text)

    def test_a_misspelled_setting_is_reported(self):
        # `icon` is not `icons`: without a word the user thinks the glyphs
        # are off, and they are not.
        self._write("icon = true\n")
        stderr = stderr_of(visuals_mod.load_settings)
        self.assertIn("icon", stderr)
        # The warning names the file it came from.
        self.assertIn(os.path.join(self.dir, "visuals.toml"), stderr)
        settings = visuals_mod.load_settings()
        self.assertNotIn("icon", settings)

    def test_the_warning_is_printed_once(self):
        # The overlay renders on every keystroke: a message on every
        # keystroke is noise.
        self._write("icon = true\n")
        first = stderr_of(visuals_mod.load_settings)
        second = stderr_of(visuals_mod.load_settings)
        self.assertIn("icon", first)
        self.assertEqual(second, "")

    def test_a_setting_of_the_wrong_type_is_reported(self):
        # `icons = "no"` is a truthy string: it would turn the glyphs *on*
        # when the user meant to turn them off.
        self._write('icons = "no"\n')
        stderr = stderr_of(visuals_mod.load_settings)
        self.assertIn("icons", stderr)
        settings = visuals_mod.load_settings()
        self.assertEqual(settings["icons"], True)

    def test_a_known_setting_is_not_reported(self):
        self._write("icons = false\n")
        self.assertEqual(stderr_of(visuals_mod.load_settings), "")
        self.assertFalse(visuals_mod.load_settings()["icons"])


class TestTheStatusColours(unittest.TestCase):
    """The status is the thing the eye should go to.

    Everything else in the overlay is one accent hue, which is calm but
    flat. The agent's status is the exception: a blocked agent wants the
    user now, so it is in the error colour, and the quiet ones are muted
    so that they do not shout.
    """

    def _visuals(self):
        palette = visuals_mod.Palette(visuals_mod.parse_colour("#5fd7ff"))
        return visuals_mod.Visuals(
            visuals_mod.DEFAULT_SETTINGS, visuals_mod.load_icons(), palette
        )

    def test_the_status_is_coloured_by_severity(self):
        visuals = self._visuals()
        palette = visuals.palette
        self.assertEqual(visuals.status_colour("blocked"), palette.err)
        self.assertEqual(visuals.status_colour("done"), palette.ok)
        self.assertEqual(visuals.status_colour("idle"), palette.hint)
        self.assertEqual(visuals.status_colour("unknown"), palette.hint)

    def test_a_working_agent_is_not_muted(self):
        # The quiet colours are the ones that wait for something. An agent
        # that is working is busy, and it is not one of them -- and it is
        # not an alarm either: it is the state you notice without it
        # being a problem.
        visuals = self._visuals()
        palette = visuals.palette
        self.assertNotEqual(visuals.status_colour("working"), palette.hint)
        self.assertNotEqual(visuals.status_colour("working"), palette.err)

    def test_a_status_that_is_not_one_of_the_five_is_muted(self):
        visuals = self._visuals()
        self.assertEqual(
            visuals.status_colour("something else"), visuals.palette.hint
        )


class TestTheKindColours(unittest.TestCase):
    """The kind marker is coloured by how deep the row is.

    The tree's shape already says how deep a row is, so the marker does
    not have to: the deeper the row, the brighter the marker, so the
    leaves -- the things you can jump to -- stand out and the structure
    recedes.
    """

    def _visuals(self):
        palette = visuals_mod.Palette(visuals_mod.parse_colour("#5fd7ff"))
        return visuals_mod.Visuals(
            visuals_mod.DEFAULT_SETTINGS, visuals_mod.load_icons(), palette
        )

    def test_the_kind_is_coloured_by_depth(self):
        visuals = self._visuals()
        palette = visuals.palette
        self.assertEqual(visuals.kind_colour("workspace"), palette.hint)
        self.assertEqual(visuals.kind_colour("tab"), palette.chord)
        self.assertEqual(visuals.kind_colour("pane"), palette.accent)

    def test_a_kind_that_is_not_one_of_the_three_is_muted(self):
        visuals = self._visuals()
        self.assertEqual(
            visuals.kind_colour("something else"), visuals.palette.hint
        )


class TestOverlayUsesTheVisuals(unittest.TestCase):
    """The overlay draws what the visuals say."""

    def _render(self, layer_id="root", columns=80, visuals=None):
        from managr import actions as actions_mod
        from managr import keymap as keymap_mod
        from managr import overlay as overlay_mod

        keymap = keymap_mod.Keymap(prefix="C-a", bindings={})
        tree = tree_mod.build(keymap=keymap, labels={})
        overlay = overlay_mod.Overlay(tree, actions_mod.Context(None), visuals=visuals)
        buffer = io.StringIO()
        overlay.out = buffer
        overlay.layer_id = layer_id
        overlay.stack = [layer_id] if layer_id != "root" else []
        overlay._size = lambda: (columns, 24)
        overlay.render()
        return buffer.getvalue()

    def test_the_frame_is_drawn(self):
        out = self._render()
        plain = table_mod.strip_ansi(out)
        self.assertIn("╭", plain)
        self.assertIn("╮", plain)
        self.assertIn("╰", plain)
        self.assertIn("╯", plain)

    def test_the_frame_can_be_turned_off(self):
        settings = dict(visuals_mod.DEFAULT_SETTINGS)
        settings["frame"] = False
        loaded = visuals_mod.Visuals(
            settings, visuals_mod.load_icons(), visuals_mod.Palette((0xFF, 0x79, 0xC6))
        )
        out = self._render(visuals=loaded)
        plain = table_mod.strip_ansi(out)
        self.assertNotIn("╭", plain)

    def test_the_layer_entries_carry_a_glyph(self):
        out = self._render()
        plain = table_mod.strip_ansi(out)
        icons = visuals_mod.load_icons()["layer"]
        self.assertIn(icons["panes"], plain)

    def test_the_glyphs_can_be_turned_off(self):
        settings = dict(visuals_mod.DEFAULT_SETTINGS)
        settings["icons"] = False
        loaded = visuals_mod.Visuals(
            settings, visuals_mod.load_icons(), visuals_mod.Palette((0xFF, 0x79, 0xC6))
        )
        out = self._render(visuals=loaded)
        plain = table_mod.strip_ansi(out)
        icons = visuals_mod.load_icons()["layer"]
        self.assertNotIn(icons["panes"], plain)

    def test_the_accent_colour_is_used(self):
        loaded = visuals_mod.Visuals(
            visuals_mod.DEFAULT_SETTINGS,
            visuals_mod.load_icons(),
            visuals_mod.Palette((0xFF, 0x79, 0xC6)),
        )
        out = self._render(visuals=loaded)
        self.assertIn("38;2;255;121;198", out)


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
