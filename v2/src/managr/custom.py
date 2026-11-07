"""The custom layer: the user's own commands, and the plugins' actions.

Every other layer's rows are in `data/layers.toml`: they are the same for
everybody, and they are actions the overlay knows how to run. This layer is
different. Its rows are not in the tree at all:

* the user's own `[[keys.command]]` entries, which the keymap engine has
  already parsed out of their `config.toml`;
* the actions of every installed plugin, which `plugin.action.list` reports.

**The plugin actions are fetched when the layer is opened, not on every
render.** A render happens on every keystroke, and a socket round trip per
keystroke would make the overlay feel like it is lagging. The list is
cached until the layer is entered again, so it is never staler than the last
visit -- the same rule the navigatr follows.

**The entries have a key space of their own.** They are not keymap actions,
so they cannot have the tree's keys: each one gets a single character, and
never the ones the layer itself needs. "/" is the filter's, and the scroll
keys are named keys and control characters rather than characters.

**A qualified plugin action id is split at the right dot.** Herdr's
qualified id is "<plugin id>.<action id>", and the rules for the two halves
are not symmetric: a plugin id may hold dots and an action id may not. So
the separator is found from the installed plugins, not from the first dot.

**A popup is a singleton in Herdr.** The overlay *is* a popup, so a command
of type "popup" cannot be run from inside it: the second popup is refused
with `ui_busy`. A command of type "pane" cannot be run from here either --
there is no socket method that opens a pane and runs a command in it. Both
say so, and both name the key to press in Herdr instead. They are not
attempted, and they do not fail silently.
"""

import sys
from dataclasses import dataclass

from . import actions as actions_mod
from . import table
from .client import HerdrError, HerdrUnavailable

# The layer this module fills.
LAYER_ID = "custom"

# The keys the entries use.
#
# They are single characters, and they never take a key the layer itself
# needs: "/" is the filter's and "?" is the reference view's. The letters
# come first, because they are the easiest to read; the punctuation is
# there for the user who has more entries than letters.
#
# The pool is finite -- it is every printable character except the space,
# the apostrophe, "/" and "?" -- and an install with many plugins has more
# actions than that. The entries past the end are listed but cannot be
# run, so the layer says how many of them there are (`unkeyed`).
ENTRY_KEYS = (
    "abcdefghijklmnopqrstuvwxyz"
    "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    "0123456789"
    "!#$%&()*+,-.:;<=>@[\\]^_`{|}~"
)

# The kinds of command the user can have in their `config.toml`.
#
# "shell" runs detached in the background. "popup" and "pane" open a window
# of their own -- and neither of those can be opened from inside the
# overlay, because the overlay is a popup already, so they are the case
# that falls through to the note below.
SHELL = "shell"
PLUGIN_ACTION = "plugin_action"


@dataclass
class Entry:
    """One row of the custom layer.

    `key` is the overlay's own key for it: a single character, never the
    filter's or the reference view's. `action` is the name of the action in
    the declarative map, so the overlay runs an entry the same way it runs
    every other action -- there is no second mechanism.

    `chord` says where the entry comes from: the plugin that owns the
    action, or the Herdr key that runs the command. `note` is what the row
    has to say about itself, e.g. that a command has to be pressed in
    Herdr.
    """

    key: str
    desc: str
    action: str
    chord: str = ""
    note: str = ""
    command: str = ""
    command_type: str = ""
    plugin_id: str = ""
    action_id: str = ""


def platform():
    """The platform name Herdr uses: "macos", "linux" or "windows"."""
    if sys.platform.startswith("darwin"):
        return "macos"
    if sys.platform.startswith("win"):
        return "windows"
    return "linux"


def _listing(result, key):
    """The list in a socket result, or an empty one."""
    if not isinstance(result, dict):
        return []
    value = result.get(key)
    if not isinstance(value, list):
        return []
    return [entry for entry in value if isinstance(entry, dict)]


def fetch(client):
    """The plugins' actions, and the plugins themselves.

    Returns (actions, plugins, error). It never raises: the socket can be
    gone and the server busy, and the overlay has to keep working, because
    the user still has to be able to press Escape.

    The plugin list is a second call, and it is optional: it says which
    plugins are enabled, and without it the actions are still shown.
    """
    actions = []
    plugins = {}
    error = ""

    try:
        call = actions_mod.LIST_PLUGIN_ACTIONS
        result = client.call(call.method, actions_mod.resolve(call.params))
        actions = _listing(result, call.result_key)
    except HerdrError as exc:
        return [], {}, "cannot read the plugins' actions: %s" % (exc.message or exc.code)
    except HerdrUnavailable as exc:
        return [], {}, "herdr unreachable: %s" % exc
    except Exception as exc:  # noqa: BLE001 - an overlay must not die
        return [], {}, "cannot read the plugins' actions: %s" % exc.__class__.__name__

    try:
        call = actions_mod.LIST_PLUGINS
        result = client.call(call.method, actions_mod.resolve(call.params))
        for entry in _listing(result, call.result_key):
            plugin_id = str(entry.get("plugin_id") or "")
            if plugin_id:
                plugins[plugin_id] = entry
    except (HerdrError, HerdrUnavailable):
        # The actions are still worth showing without it: the list of the
        # plugins only says which of them are enabled.
        pass
    except Exception:  # noqa: BLE001 - an overlay must not die
        pass

    return actions, plugins, error


def _command_chord(command, keymap):
    """The Herdr key that runs the command, e.g. "C-a space"."""
    if command.direct:
        return command.chord
    return "%s %s" % (keymap.prefix, command.chord)


def _split_qualified(action_id, plugin_actions=(), plugin_ids=()):
    """A qualified plugin action id into (plugin id, action id).

    Herdr's qualified id is "<plugin id>.<action id>", and the rules for
    the two halves are **not symmetric**: a plugin id may hold dots
    (`normalize_identifier` allows them) and an action id may not
    (`normalize_local_identifier` does not -- the server answers
    `invalid_plugin_action_id` for "layout.apply"). So the qualified id of
    the plugin "example.worktree-bootstrap" and the action "bootstrap" is
    "example.worktree-bootstrap.bootstrap", and splitting that at the
    FIRST dot asks for the plugin "example" and the action
    "worktree-bootstrap.bootstrap" -- an action id that cannot exist.

    Three answers, in order of how sure they are:

    1. The whole id, matched against the qualified ids of the installed
       plugins' actions. That is what Herdr's own keybind path does, so it
       is the surest answer of the three.
    2. The longest *installed* plugin id the id starts with, when what is
       left of it can be an action id -- that is, when it holds no dot.
    3. The LAST dot, which is the only one that can be the separator when
       no installed plugin says where it is, because an action id cannot
       hold a dot.

    An id with no dot at all is a bare action id: the plugin that owns it
    is looked up in the list of the installed plugins.
    """
    text = str(action_id or "")
    if "." not in text:
        return "", text

    for action in plugin_actions or ():
        plugin_id = str(action.get("plugin_id") or "")
        name = str(action.get("action_id") or "")
        if plugin_id and name and text == plugin_id + "." + name:
            return plugin_id, name

    best = ""
    for plugin_id in plugin_ids or ():
        plugin_id = str(plugin_id or "")
        if not plugin_id or not text.startswith(plugin_id + "."):
            continue
        rest = text[len(plugin_id) + 1 :]
        if "." in rest:
            # What is left cannot be an action id, so this plugin is not
            # the one that owns the id.
            continue
        if len(plugin_id) > len(best):
            best = plugin_id
    if best:
        return best, text[len(best) + 1 :]

    plugin_id, _, action = text.rpartition(".")
    return plugin_id, action


def _installed_plugin_ids(plugin_actions, plugins):
    """The ids of the plugins that are installed, longest first.

    A plugin id may hold dots, so the longest one that prefixes a
    qualified id is the plugin that owns it. The list comes from
    `plugin.list` and from the `plugin.action.list` rows, because either
    one alone can be short of the other.
    """
    ids = set()
    for plugin_id in plugins or {}:
        if plugin_id:
            ids.add(str(plugin_id))
    for action in plugin_actions or []:
        plugin_id = str(action.get("plugin_id") or "")
        if plugin_id:
            ids.add(plugin_id)
    return sorted(ids, key=len, reverse=True)


def _command_entry(command, keymap, plugin_actions, plugin_ids=()):
    """One of the user's own `[[keys.command]]` entries.

    The kind decides what happens when the key is pressed: a shell command
    runs, a plugin action is invoked, and a popup or a pane command cannot
    be run from inside the overlay at all.
    """
    kind = str(command.type or SHELL)
    chord = _command_chord(command, keymap)
    entry = Entry(
        key="",
        desc=command.description or command.command or kind,
        action=actions_mod.WINDOW_COMMAND,
        chord=chord,
        command=command.command,
        command_type=kind,
    )

    if kind == SHELL:
        entry.action = actions_mod.SHELL_COMMAND
        return entry, ""

    if kind == PLUGIN_ACTION:
        plugin_id, action_id = _split_qualified(
            str(command.command or ""), plugin_actions, plugin_ids
        )
        if not plugin_id:
            # A bare action id: the plugin that owns it is looked up in the
            # list, because the socket wants the plugin and the action.
            for action in plugin_actions:
                if str(action.get("action_id") or "") == action_id:
                    plugin_id = str(action.get("plugin_id") or "")
                    break
        if not plugin_id or not action_id:
            return None, "no plugin has an action called %r" % action_id
        entry.action = actions_mod.PLUGIN_ACTION
        entry.plugin_id = plugin_id
        entry.action_id = action_id
        entry.chord = chord
        return entry, ""

    # A popup or a pane command. The overlay is a popup, and a popup is a
    # singleton in Herdr: opening another one while ours is up is refused
    # with `ui_busy`. There is no way to open a pane and run a command in
    # it either. So the row says where the key is, and the key is pressed
    # in Herdr.
    entry.note = "press this in herdr"
    return entry, ""


def _plugin_entry(action, plugins, current):
    """One of the installed plugins' actions.

    An action of a plugin that is disabled, or one that is not for this
    platform, is left out -- and the reason is reported, because a row
    that is not shown is a thing the user cannot find.
    """
    plugin_id = str(action.get("plugin_id") or "")
    action_id = str(action.get("action_id") or "")
    # The title is the plugin's own text, and a plugin can be anything:
    # the escape sequences are stripped out of it, so a plugin cannot
    # colour the overlay or move the cursor in the middle of a row.
    title = table.strip_ansi(str(action.get("title") or action_id or plugin_id))

    plugin = plugins.get(plugin_id)
    if plugin is not None and not plugin.get("enabled", True):
        return None, "%s: %s (the plugin is disabled)" % (plugin_id, title)

    platforms = action.get("platforms") or []
    if not platforms and plugin is not None:
        platforms = plugin.get("platforms") or []
    if platforms and current not in platforms:
        return None, "%s: %s (not for %s)" % (plugin_id, title, current)

    return (
        Entry(
            key="",
            desc=title,
            action=actions_mod.PLUGIN_ACTION,
            chord=plugin_id,
            plugin_id=plugin_id,
            action_id=action_id,
        ),
        "",
    )


def build(keymap, plugin_actions, plugins):
    """The custom layer's entries: the user's commands, then the plugins'.

    Returns (entries, skipped). `skipped` says what was left out and why,
    so nothing is dropped in silence.
    """
    entries = []
    skipped = []
    plugin_ids = _installed_plugin_ids(plugin_actions, plugins)

    for command in getattr(keymap, "custom", None) or []:
        entry, reason = _command_entry(command, keymap, plugin_actions, plugin_ids)
        if entry is None:
            skipped.append(reason)
            continue
        entries.append(entry)

    current = platform()
    for action in plugin_actions:
        entry, reason = _plugin_entry(action, plugins, current)
        if entry is None:
            skipped.append(reason)
            continue
        entries.append(entry)

    for index, entry in enumerate(entries):
        entry.key = key_for(index)

    return entries, skipped


def key_for(index):
    """The key for the entry at `index`, or "" when there are none left.

    An entry without a key is still shown -- it is a thing the user can
    find with the filter -- but it cannot be run from the layer, so the
    layer says how many of them there are.
    """
    if 0 <= index < len(ENTRY_KEYS):
        return ENTRY_KEYS[index]
    return ""


def unkeyed(entries):
    """The entries the layer could not give a key to.

    The pool of keys is finite, and a plugin-heavy install has more
    actions than it has characters. Those rows are listed -- the filter
    finds them -- but no key runs them, so the layer has to say so rather
    than leave rows that look runnable and are not.
    """
    return [entry for entry in entries or [] if not entry.key]

