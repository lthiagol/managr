"""The overlay's look: the accent colour, the palette and the glyphs.

Herdr strips ANSI from its *status* entries, but the overlay lives in a popup,
which is a real terminal: colour works there. The question is which colour.
The answer is the one the user already chose for Herdr -- the accent of their
own theme -- so the overlay reads as part of their terminal rather than as a
plugin with its own idea of what cyan means.

Everything else is derived from that accent: the chords are a dimmed variant,
the hints a muted one. A palette that is one hue reads as a palette; a palette
that mixes cyan, magenta and grey reads as a mistake.

**The background is unknown.** The popup is drawn over whatever the terminal
shows, and no plugin can ask. So an accent is checked against a dark and a
light background before it is used: a colour that fails 4.5:1 against *both*
is unreadable on either, so it is refused -- the next candidate is tried, and
the user is told, once, on stderr. An accent that works on one of the two is
used as it is.

**The glyphs are Nerd Font icons** (v3 codepoints), in the private use area.
They are one column wide -- `table.char_width` counts the private use area as
one, because a terminal that cannot draw the glyph shows a box anyway. They
are opt-out: a user whose font does not have them sets `icons = false`.

The settings live in `visuals.toml` and the glyphs in `icons.toml`, both in
the plugin's config dir, and both merged over the bundled copies: a user can
change one setting without copying the whole file. The bundled `visuals.toml`
is where the defaults come from -- it is read, not duplicated in the code --
and a setting the plugin does not know is dropped with a word on stderr,
because a misspelled setting that is ignored in silence looks exactly like a
setting that did not work.
"""

import os
import sys

from . import keymap as keymap_mod
from . import merge as merge_mod
from . import tomlite

# The name the diagnostics are prefixed with.
PLUGIN = "managr"

# The escape sequences the overlay writes.
RESET = "\x1b[0m"
BOLD = "\x1b[1m"
DIM = "\x1b[2m"

# The two backgrounds the accent is checked against. The popup is drawn over
# whatever the terminal shows, so a colour has to work on both.
DARK_BACKGROUND = (0x19, 0x1D, 0x24)
LIGHT_BACKGROUND = (0xFD, 0xFD, 0xFD)

# The contrast a colour needs to be readable as text.
MINIMUM_CONTRAST = 4.5

# The contrast a derived colour needs. The chords and the hints are secondary
# to the keys, but they still have to be readable.
MINIMUM_DERIVED_CONTRAST = 3.0

# The accent used when nothing else is known: the cyan the overlay used to
# hardcode, so an installation with no Herdr theme still looks like before.
FALLBACK_ACCENT = "#5fd7ff"

# The semantic colours, used when the theme does not say what they are.
# They are not derived from the accent: green and red mean "it worked" and
# "it did not", and that meaning is worth more than a consistent hue.
OK_COLOUR = (0x3F, 0xB9, 0x50)
ERR_COLOUR = (0xF8, 0x51, 0x49)
# Yellow is the third status colour, and it is the one Herdr itself uses
# for an agent that is working -- so it is what the overlay uses too.
WARN_COLOUR = (0xE3, 0xB3, 0x41)

# The palettes of the themes Herdr ships, so a user who runs one of them
# gets that theme's colours without configuring anything.
#
# Herdr's built-in themes are compiled into its binary, so there is nothing
# to read them from: the copy here is ours, and it can drift. Two things
# keep it honest. The user's own `[theme.custom]` colours win over it (that
# is the setting Herdr itself reads), and the plugin's own `visuals.toml`
# wins over everything.
#
# `dracula` is the one that was MEASURED: sampled out of Herdr's own
# rendering of its "Go to" surface, which is what the accent has to match
# for the overlay to look like part of Herdr rather than a stranger in it.
THEME_PALETTES = {
    "catppuccin": {
        "background": "#1e1e2e", "foreground": "#cdd6f4", "accent": "#cba6f7",
        "muted": "#9399b2", "ok": "#a6e3a1", "err": "#f38ba8", "warn": "#f9e2af",
    },
    "catppuccin-latte": {
        "background": "#eff1f5", "foreground": "#4c4f69", "accent": "#8839ef",
        "muted": "#6c6f85", "ok": "#40a02b", "err": "#d20f39", "warn": "#df8e1d",
    },
    "dracula": {
        "background": "#282a36", "foreground": "#f8f8f2", "accent": "#bd93f8",
        "muted": "#6272a4", "ok": "#50fa7b", "err": "#ff5555", "warn": "#f1fa8c",
    },
    "nord": {
        "background": "#2e3440", "foreground": "#d8dee9", "accent": "#88c0d0",
        "muted": "#4c566a", "ok": "#a3be8c", "err": "#bf616a", "warn": "#ebcb8b",
    },
    "gruvbox": {
        "background": "#282828", "foreground": "#ebdbb2", "accent": "#d3869b",
        "muted": "#928374", "ok": "#b8bb26", "err": "#fb4934", "warn": "#fabd2f",
    },
    "tokyo-night": {
        "background": "#1a1b26", "foreground": "#c0caf5", "accent": "#7aa2f7",
        "muted": "#565f89", "ok": "#9ece6a", "err": "#f7768e", "warn": "#e0af68",
    },
    "one-dark": {
        "background": "#282c34", "foreground": "#abb2bf", "accent": "#c678dd",
        "muted": "#5c6370", "ok": "#98c379", "err": "#e06c75", "warn": "#e5c07b",
    },
    "solarized": {
        "background": "#002b36", "foreground": "#839496", "accent": "#d33682",
        "muted": "#586e75", "ok": "#859900", "err": "#dc322f", "warn": "#b58900",
    },
    "kanagawa": {
        "background": "#1f1f28", "foreground": "#dcd7ba", "accent": "#957fb8",
        "muted": "#727169", "ok": "#98bb6c", "err": "#e82424", "warn": "#e6c384",
    },
    "rose-pine": {
        "background": "#191724", "foreground": "#e0def4", "accent": "#c4a7e7",
        "muted": "#6e6a86", "ok": "#9ccfd8", "err": "#eb6f92", "warn": "#f6c177",
    },
    "vesper": {
        "background": "#101010", "foreground": "#eeeeee", "accent": "#a599e9",
        "muted": "#666666", "ok": "#99ff99", "err": "#ff6666", "warn": "#ffcc66",
    },
    # The terminal's own colours: there is no palette to copy, so only the
    # accent is known and the rest is derived from it.
    "terminal": {"accent": "#5fd7ff"},
}

# The named colours a terminal understands. Herdr's own config accepts a name
# as well as a hex, so the overlay has to as well.
NAMED_COLOURS = {
    "black": "#000000",
    "red": "#cd0000",
    "green": "#00cd00",
    "yellow": "#cdcd00",
    "blue": "#0000ee",
    "magenta": "#cd00cd",
    "cyan": "#00cdcd",
    "white": "#e5e5e5",
    "gray": "#7f7f7f",
    "grey": "#7f7f7f",
    "brightblack": "#7f7f7f",
    "brightred": "#ff0000",
    "brightgreen": "#00ff00",
    "brightyellow": "#ffff00",
    "brightblue": "#5c5cff",
    "brightmagenta": "#ff00ff",
    "brightcyan": "#00ffff",
    "brightwhite": "#ffffff",
    "orange": "#ffaf00",
    "purple": "#af00ff",
    "pink": "#ff87af",
}

# The settings the plugin knows.
#
# The bundled `visuals.toml` supplies the *values* -- it is the single
# source of truth for the defaults, read rather than duplicated in the
# code -- but the *names* have to be here: the code has to know which
# settings it reads, and a key that is not in this list is not a setting at
# all. A setting added to the bundled file without being added here is
# dropped, and the test that says every bundled setting survives says so.
KNOWN_SETTINGS = ("accent", "icons", "frame", "theme")

# The sections of `icons.toml` the plugin reads. A section the user names
# that is not one of these is dropped with a word; the glyph *names* inside
# a section are free, because a user may add a glyph for a layer of their
# own.
KNOWN_ICON_SECTIONS = ("layer", "group", "kind", "marker", "status")

# Where the settings and the glyphs live.
SETTINGS_FILE = "visuals.toml"
ICONS_FILE = "icons.toml"

# The warnings are printed once per process: the overlay renders on every
# keystroke, and a message on every keystroke is noise.
#
# The set itself is the merge module's, so there is one place that
# remembers what has been said -- the merge warnings go through the same
# one.
_WARNED = merge_mod._WARNED


def _warn_once(key, message):
    if key in _WARNED:
        return
    _WARNED.add(key)
    sys.stderr.write("managr: %s\n" % message)


# -- colour ------------------------------------------------------------------


def _channel(value):
    """The linearised value of one colour channel."""
    scaled = value / 255.0
    if scaled <= 0.03928:
        return scaled / 12.92
    return ((scaled + 0.055) / 1.055) ** 2.4


def luminance(rgb):
    """The relative luminance of a colour, per WCAG."""
    red, green, blue = (_channel(value) for value in rgb)
    return 0.2126 * red + 0.7152 * green + 0.0722 * blue


def contrast(first, second):
    """The contrast ratio between two colours, from 1 to 21."""
    high = luminance(first)
    low = luminance(second)
    if high < low:
        high, low = low, high
    return (high + 0.05) / (low + 0.05)


def parse_colour(text):
    """(red, green, blue) for a hex, `rgb(...)` or named colour, else None."""
    if not isinstance(text, str):
        return None
    value = text.strip()
    if not value:
        return None
    if value.startswith("#"):
        digits = value[1:]
        if len(digits) == 3:
            digits = "".join(char * 2 for char in digits)
        if len(digits) != 6:
            return None
        try:
            return tuple(int(digits[index : index + 2], 16) for index in (0, 2, 4))
        except ValueError:
            return None
    if value.lower().startswith("rgb(") and value.endswith(")"):
        parts = value[4:-1].split(",")
        if len(parts) != 3:
            return None
        try:
            return tuple(max(0, min(255, int(part.strip()))) for part in parts)
        except ValueError:
            return None
    named = NAMED_COLOURS.get(value.lower())
    if named:
        return parse_colour(named)
    return None


def foreground(rgb):
    """The escape sequence that prints text in this colour."""
    return "\x1b[38;2;%d;%d;%dm" % tuple(rgb)


def _mix(first, second, amount):
    """`first` moved `amount` of the way towards `second`."""
    return tuple(
        int(round(first[index] + (second[index] - first[index]) * amount))
        for index in range(3)
    )


def _readable_everywhere(rgb, minimum=MINIMUM_DERIVED_CONTRAST):
    """True when the colour can be read on a dark *or* a light background."""
    return (
        contrast(rgb, DARK_BACKGROUND) >= minimum
        or contrast(rgb, LIGHT_BACKGROUND) >= minimum
    )


def derive(accent, towards, amount):
    """A variant of the accent: dimmed towards black, muted towards grey.

    A variant that cannot be read on either background is no use, so one that
    fails both keeps the accent's own brightness.
    """
    variant = _mix(accent, towards, amount)
    if not _readable_everywhere(variant):
        return accent
    return variant


class Palette:
    """The colours the overlay writes.

    `accent` is the user's own: the key column, the frame's left edge and the
    input prompt. `chord` is the dimmed accent and `hint` the muted one, so
    the whole overlay is one hue with a clear hierarchy.

    The rest comes from Herdr's theme when the theme says what they are. A
    theme has a muted colour of its own (dracula's is its "comment" grey),
    and the status colours are its green, its red and its yellow -- the same
    ones Herdr draws in its own sidebar. A palette that is all one hue is
    calm, but it is not the theme the user is looking at.
    """

    def __init__(self, colours, source=""):
        self.source = source
        if not isinstance(colours, dict):
            # A bare colour is the accent, and everything else is derived
            # from it: that is the palette of a session whose theme says
            # nothing about the rest.
            colours = {"accent": tuple(colours)}
        self.accent_rgb = tuple(colours["accent"])
        self.chord_rgb = derive(self.accent_rgb, (0, 0, 0), 0.25)
        # The theme's muted colour when it has one, and the dimmed accent
        # when it does not: the hint is secondary, so it has to be readable
        # on either background.
        self.hint_rgb = colours.get("muted") or derive(
            self.accent_rgb, (0x80, 0x80, 0x80), 0.45
        )
        # The colour the panel is painted on. Herdr's own "Go to" surface
        # uses the theme's background, so the overlay can use it too -- and
        # the text on the selected row is this colour, because the band
        # under it is the accent.
        self.background_rgb = colours.get("background")
        # The band under the selected row. Herdr's own navigation surface
        # draws it in the accent, and the theme's `selection_bg` is the
        # user's own choice when they have made one.
        self.selection_rgb = colours.get("selection") or self.accent_rgb
        self.accent = foreground(self.accent_rgb)
        self.chord = foreground(self.chord_rgb)
        self.hint = foreground(self.hint_rgb)
        self.ok = foreground(colours.get("ok") or OK_COLOUR)
        self.err = foreground(colours.get("err") or ERR_COLOUR)
        self.warn = foreground(colours.get("warn") or WARN_COLOUR)
        self.reset = RESET
        self.bold = BOLD
        self.dim = DIM

    def background(self):
        """The escape sequence that paints the panel's own background."""
        if not self.background_rgb:
            return ""
        return "\x1b[48;2;%d;%d;%dm" % tuple(self.background_rgb)

    def on_selection(self):
        """The escape sequence for text that sits on the selected row.

        The band is the accent, so the text on it is the panel's background
        colour: light text on a light band is unreadable, and Herdr's own
        navigation surface does exactly this.
        """
        if self.background_rgb:
            return "\x1b[38;2;%d;%d;%dm" % tuple(self.background_rgb)
        # The theme's background is not known, so the darkest version of the
        # accent is used: it is close enough to a panel, and it is always
        # darker than the band it sits on.
        return foreground(derive(self.accent_rgb, (0, 0, 0), 0.85))

    def selection(self):
        """The escape sequence that paints the selected row's band."""
        return "\x1b[48;2;%d;%d;%dm" % tuple(self.selection_rgb)


# -- the files ---------------------------------------------------------------


def config_dir():
    """The plugin's config dir, or "" when there is none."""
    return os.environ.get("HERDR_PLUGIN_CONFIG_DIR") or ""


def bundled_path(name):
    """The copy of `name` that ships with the plugin."""
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", name)


def user_path(name):
    """The user's copy of `name`, or None when there is none."""
    directory = config_dir()
    if not directory:
        return None
    return os.path.join(directory, name)


def _read_toml(path):
    """A TOML file as a dict, or {} when it cannot be read."""
    if not path or not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as handle:
            text = handle.read()
        data = tomlite.loads(text)
    except Exception:  # noqa: BLE001 - a user's file must not crash the overlay
        return {}
    return data if isinstance(data, dict) else {}


def bundled_defaults():
    """The bundled file's values for the settings the plugin knows.

    The bundled `visuals.toml` is the single source of truth for the
    defaults: they are read from it, not duplicated in the code, so a
    setting added there is a setting the plugin has.
    """
    data = _read_toml(bundled_path(SETTINGS_FILE))
    defaults = {}
    for key in KNOWN_SETTINGS:
        value = data.get(key)
        if isinstance(value, (str, bool, int, float)):
            defaults[key] = value
    for key in data:
        if key not in KNOWN_SETTINGS:
            # Our own file, so this is our mistake: say so, because a
            # setting that is read from nowhere looks like a setting that
            # does nothing.
            _warn_once(
                "unknown-bundled:%s" % key,
                "the bundled %s has a setting %r that this plugin does not "
                "know; ignoring it" % (SETTINGS_FILE, key),
            )
    return defaults


# The defaults, loaded from the bundled file. It is read here rather than
# written out again, so the two cannot drift apart.
DEFAULT_SETTINGS = bundled_defaults()


def load_settings():
    """The visual settings: the bundled defaults with the user's file over them.

    Only the keys the user mentions change, so one setting can be overridden
    without copying the whole file. The merge is the one rule every
    user-editable file uses: a setting the plugin does not know is dropped
    with a word on stderr that names it and the file it came from.
    """
    merged = bundled_defaults()
    user = user_path(SETTINGS_FILE)
    if user:
        merged = merge_mod.merge_settings(
            merged,
            _read_toml(user),
            KNOWN_SETTINGS,
            PLUGIN,
            user,
            expected={key: type(value) for key, value in merged.items()},
            seen=_WARNED,
        )
    return merged


def load_icons():
    """The glyphs: the user's file merged over the bundled one.

    The merge is per section, so a user can change one glyph without copying
    the whole file. The glyph names inside a section are free: a user may
    add one for a layer of their own.
    """
    user = user_path(ICONS_FILE)
    return merge_mod.merge_records(
        _read_toml(bundled_path(ICONS_FILE)),
        _read_toml(user) if user else {},
        None,
        PLUGIN,
        user or bundled_path(ICONS_FILE),
        known_names=KNOWN_ICON_SECTIONS,
        what="section",
        seen=_WARNED,
        values=lambda value: isinstance(value, str),
    )


# -- the accent --------------------------------------------------------------


def herdr_config_path():
    """The Herdr config to read the accent from.

    Derived from the config dir, so it is the same file the rest of the
    plugin reads and the same one an XDG user keeps in
    `$XDG_CONFIG_HOME/herdr/`. A hardcoded `~/.config/herdr/config.toml`
    was a second answer to "where is Herdr's config", and for an XDG user
    it was the wrong one.
    """
    return keymap_mod.user_config_path()


def read_herdr_config():
    """Herdr's own config, as a dict. Empty when it cannot be read."""
    return _read_toml(herdr_config_path())


def _dig(data, *path):
    """A value in a nested dict, or None."""
    node = data
    for key in path:
        if not isinstance(node, dict):
            return None
        node = node.get(key)
    return node


def resolve_palette(settings=None):
    """The colours to draw with, and where the accent came from.

    The accent's order is: the plugin's own `accent` setting, then Herdr's
    `theme.custom.accent`, then Herdr's `ui.accent`, then the name of a theme
    -- the plugin's `theme` setting first, then Herdr's own `theme.name`.

    The rest of the palette comes from the theme the user is running, and
    from Herdr's own `[theme.custom]` colours where the theme has them:
    `green`, `red` and `yellow` for the statuses, `panel_bg` for the
    background and `selection_bg` for the selected row's band. A theme that
    says nothing about one of them leaves it to be derived.

    The accent is checked against a dark and a light background, and one
    that cannot be read on either is refused -- the candidates are a chain,
    and a later one may be perfectly readable.
    """
    if settings is None:
        settings = load_settings()
    herdr = read_herdr_config()

    theme_name = ""
    for name in (settings.get("theme"), _dig(herdr, "theme", "name")):
        if isinstance(name, str) and name.strip():
            theme_name = name.strip().lower()
            break
    theme = THEME_PALETTES.get(theme_name, {})

    candidates = []

    def offer(label, text):
        if isinstance(text, str) and text.strip():
            candidates.append((label, text.strip()))

    offer("the plugin's accent setting", settings.get("accent"))
    offer("Herdr's theme.custom.accent", _dig(herdr, "theme", "custom", "accent"))
    offer("Herdr's ui.accent", _dig(herdr, "ui", "accent"))
    if theme.get("accent"):
        offer(
            "the %s theme" % theme_name,
            theme["accent"],
        )

    accent = None
    source = "the built-in accent"
    for label, text in candidates:
        rgb = parse_colour(text)
        if rgb is None:
            # A colour we cannot read is not a colour: try the next one.
            continue
        if contrast(rgb, DARK_BACKGROUND) >= MINIMUM_CONTRAST:
            accent, source = rgb, label
            break
        if contrast(rgb, LIGHT_BACKGROUND) >= MINIMUM_CONTRAST:
            accent, source = rgb, label
            break
        _warn_once(
            "accent:%s" % text,
            "the accent %s (%s) cannot be read on a dark or a light background; "
            "refusing it" % (text, label),
        )
        # Keep looking. The candidates are a chain, and one that cannot be
        # read is refused -- but a later one may be perfectly readable, and
        # the user should get the best accent available rather than the
        # built-in one.
        continue

    if accent is None:
        accent = parse_colour(FALLBACK_ACCENT)

    def role(name, *paths):
        """The first of `paths` that names a colour we can read.

        The paths are tried in order: the user's own `[theme.custom]`
        colour first, then the theme's own.
        """
        for path in paths:
            text = None
            if len(path) == 3:
                text = _dig(herdr, path[0], path[1], path[2])
            elif len(path) == 2 and path[0] == "theme":
                text = theme.get(path[1])
            if text is None:
                continue
            rgb = parse_colour(text)
            if rgb is not None:
                return rgb
        return None

    return {
        "accent": accent,
        "muted": role("muted", ("theme", "custom", "text"), ("theme", "muted")),
        "ok": role("ok", ("theme", "custom", "green"), ("theme", "ok")),
        "err": role("err", ("theme", "custom", "red"), ("theme", "err")),
        "warn": role("warn", ("theme", "custom", "yellow"), ("theme", "warn")),
        "background": role(
            "background", ("theme", "custom", "panel_bg"), ("theme", "background")
        ),
        "selection": role(
            "selection", ("theme", "custom", "selection_bg"), ("theme", "selection")
        ),
        "source": source,
        "theme": theme_name,
    }


class Visuals:
    """Everything the overlay needs to draw itself."""

    def __init__(self, settings, icons, palette):
        self.settings = settings
        self.icons = icons
        self.palette = palette

    @property
    def frame(self):
        """Whether the overlay is drawn inside a box."""
        return bool(self.settings.get("frame", True))

    @property
    def use_icons(self):
        """Whether the Nerd Font glyphs are drawn."""
        return bool(self.settings.get("icons", True))

    def layer_icon(self, layer_id):
        """The glyph for a layer, or "" when there is none."""
        if not self.use_icons:
            return ""
        return self.icons.get("layer", {}).get(layer_id, "")

    def group_icon(self, group):
        """The glyph for a group in the reference view, or ""."""
        if not self.use_icons:
            return ""
        return self.icons.get("group", {}).get(group, "")

    def kind_label(self, kind):
        """The word that says what kind of thing a row is.

        It is the fallback for the glyph: `icons = false` turns the font's
        glyphs off, and the kind of a row -- a space, a tab, a pane, an
        agent -- has to stay readable, because the tree's own shape does
        not say it. A tab and a pane are drawn with the same markers.

        Unlike the glyphs, this is plain text, so it is not turned off by
        `icons = false`: it is what the glyph is replaced *by*.
        """
        return self.icons.get("kind", {}).get(kind, "")

    def status_colour(self, category):
        """The colour for an agent status, from the category's name.

        The status is the thing the eye should go to: an agent that is
        blocked wants the user now, one that is done is finished, and one
        that is idle is waiting for something. So the status is the only
        place in the overlay with a colour that is not a shade of the
        accent -- red for blocked, green for done -- and the quiet ones
        are muted so they do not shout.

        The category is the name, not the raw status text: "blocked" is
        the category of "needs_attention" as well.
        """
        return {
            "blocked": self.palette.err,
            # Working is yellow, the way Herdr's own sidebar draws an agent
            # that is busy: it is the state you notice without it being an
            # alarm.
            "working": self.palette.warn,
            "done": self.palette.ok,
            "idle": self.palette.hint,
            "unknown": self.palette.hint,
        }.get(category, self.palette.hint)

    def kind_colour(self, kind):
        """The colour for a row's kind marker, by how deep the row is.

        The tree's shape already says how deep a row is, so the marker
        does not have to: it says what KIND of thing the row is, and the
        colour says how far down it is. The deeper the row, the brighter
        the marker -- so the leaves, which are the things you can jump
        to, stand out and the structure recedes.
        """
        return {
            "workspace": self.palette.hint,
            "tab": self.palette.chord,
            "pane": self.palette.accent,
        }.get(kind, self.palette.hint)

    def status_icon(self, status):
        """The glyph for an agent status, or "" when there is none.

        The vocabulary is the one in `icons.toml`: blocked, idle, done and
        unknown. It is the herdr-navigator's, so the two read as one family.

        Unlike the Nerd Font icons, these are plain characters that any
        font can draw, so `icons = false` does not turn them off: the
        setting is about the font's private-use-area glyphs.
        """
        return self.icons.get("status", {}).get(status, "")

    def marker(self, name):
        """One of the tree markers, or ""."""
        if not self.use_icons:
            return ""
        return self.icons.get("marker", {}).get(name, "")


def load(settings=None, icons=None):
    """The visuals for this run: the settings, the glyphs and the palette."""
    if settings is None:
        settings = load_settings()
    if icons is None:
        icons = load_icons()
    colours = resolve_palette(settings)
    source = colours.pop("source", "")
    colours.pop("theme", None)
    return Visuals(settings, icons, Palette(colours, source))
