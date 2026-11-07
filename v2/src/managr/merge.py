"""The one merge rule for every user-editable file.

Four files in this repository can be edited by the user: `bar.toml` in v1,
and `layers.toml`, `visuals.toml` and `icons.toml` in v2. They used to be
merged four different ways -- one silently, one per key, one per section,
one replacing the items -- and a setting that was misspelled was ignored
in silence in three of them.

**The rule.**

* A **setting** -- a name the plugin reads a value by -- is known. The
  user's value wins; a setting the user does not name keeps the bundled
  value. A setting the plugin does not know is dropped, with a warning on
  stderr that names the key and the file it came from.
* A **named entry** -- a segment, a layer, an icon section -- the user
  names has its fields merged over the bundled entry's, field by field. An
  entry the user does not name is inherited untouched. A field the plugin
  does not know is dropped, with a warning.
* A **list** the user names replaces the bundled list. The fields of each
  entry are checked the same way.
* A **name** is free: it names something, and the plugin cannot know every
  name a user may want. A layer the user writes in `layers.toml` gets a
  glyph in `icons.toml`, so a name that is not in the bundled file is
  perfectly good.

A setting that is ignored in silence looks exactly like a setting that did
not work, so an unknown one is always reported.

This module is copied into both plugins, like `tomlite.py` and `keymap.py`:
a Herdr plugin is a self-contained directory, and the manifest cannot say
that one plugin depends on another. `make check-sync` keeps the copies in
step.
"""

import sys

# The warnings are printed once each: the overlay renders on every
# keystroke, and a message on every keystroke is noise. A caller that
# keeps its own set can pass it in; the default is this one.
_WARNED = set()


def warn_once(plugin, message, seen=None):
    """A diagnostic on stderr, once per process.

    `seen` is the set of things already said. It is a parameter so that a
    module with its own set -- the visuals, which warn about more than
    merges -- keeps using it.
    """
    if seen is None:
        seen = _WARNED
    key = (plugin, message)
    if key in seen:
        return
    seen.add(key)
    sys.stderr.write("%s: %s\n" % (plugin, message))


def _type_name(kind):
    """The name of a type, for a warning about a value."""
    if kind is bool:
        return "boolean"
    if kind is str:
        return "string"
    if kind is int:
        return "whole number"
    if kind is float:
        return "number"
    return kind.__name__


def unknown_setting(plugin, path, key, seen=None):
    """The warning for a setting the plugin does not know."""
    warn_once(
        plugin,
        "the setting %r in %s is not one this plugin knows; ignoring it"
        % (key, path),
        seen,
    )


def unknown_field(plugin, path, field, what, name=None, seen=None):
    """The warning for a field of an entry the plugin does not know."""
    if name:
        message = "the %s %r in %s has a field %r that this plugin does not " \
                  "know; ignoring it" % (what, name, path, field)
    else:
        message = "a %s in %s has a field %r that this plugin does not know; " \
                  "ignoring it" % (what, path, field)
    warn_once(plugin, message, seen)


def merge_settings(base, user, known, plugin, path, truthy_only=(), expected=None, seen=None):
    """The user's settings over the bundled ones, key by key.

    `base` is the bundled settings, `user` the user's file, and `known`
    the names the plugin reads. A setting the user names wins; a setting
    the user does not name keeps the bundled value. A setting the plugin
    does not know is dropped, with a word about it.

    `truthy_only` names the settings whose empty value means "not set":
    the bundled value stands. It is for the settings where an empty value
    is not a value -- not for the ones where it is, like `marker = ""`,
    which turns the glyph off.

    `expected` maps a setting to the type it has to be. A value of the
    right name and the wrong type is ignored with a word: `icons = "no"`
    is truthy, so it would turn the glyphs *on* when the user meant to
    turn them off.
    """
    merged = dict(base or {})
    for key, value in (user or {}).items():
        if key not in known:
            unknown_setting(plugin, path, key, seen)
            continue
        if key in truthy_only and not value:
            continue
        if expected and key in expected:
            wanted = expected[key]
            if wanted is not None and not isinstance(value, wanted):
                warn_once(
                    plugin,
                    "the setting %r in %s should be a %s; ignoring it"
                    % (key, path, _type_name(wanted)),
                    seen,
                )
                continue
        merged[key] = value
    return merged


def merge_records(base, user, known_fields, plugin, path, known_names=None, what="entry", seen=None, values=None):
    """The user's named entries over the bundled ones, field by field.

    `base` and `user` are dicts of name -> dict of fields. An entry the
    user names has its fields merged over the bundled entry's; an entry the
    user does not name is inherited untouched.

    `known_fields` is the set of fields the plugin reads, or None when
    the fields are names rather than settings -- the glyphs in
    `icons.toml`, where a user may add one for a layer of their own.
    `known_names` is the set of entry names the plugin knows, or None when
    the name is free.

    `values` says whether a value is one the plugin can use; a value it
    cannot is dropped without a word, because it is not a setting that
    was misspelled -- it is not a value at all.
    """
    merged = {}
    for name, fields in (base or {}).items():
        merged[name] = dict(fields) if isinstance(fields, dict) else {}
    for name, fields in (user or {}).items():
        if known_names is not None and name not in known_names:
            unknown_setting(plugin, path, name, seen)
            continue
        if not isinstance(fields, dict):
            continue
        target = merged.setdefault(name, {})
        for field, value in fields.items():
            if known_fields is not None and field not in known_fields:
                unknown_field(plugin, path, field, what, name, seen)
                continue
            if values is not None and not values(value):
                continue
            target[field] = value
    return merged


def merge_list(base, user, known_fields, plugin, path, what="entry", seen=None, values=None):
    """A list the user names replaces the bundled list.

    The fields of each entry are checked the same way: a field the plugin
    does not know is dropped, with a word about it.
    """
    entries = user if user else base
    merged = []
    for entry in entries or []:
        if not isinstance(entry, dict):
            continue
        clean = {}
        for field, value in entry.items():
            if known_fields is not None and field not in known_fields:
                unknown_field(plugin, path, field, what, None, seen)
                continue
            if values is not None and not values(value):
                continue
            clean[field] = value
        merged.append(clean)
    return merged
