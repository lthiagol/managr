#!/usr/bin/env python3
"""Add or remove the keybinding that opens the overlay.

The binding is a `type = "plugin_action"` entry, and it invokes the plugin's
own `open` action. Two reasons, and both of them came out of Herdr's own
rules:

* **A popup binding has nowhere to put a title.** `[[keys.command]]` has
  `key`, `type`, `command`, `description`, `width` and `height`, and no
  `title` -- so a popup opened by a config binding is called "popup". The
  plugin's own pane in the manifest has a title, and that is what Herdr
  puts on the frame. Opening the popup through the plugin is what makes it
  say "Managr".
* **A popup binding is launched by the server**, with no `HERDR_PLUGIN_*`
  environment at all, so it cannot name a path inside the plugin directory
  and needs a generated launcher to stand in for one. A plugin action is
  launched by the plugin runtime, with the plugin root as the working
  directory and the full environment, so the binding names an action id
  and there is no launcher to keep.

What this manages in `config.toml`:

    [[keys.command]]
    key = "prefix+space"
    type = "plugin_action"
    command = "managr.open"
    description = "layered keymap"

The size of the popup is not here: it is in the manifest, next to the pane
entry, where Herdr reads it from.

`[[keys.command]]` is an array of tables, so appending a second one is
correct TOML, and removing ours means removing one table, not one line. This
finds the table by its `command` value rather than by position, so it survives
the user adding and reordering their own custom commands.

    configure.py install
    configure.py uninstall
    configure.py status
    configure.py print
"""

import os
import re
import subprocess
import sys

BACKUP_SUFFIX = ".managr.bak"

# The trigger key.
#
# It used to be `prefix+?`, which is a trap for anyone whose layout puts "?"
# behind shift (a "/?" layout): Herdr matches the *character*, so they would
# have to press shift+slash, and on a kitty-protocol terminal that same
# keystroke also matches `prefix+shift+?`, making the two bindings ambiguous.
#
# `prefix+space` needs no shift, is unambiguous on every layout, and leaves
# Herdr's native help panel on `prefix+?` where it belongs -- so this plugin
# never has to touch the native keymap.
#
# The env var is named after the plugin. It used to be `HERDR_LAYERS_KEY`,
# left over from an even earlier name, and the old name still works so a
# script that set it keeps working.
TRIGGER_KEY = (
    os.environ.get("HERDR_MANAGR_KEY")
    or os.environ.get("HERDR_LAYERS_KEY")
    or "prefix+space"
)
DESCRIPTION = "layered keymap"
PLUGIN_ID = "managr"

# The action the binding invokes: the plugin's own `open`, qualified.
#
# The qualified id is `<plugin id>.<action id>`, and the server wants the
# whole thing. A plugin id may hold dots and an action id may not, so the
# id is not split here.
ACTION_ID = "%s.open" % PLUGIN_ID

# An earlier version of this plugin opened the popup with a `type = "popup"`
# binding that named a generated launcher. The binding is found by its
# `command` value, so the old one has to be recognised too -- otherwise
# uninstall would leave it behind, and the user would have two bindings
# for the same key.
LEGACY_COMMAND = "overlay.sh"

# The plugin has been renamed more than once: `herdr-nextkey`, then
# `herdr-layers`, then `herdr-keybar`, then `herdr-chord`, and now
# `managr`. A binding written by an earlier name names the action of a
# plugin that no longer exists, so the old qualified ids are recognised
# too -- otherwise an upgrade leaves the user with two bindings for the
# same key: a dead one and a live one.
LEGACY_ACTION_IDS = (
    "herdr-chord.open",
    "herdr-keybar.open",
    "herdr-layers.open",
)

# An earlier version moved Herdr's native help panel to `prefix+shift+?` to
# make room for a `prefix+?` trigger. That was a bad trade on a "/?" layout,
# so the trigger moved to `prefix+space` and the native help went back where
# it was. This marker lets an old install clean itself up.
LEGACY_MARKER = "# >>> managr >>>"

# The marker earlier names wrote. A renamed plugin leaves its old marker
# behind, so an install from an older version still cleans itself up.
LEGACY_MARKERS = (
    "# >>> herdr-chord >>>",
    "# >>> herdr-keybar >>>",
    "# >>> herdr-layers >>>",
    LEGACY_MARKER,
)


def _strip_legacy_help(blocks):
    """Remove a native-help line a previous version of this plugin moved.

    Only lines carrying one of our markers are touched, so a `keys.help` the
    user set themselves is never affected.
    """
    removed = False
    for block in blocks:
        if block["name"] != "keys":
            continue
        kept = []
        for line in block["body"]:
            match = KEY_RE.match(line)
            if (
                match
                and match.group(1) == "help"
                and any(marker in line for marker in LEGACY_MARKERS)
            ):
                removed = True
                continue
            kept.append(line)
        block["body"] = kept
    return removed

TABLE_RE = re.compile(r"^\s*\[+\[?([^\[\]]+)\]?\]*\s*$")
KEY_RE = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_.]*)\s*=\s*(.*?)\s*$")


def config_path():
    return os.environ.get("HERDR_CONFIG_PATH") or os.path.expanduser("~/.config/herdr/config.toml")


def legacy_launcher_path():
    """Where the startup hook used to write the launcher the binding named.

    Nothing writes it any more: the binding invokes the plugin's `open`
    action, and the plugin runtime runs the pane's command with the plugin
    root as the working directory. It is still computed, because the old
    binding has to be found so that an upgrade can replace it and an
    uninstall can remove it.
    """
    config_dir = os.environ.get("HERDR_PLUGIN_CONFIG_DIR")
    if not config_dir:
        try:
            out = subprocess.run(
                ["herdr", "plugin", "config-dir", PLUGIN_ID],
                capture_output=True,
                text=True,
                timeout=10,
            )
            if out.returncode == 0 and out.stdout.strip():
                config_dir = out.stdout.strip().splitlines()[-1].strip()
        except (OSError, subprocess.SubprocessError):
            config_dir = None
    if not config_dir:
        config_dir = os.path.join(
            os.path.expanduser("~/.config/herdr/plugins/config"), PLUGIN_ID
        )
    return os.path.join(config_dir, LEGACY_COMMAND)


# Kept under its old name too: the tests and the status output use it, and
# renaming it everywhere would be noise.
launcher_path = legacy_launcher_path


def read_config(path):
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as handle:
            return handle.read().splitlines()
    return ["onboarding = false"]


def write_config(lines, path):
    text = "\n".join(lines).rstrip("\n") + "\n"
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    sys.path.insert(0, os.path.join(here, "src"))
    try:
        from managr import tomlite
    except Exception:  # noqa: BLE001
        tomlite = None

    # Two independent checks, because they fail in different ways. The
    # structural one is not redundant: `tomlite` is deliberately lenient -- it
    # has to read a user's config that may hold anything -- but that leniency
    # makes it useless as a safety net. On Python 3.9 there is no `tomllib`, so
    # a malformed table header like "[ui" would parse fine and the file would
    # be rewritten anyway. The mutator runs on whatever `python3` is on PATH, and
    # on macOS that is the system 3.9.
    #
    # So the guarantee is not "the result is valid TOML" -- on 3.9 only the
    # structural check runs. It is "the result passes the checks this
    # interpreter can make", and a file that does not is left untouched.
    problems = tomlite.check_strict(text) if tomlite is not None else []
    parser = None
    try:
        import tomllib

        parser = tomllib.loads
    except ImportError:
        parser = tomlite.loads if tomlite is not None else None

    if parser is not None:
        try:
            parser(text)
        except Exception as exc:  # noqa: BLE001
            problems = list(problems) + [str(exc)]
    if problems:
        print(
            "managr: refusing to write, the result does not pass the TOML "
            "checks (%s)\n"
            "managr: %s is untouched." % ("; ".join(problems[:3]), path),
            file=sys.stderr,
        )
        return 1
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(text)
    return 0


def tables(lines):
    """Split the file into (header, body_lines) blocks.

    A block runs from a table header to the next header. A `[[keys.command]]`
    table is one such block, so it can be removed wholesale.
    """
    blocks = []
    current = None
    for line in lines:
        match = TABLE_RE.match(line)
        if match:
            if current is not None:
                blocks.append(current)
            current = {"header": line, "name": match.group(1).strip(), "body": []}
        elif current is not None:
            current["body"].append(line)
        else:
            blocks.append({"header": None, "name": None, "body": [line]})
    if current is not None:
        blocks.append(current)
    return blocks


def flatten(blocks, skip=None):
    """Turn blocks back into lines, optionally dropping some.

    `skip` may be one block or a list of them. Blocks are compared by
    identity, not by value: two identical tables are two different tables.
    """
    if skip is None:
        dropped = set()
    elif isinstance(skip, (list, tuple, set)):
        dropped = {id(block) for block in skip}
    else:
        dropped = {id(skip)}
    out = []
    for block in blocks:
        if id(block) in dropped:
            continue
        if block["header"] is not None:
            out.append(block["header"])
        out.extend(block["body"])
    return out


def _field(block, name):
    for line in block["body"]:
        match = KEY_RE.match(line)
        if match and match.group(1) == name:
            value = match.group(2).strip()
            if " #" in value:
                value = value.split(" #", 1)[0].strip()
            if value.startswith('"') and value.endswith('"'):
                return value[1:-1]
            return value
    return None


def _is_ours(block):
    """True when a block is a binding this plugin owns.

    Three shapes are ours: the current one, which invokes the plugin's `open`
    action; the one an earlier *name* of this plugin wrote, which invokes an
    action of a plugin that no longer exists; and the oldest one, which named
    a generated launcher. The old ones are found so that an upgrade replaces
    them instead of leaving the user with two bindings for the same key.
    """
    if block["name"] != "keys.command":
        return False
    command = _field(block, "command")
    return (
        command == ACTION_ID
        or command in LEGACY_ACTION_IDS
        or command == legacy_launcher_path()
    )


def find_ours(blocks):
    """Our binding, or None."""
    for block in blocks:
        if _is_ours(block):
            return block
    return None


def find_all_ours(blocks):
    """Every binding of ours.

    A rename can leave more than one behind: the binding the old id wrote
    and the binding the new id writes. Only one of them may stay, so the
    installer keeps one and drops the rest, and the uninstaller drops them
    all.
    """
    return [block for block in blocks if _is_ours(block)]


def _set_field(body, name, value):
    """Set `name` in a block's body, or append it."""
    for index, line in enumerate(body):
        match = KEY_RE.match(line)
        if match and match.group(1) == name:
            body[index] = '%s = "%s"' % (name, value)
            return
    body.append('%s = "%s"' % (name, value))


def _drop_field(body, name):
    """Remove `name` from a block's body. True when a line went."""
    for index, line in enumerate(body):
        match = KEY_RE.match(line)
        if match and match.group(1) == name:
            del body[index]
            return True
    return False


def do_install():
    path = config_path()
    original = read_config(path)
    backup = path + BACKUP_SUFFIX
    if not os.path.exists(backup):
        with open(backup, "w", encoding="utf-8") as handle:
            handle.write("\n".join(original).rstrip("\n") + "\n")
        print("managr: backed up %s -> %s" % (path, backup), file=sys.stderr)

    blocks = tables(original)
    ours = find_ours(blocks)

    legacy_removed = _strip_legacy_help(blocks)
    if legacy_removed:
        print(
            "managr: restored the native help panel to its default key",
            file=sys.stderr,
        )

    if ours is not None:
        # Already there. Make sure it is the binding we want.
        changed = False
        for field, wanted in (
            ("key", TRIGGER_KEY),
            ("type", "plugin_action"),
            ("command", ACTION_ID),
            ("description", DESCRIPTION),
        ):
            if _field(ours, field) != wanted:
                _set_field(ours["body"], field, wanted)
                changed = True
        # The old binding carried the popup's size. The size is in the
        # manifest now, next to the pane entry, where Herdr reads it from,
        # and a `plugin_action` binding has no size of its own.
        for field in ("width", "height"):
            if _drop_field(ours["body"], field):
                changed = True
        # A rename can leave two bindings for the same key: the one an
        # earlier id wrote and the one the current id writes. The one we
        # keep is rewritten above; the others go, or the user has a dead
        # binding and a live one on the same key.
        extras = [block for block in find_all_ours(blocks) if block is not ours]
        if extras:
            blocks = [
                block
                for block in blocks
                if not any(block is extra for extra in extras)
            ]
            changed = True
        if not changed and not legacy_removed:
            print("managr: binding already correct", file=sys.stderr)
            return 0
        rebuilt = flatten(blocks)
        code = write_config(rebuilt, path)
        if code == 0:
            print("managr: updated the binding in %s" % path, file=sys.stderr)
        return code

    # Not present: append a new table. TOML allows repeated array-of-table
    # headers, so appending is the correct way to add one.
    new_block = [
        "",
        "[[keys.command]]",
        'key = "%s"' % TRIGGER_KEY,
        'type = "plugin_action"',
        'command = "%s"' % ACTION_ID,
        'description = "%s"' % DESCRIPTION,
    ]
    rebuilt = flatten(blocks)
    while rebuilt and not rebuilt[-1].strip():
        rebuilt.pop()
    rebuilt.extend(new_block)

    code = write_config(rebuilt, path)
    if code == 0:
        print("managr: added the binding to %s" % path, file=sys.stderr)
    return code


def do_uninstall():
    path = config_path()
    if not os.path.exists(path):
        print("managr: no config at %s" % path, file=sys.stderr)
        return 0
    original = read_config(path)
    blocks = tables(original)
    ours = find_all_ours(blocks)

    if not ours:
        print("managr: no binding to remove", file=sys.stderr)
        return 0

    # Every binding of ours goes, including the ones an earlier id wrote.
    rebuilt = flatten(blocks, skip=ours)

    # Collapse the blank lines the removal left behind.
    cleaned = []
    for line in rebuilt:
        if not line.strip() and cleaned and not cleaned[-1].strip():
            continue
        cleaned.append(line)

    code = write_config(cleaned, path)
    if code == 0:
        print("managr: removed the binding from %s" % path, file=sys.stderr)
    return code


def do_status():
    path = config_path()
    print("config:   %s" % path)
    print("key:      %s" % TRIGGER_KEY)
    print("action:   %s" % ACTION_ID)
    if not os.path.exists(path):
        print("installed: no (no config)")
        return 0
    blocks = tables(read_config(path))
    ours = find_all_ours(blocks)
    print("installed: %s" % ("yes" if ours else "no"))
    for block in ours:
        # A binding of an earlier id is worth naming: it invokes a plugin
        # that does not exist any more.
        command = _field(block, "command")
        label = "binding" if command == ACTION_ID else "stale binding"
        print("\n%s:" % label)
        for line in [block["header"]] + block["body"]:
            if line.strip():
                print("  " + line)
    return 0


def do_print():
    print("[[keys.command]]")
    print('key = "%s"' % TRIGGER_KEY)
    print('type = "plugin_action"')
    print('command = "%s"' % ACTION_ID)
    print('description = "%s"' % DESCRIPTION)
    return 0


COMMANDS = {
    "install": do_install,
    "uninstall": do_uninstall,
    "status": do_status,
    "print": do_print,
}


def main(argv):
    if len(argv) != 2 or argv[1] not in COMMANDS:
        print("usage: configure.py {%s}" % "|".join(sorted(COMMANDS)), file=sys.stderr)
        return 2
    return COMMANDS[argv[1]]()


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
