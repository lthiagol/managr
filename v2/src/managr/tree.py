"""The layer tree: what the overlay shows and what each key does.

The tree is hand-authored in `data/layers.toml` (which actions live in which
layer, and under which private key) but the *chords* it displays come from the
live Herdr keymap, so the overlay never shows a binding you do not actually
have.

The format is deliberately flat. TOML's `[[layer.item]]` nested inside
`[[layer]]` is ambiguous once you have more than one layer, so items are a
sibling table that names its layer:

    [[layer]]
    id = "panes"
    key = "p"

    [[item]]
    layer = "panes"
    key = "v"
    action = "split_vertical"
"""

import os
import sys
from dataclasses import dataclass, field

from . import actions as actions_mod
from . import keymap as keymap_mod
from . import merge as merge_mod
from . import tomlite

# The name the diagnostics are prefixed with.
PLUGIN = "managr"

# The fields a layer may carry.
KNOWN_LAYER_FIELDS = ("id", "title", "hint", "key")

# The fields an item may carry.
KNOWN_ITEM_FIELDS = ("layer", "key", "action", "desc", "keep", "value")

# The settings of the file that are not layers or items.
KNOWN_SETTINGS = ("reference_key", "navigatr_key", "agents_key")

# The action that opens the navigatr.
#
# The navigatr is a mode, not an action: it is a list of destinations with
# a cursor on it, and the overlay opens it itself. But the root lists it,
# so the key that opens it is discoverable -- a key that does something and
# is not listed anywhere is a key the user cannot find.
NAVIGATR_ACTION = "mode:navigatr"

# What the root calls the navigatr.
NAVIGATR_TITLE = "navigatr"

# The action that opens the agents list, and what the root calls it.
#
# The agents list is the second kind of navigatr: a flat list of every
# agent in the session, the ones that want attention first. It is a mode
# too, and it is listed in the root for the same reason the navigatr is.
AGENTS_ACTION = "mode:agents"
AGENTS_TITLE = "agents"

# Which layer an action belongs to when generating the flat reference.
REFERENCE_GROUPS = [
    ("pane", ("pane", "split", "close_pane", "zoom", "resize", "copy", "clear", "swap", "last_pane", "cycle")),
    ("tab", ("tab",)),
    ("workspace", ("workspace", "worktree")),
    ("agent", ("agent",)),
    ("session", ("detach", "reload", "settings", "help", "notification", "goto", "toggle_sidebar", "open_notification")),
]

# The title of each group in the reference view. The groups are how the
# bindings are sorted; the titles are what the headings say.
GROUP_TITLES = {
    "pane": "panes",
    "tab": "tabs",
    "workspace": "workspaces",
    "agent": "agents",
    "session": "session",
    "other": "other",
    "custom": "custom commands",
}


def _warn_once(message):
    """A diagnostic on stderr, once per process.

    The tree is built once per popup, and the popup is opened over and
    over: a warning on every build is a warning the user sees every time
    they open the overlay. The merge module keeps the set of things that
    have been said, so the other one-shot diagnostics share it.
    """
    merge_mod.warn_once(PLUGIN, message)


def _unpressable(key):
    """True when a key cannot be pressed.

    The overlay's keyspace is single characters: `handle()` compares the
    character that was typed with the key of an entry. A key of two
    characters is therefore a promise the plugin cannot keep -- the entry
    would be listed and no keypress could ever reach it.

    A key that is not a string at all is not "unpressable": it is not a
    key, and the merge drops it before it gets here. The check is here
    anyway, because a value that is not a string must never reach the
    code that measures it.
    """
    if not isinstance(key, str):
        return False
    return len(key) > 1


@dataclass
class Item:
    """One row in a layer."""

    key: str
    desc: str
    action: str
    layer: str = ""
    chord: str = ""
    keep: bool = False
    value: str = ""
    # The group this row heads, in the reference view. A heading is a
    # marker, not an action: it has no key, so nothing can press it.
    heading: str = ""
    # What the row has to say about itself, beside the chord. The hint rows
    # say "press this in herdr" because they have no socket method; the
    # custom layer's rows say it when the command has to be pressed in
    # Herdr rather than run from the overlay.
    note: str = ""

    @property
    def is_layer(self) -> bool:
        """A layer entry descends into a layer rather than running an action."""
        return self.action.startswith("layer:")

    @property
    def is_mode(self) -> bool:
        """A mode entry: another way of using the overlay, not an action.

        The navigatr is one. It is listed in the root so that its key is
        discoverable, and the overlay opens it itself rather than running
        it from the action map.
        """
        return self.action.startswith("mode:")

    @property
    def executable(self) -> bool:
        if self.is_layer or self.is_mode:
            return True
        return self.action in actions_mod.EXECUTORS

    @property
    def prompt(self):
        """The prompt to show when this item needs a typed value, else None."""
        return actions_mod.NEEDS_INPUT.get(self.action)

    @property
    def is_hint(self) -> bool:
        return not self.executable


@dataclass
class Layer:
    id: str
    title: str
    hint: str = ""
    key: str = ""
    items: list = field(default_factory=list)
    # Whether `key` was given explicitly. A layer redefined without a key
    # keeps the built-in one: overwriting it with "" would drop the layer
    # out of the root, where only layers with a key are listed.
    has_key: bool = False


@dataclass
class Tree:
    layers: dict = field(default_factory=dict)
    root: Layer = None
    reference: Layer = None
    # The key that opens the reference view, from `reference_key`.
    reference_key: str = ""
    # The key that opens the navigatr, from `navigatr_key`.
    navigatr_key: str = ""
    # The key that opens the agents list, from `agents_key`.
    agents_key: str = ""
    # The keymap the tree was built from. The custom layer needs it: the
    # user's own `[[keys.command]]` entries are in it, and they are rows
    # of that layer.
    keymap: object = None

    def layer(self, layer_id):
        if layer_id == "root":
            return self.root
        if layer_id == "reference":
            return self.reference
        return self.layers.get(layer_id)


def _chord_for(keymap, action):
    """The Herdr chord for an action, e.g. `C-a v`, or '' when unbound."""
    binding = keymap.bindings.get(action)
    if not binding:
        return ""
    if binding.direct:
        return binding.chord
    return "%s %s" % (keymap.prefix, binding.chord)


def _group_for(action):
    for name, needles in REFERENCE_GROUPS:
        if any(needle in action for needle in needles):
            return name
    return "other"


def _load_definition(path):
    with open(path, "r", encoding="utf-8") as handle:
        return tomlite.loads(handle.read())


def load_labels():
    """Action -> human label, using Herdr's own wording where we have it.

    The labels live with the plugin and are read-only to the user: they are
    Herdr's own wording for its actions. An action the file does not name
    falls back to its name with the underscores turned into spaces.
    """
    path = os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "data", "labels.toml"
    )
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = tomlite.loads(handle.read())
    except Exception:  # noqa: BLE001 - a missing label file is not fatal
        return {}
    return {k: v for k, v in data.items() if isinstance(v, str)}


def default_definition_path():
    """The layer definition to load.

    A `layers.toml` in the plugin config dir wins over the bundled one, so a
    user can add their own layers without editing the plugin.
    """
    config_dir = os.environ.get("HERDR_PLUGIN_CONFIG_DIR")
    if config_dir:
        candidate = os.path.join(config_dir, "layers.toml")
        if os.path.exists(candidate):
            return candidate
    return os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "data", "layers.toml"
    )


def _check_fields(entries, known, what, path):
    """A field the plugin does not know is dropped with a word about it.

    A misspelled field that is ignored in silence looks exactly like a
    field that did not work: the user writes `descc` and the row comes
    out with the action's name instead of their text.
    """
    for entry in entries or []:
        if not isinstance(entry, dict):
            continue
        name = entry.get("id") or entry.get("layer") or ""
        for field in entry:
            if field not in known:
                merge_mod.unknown_field(
                    PLUGIN, path, field, what, name or None
                )


def _parse(data, keymap, labels):
    """Turn a definition document into (layers, order, items_by_layer).

    Layers keep their declaration order, which is the order the root lists
    them in.
    """
    layers = {}
    order = []
    for entry in data.get("layer", []) or []:
        if not isinstance(entry, dict):
            continue
        layer_id = entry.get("id")
        if not layer_id:
            continue
        layers[layer_id] = Layer(
            id=layer_id,
            title=entry.get("title", layer_id),
            hint=entry.get("hint", ""),
            key=entry.get("key", ""),
            has_key="key" in entry,
        )
        order.append(layer_id)

    items = {layer_id: [] for layer_id in order}
    for entry in data.get("item", []) or []:
        if not isinstance(entry, dict):
            continue
        layer_id = entry.get("layer")
        if layer_id not in items:
            items[layer_id] = []
        action = entry.get("action", "")
        items[layer_id].append(
            Item(
                key=entry.get("key", ""),
                desc=entry.get("desc") or labels.get(action, action.replace("_", " ")),
                action=action,
                layer=layer_id,
                chord=_chord_for(keymap, action),
                keep=bool(entry.get("keep", False)),
                value=str(entry.get("value", "")),
            )
        )
    return layers, order, items


def build(keymap=None, definition_path=None, labels=None):
    """Assemble the tree.

    `keymap` supplies the chords; when omitted it is rebuilt from the running
    Herdr install, which is what the overlay wants.

    A `layers.toml` in the plugin config dir is merged over the bundled one:
    a layer you define replaces the built-in layer of the same name, and a
    layer you do not define is inherited. Define a layer with items and those
    items replace the built-in ones; define a layer with no items and it keeps
    them.
    """
    if keymap is None:
        labels = labels or {}
        keymap = keymap_mod.build(labels=labels)
    labels = labels or {}

    bundled_path = os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "data", "layers.toml"
    )
    user_path = None
    if definition_path is not None:
        bundled_path = definition_path
    else:
        candidate = default_definition_path()
        if os.path.abspath(candidate) != os.path.abspath(bundled_path):
            user_path = candidate

    layers, order, items = _parse(_load_definition(bundled_path), keymap, labels)

    # Layers that exist only because an item named them. They have no key and
    # no title of their own, so nothing can reach them.
    synthetic = set()

    user_data = {}
    if user_path and os.path.exists(user_path):
        user_data = _load_definition(user_path)
        _check_fields(user_data.get("layer"), KNOWN_LAYER_FIELDS, "layer", user_path)
        _check_fields(user_data.get("item"), KNOWN_ITEM_FIELDS, "item", user_path)
        user_layers, user_order, user_items = _parse(user_data, keymap, labels)

        # The user's layers over the bundled ones, field by field. This is
        # the one merge rule every user-editable file uses: a field the
        # user does not name keeps the bundled value, a layer the user does
        # not mention is inherited untouched, and a field the plugin does
        # not know is dropped with a word.
        base_fields = {
            layer_id: {"title": layer.title, "hint": layer.hint, "key": layer.key}
            for layer_id, layer in layers.items()
        }
        user_fields = {}
        for layer_id in user_order:
            layer = user_layers[layer_id]
            fields = {"title": layer.title, "hint": layer.hint}
            # Only take the key if you gave one. Overwriting it with ""
            # would drop the layer out of the root, because the root only
            # lists layers that have a key.
            if layer.has_key:
                fields["key"] = layer.key
            user_fields[layer_id] = fields
        merged_fields = merge_mod.merge_records(
            base_fields, user_fields, KNOWN_LAYER_FIELDS, PLUGIN, user_path, what="layer"
        )

        for layer_id in user_order:
            fields = merged_fields.get(layer_id, {})
            if layer_id not in layers:
                # A layer of the user's own: it is added to the root.
                order.append(layer_id)
                layers[layer_id] = Layer(
                    id=layer_id,
                    title=fields.get("title", layer_id),
                    hint=fields.get("hint", ""),
                    key=fields.get("key", ""),
                    has_key="key" in fields,
                )
            else:
                # A layer you redefine keeps its place in the root but takes
                # your title, hint and key.
                layers[layer_id].title = fields.get("title", layer_id)
                layers[layer_id].hint = fields.get("hint", "")
                if "key" in fields:
                    layers[layer_id].key = fields["key"]
            # A list the user names replaces the bundled list: your items
            # for a layer replace the built-in ones, and a layer you gave
            # no items for keeps them.
            if user_items.get(layer_id):
                items[layer_id] = user_items[layer_id]
        for layer_id, entries in user_items.items():
            if not entries:
                continue
            if layer_id in layers:
                # Items for a layer you did not redefine. They are still the
                # user's items: dropping them would lose work with no word.
                if layer_id not in user_order:
                    items[layer_id] = entries
            else:
                order.append(layer_id)
                if layer_id in user_layers:
                    layers[layer_id] = user_layers[layer_id]
                else:
                    synthetic.add(layer_id)
                    layers[layer_id] = Layer(id=layer_id, title=layer_id)
                items[layer_id] = entries

    for layer_id in order:
        layers[layer_id].items = items.get(layer_id, [])

    # `reference_key`, `navigatr_key` and `agents_key` are settings: the
    # user's value wins. An empty one is not a setting -- it means "not
    # set" -- so the bundled value stands.
    #
    # The user's settings go through the merge as they are, so a setting
    # the plugin does not know is reported rather than ignored in silence:
    # a misspelled `navigatr_key` would leave the user with the bundled
    # one and no word about why.
    #
    # The three keys are strings, and a value of the wrong type is
    # ignored with a word rather than used. `agents_key = 5` used to kill
    # the popup with a traceback: the key was compared with `len()` and
    # with a set, and neither takes an integer. A setting the user
    # hand-wrote is the most likely setting in the file to be wrong.
    bundled_settings = _load_definition(bundled_path)
    settings = merge_mod.merge_settings(
        {
            "reference_key": bundled_settings.get("reference_key", ""),
            "navigatr_key": bundled_settings.get("navigatr_key", ""),
            "agents_key": bundled_settings.get("agents_key", ""),
        },
        {
            key: value
            for key, value in (user_data or {}).items()
            if key not in ("layer", "item")
        },
        KNOWN_SETTINGS,
        PLUGIN,
        user_path or bundled_path,
        truthy_only=("reference_key", "navigatr_key", "agents_key"),
        expected={
            "reference_key": str,
            "navigatr_key": str,
            "agents_key": str,
        },
    )
    reference_key = settings.get("reference_key") or ""
    navigatr_key = settings.get("navigatr_key") or ""
    agents_key = settings.get("agents_key") or ""

    root = Layer(id="root", title="layers", hint="pick a layer")
    for layer_id in order:
        layer = layers[layer_id]
        if layer.key:
            root.items.append(
                Item(key=layer.key, desc=layer.title, action="layer:" + layer_id, layer="root")
            )

    # The navigatr is listed in the root, so the key that opens it is
    # discoverable: a key that does something and is nowhere on the screen
    # is a key the user cannot find.
    #
    # The key is the one from the definition file, so a user who wants it
    # somewhere else moves it there. And a key that a layer already takes
    # is not listed: the layer's own binding wins, exactly like the
    # filter's key, and an entry that promised the key would be a promise
    # the plugin does not keep.
    #
    # A key that cannot be pressed is not listed either. The overlay's
    # keyspace is single characters, so a key of two characters is an
    # entry that promises a key no keypress could ever reach.
    taken = set(item.key for item in root.items)
    if navigatr_key:
        if _unpressable(navigatr_key):
            # The key cannot be pressed, so the root must not list it: an
            # entry that promises a key nobody can type is the same lie
            # as an entry that promises a key a layer already took.
            _warn_once(
                "the navigatr's key %r is more than one character, so it "
                "cannot be pressed, and the navigatr is not reachable by it"
                % navigatr_key
            )
        elif navigatr_key in taken or navigatr_key == reference_key:
            # The navigatr is not reachable by that key. Saying so beats
            # a root that quietly does not have it: the user set the key
            # and the plugin does not use it.
            _warn_once(
                "the navigatr's key %r is taken by another entry "
                "in the root, so the navigatr is not reachable by it"
                % navigatr_key
            )
        else:
            root.items.append(
                Item(
                    key=navigatr_key,
                    desc=NAVIGATR_TITLE,
                    action=NAVIGATR_ACTION,
                    layer="root",
                )
            )
            # The key is taken now, so the agents list cannot have it.
            taken.add(navigatr_key)

    # The agents list is the second kind of navigatr, and it follows the
    # same rules as the navigatr: a key that cannot be pressed is not
    # listed, and a key another entry in the root already has is not
    # listed either -- the warning says so, and the root does not promise
    # a key the plugin does not use.
    #
    # It is added after the navigatr's entry, so the navigatr's key is
    # already in the taken set when this key is checked.
    if agents_key:
        if _unpressable(agents_key):
            _warn_once(
                "the agents list's key %r is more than one character, so it "
                "cannot be pressed, and the agents list is not reachable by it"
                % agents_key
            )
        elif agents_key in taken or agents_key == reference_key:
            _warn_once(
                "the agents list's key %r is taken by another entry "
                "in the root, so the agents list is not reachable by it"
                % agents_key
            )
        else:
            root.items.append(
                Item(
                    key=agents_key,
                    desc=AGENTS_TITLE,
                    action=AGENTS_ACTION,
                    layer="root",
                )
            )

    if reference_key:
        if _unpressable(reference_key):
            # The same rule as the navigatr's key: a key that cannot be
            # pressed is not listed.
            _warn_once(
                "the reference's key %r is more than one character, so it "
                "cannot be pressed, and the reference view is not "
                "reachable by it" % reference_key
            )
        else:
            root.items.append(
                Item(
                    key=reference_key,
                    desc="all bindings",
                    action="layer:reference",
                    layer="root",
                )
            )

    reference = _build_reference(keymap, labels)

    _warn_unreachable(layers, order, synthetic)

    return Tree(
        layers=layers,
        root=root,
        reference=reference,
        reference_key=reference_key or "",
        navigatr_key=navigatr_key or "",
        agents_key=agents_key or "",
        keymap=keymap,
    )


def _warn_unreachable(layers, order, synthetic):
    """Say so when items are loaded that no key can reach.

    The root lists a layer by its key, and an item reaches another layer by
    naming it. A layer with neither is invisible: its items load, and
    nothing ever shows them, which looks like the plugin lost them. A
    warning on stderr beats silence -- stdout is the tree, and a note in
    the middle of it would be read as part of the tree.
    """
    referenced = set()
    for layer in layers.values():
        for item in layer.items:
            if item.is_layer:
                referenced.add(item.action.split(":", 1)[1])
    for layer_id in order:
        layer = layers[layer_id]
        if layer.key or layer_id in referenced:
            continue
        if not layer.items:
            continue
        if layer_id in synthetic:
            reason = "no layer with that id exists"
        else:
            reason = "the layer has no key, so the root cannot reach it"
        sys.stderr.write(
            "managr: the items for layer %r are unreachable: %s\n"
            % (layer_id, reason)
        )


def _group_heading(group):
    """The row that heads a group in the reference view."""
    return Item(
        key="",
        desc=GROUP_TITLES.get(group, group),
        action="group:" + group,
        layer="reference",
        heading=group,
    )


def _build_reference(keymap, labels):
    """Every Herdr binding, grouped, for the `all bindings` view."""
    layer = Layer(id="reference", title="all bindings", hint="every live Herdr binding")
    groups = {}
    for action, binding in keymap.bindings.items():
        group = _group_for(action)
        groups.setdefault(group, []).append((action, binding))
    for group in [g for g, _ in REFERENCE_GROUPS] + ["other"]:
        entries = groups.get(group)
        if not entries:
            continue
        layer.items.append(_group_heading(group))
        for action, binding in sorted(entries, key=lambda pair: pair[1].chord):
            chord = binding.chord if binding.direct else "%s %s" % (keymap.prefix, binding.chord)
            layer.items.append(
                Item(
                    key="",
                    desc=labels.get(action, action.replace("_", " ")),
                    action=action,
                    chord=chord,
                    layer="reference",
                )
            )
    # The user's own `[[keys.command]]` entries are live bindings too, and
    # this view claims to list every one of them.
    if keymap.custom:
        layer.items.append(_group_heading("custom"))
    for entry in keymap.custom:
        chord = entry.chord if entry.direct else "%s %s" % (keymap.prefix, entry.chord)
        layer.items.append(
            Item(
                key="",
                desc=entry.description or entry.command or entry.type,
                action="custom:" + entry.key,
                chord=chord,
                layer="reference",
            )
        )
    return layer
