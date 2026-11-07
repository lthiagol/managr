"""Reconstruct the effective Herdr keymap (v2 copy of the v1 engine).

Herdr exposes no API for the resolved keymap. There is no `keys.*` socket method
and no CLI subcommand that prints it, so the only way to know which bindings are
actually live is to rebuild them from the sources that do exist, in the order we
should trust them:

1. the user's own ``config.toml`` (real overrides) -- the one in the config
   dir, which is ``$XDG_CONFIG_HOME/herdr/`` when that is set and
   ``~/.config/herdr/`` otherwise; see ``config_dir()``,
2. ``herdr --default-config`` from the *installed* binary (so a version bump on
   this machine shows up here rather than in a stale bundled table),
3. a pinned snapshot shipped with the plugin, used only when 1 and 2 fail.

That third tier is what keeps a Herdr update from silently reducing the bar to a
handful of rows. ``sources_ok`` reports which tiers were usable so callers can
warn instead of pretending.
"""

import os
import re
import subprocess
from dataclasses import dataclass, field

from . import tomlite

# Guard for tier 2/3. The installed binary currently prints 44 bindable actions;
# a large drop means Herdr changed the comment layout and the parse is now
# reading noise rather than keybindings.
MIN_PLAUSIBLE_ACTIONS = 30

DEFAULT_BINDINGS = {
    "prefix": "ctrl+b",
    "help": "prefix+?",
    "settings": "prefix+s",
    "detach": "prefix+q",
    "reload_config": "prefix+shift+r",
    "open_notification_target": "prefix+o",
    "workspace_picker": "prefix+w",
    "goto": "prefix+g",
    "new_workspace": "prefix+shift+n",
    "new_worktree": "prefix+shift+g",
    "rename_workspace": "prefix+shift+w",
    "close_workspace": "prefix+shift+d",
    "new_tab": "prefix+c",
    "rename_tab": "prefix+shift+t",
    "previous_tab": "prefix+p",
    "next_tab": "prefix+n",
    "switch_tab": "prefix+1..9",
    "close_tab": "prefix+shift+x",
    "rename_pane": "prefix+shift+p",
    "edit_scrollback": "prefix+e",
    "copy_mode": "prefix+[",
    "focus_pane_left": "prefix+h",
    "focus_pane_down": "prefix+j",
    "focus_pane_up": "prefix+k",
    "focus_pane_right": "prefix+l",
    "swap_pane_left": "prefix+shift+h",
    "swap_pane_down": "prefix+shift+j",
    "swap_pane_up": "prefix+shift+k",
    "swap_pane_right": "prefix+shift+l",
    "cycle_pane_next": "prefix+tab",
    "cycle_pane_previous": "prefix+shift+tab",
    "split_vertical": "prefix+v",
    "split_horizontal": "prefix+minus",
    "close_pane": "prefix+x",
    "zoom": "prefix+z",
    "resize_mode": "prefix+r",
    "toggle_sidebar": "prefix+b",
    "navigate_workspace_up": "up",
    "navigate_workspace_down": "down",
    "navigate_pane_left": "h",
    "navigate_pane_down": "j",
    "navigate_pane_up": "k",
    "navigate_pane_right": "l",
}

# `copy_mode` and the four `swap_pane_*` actions are real and bindable, but Herdr
# omits them from `--default-config` *and* from its own `prefix+?` panel. Nothing
# else documents them, so they are pinned here and shown only if the user binds
# them.
KNOWN_UNLISTED_ACTIONS = {
    "copy_mode",
    "swap_pane_left",
    "swap_pane_down",
    "swap_pane_up",
    "swap_pane_right",
}

NAMED_KEYS = {
    "minus": "-",
    "plus": "+",
    "comma": ",",
    "period": ".",
    "question": "?",
    "slash": "/",
    "backslash": "\\",
    "semicolon": ";",
    "apostrophe": "'",
    "backtick": "`",
    "bracketleft": "[",
    "bracketright": "]",
    "equal": "=",
    "left": "<",
    "right": ">",
    "bar": "|",
    "less": "<",
    "greater": ">",
    "space": "space",
    "enter": "enter",
    "tab": "tab",
    "esc": "esc",
    "escape": "esc",
    "backspace": "bksp",
    "up": "up",
    "down": "down",
}

MODIFIER_WORDS = ("ctrl", "control", "alt", "option", "shift", "cmd", "command", "super", "hyper", "meta")
SPECIAL_WORDS = set(NAMED_KEYS) | {
    "up", "down", "left", "right", "home", "end", "pageup", "pagedown",
    "delete", "insert", "f1", "tab", "esc",
}

_BINDING_LINE_RE = re.compile(r'^#\s*([a-z_][a-z0-9_]*)\s*=\s*"([^"]*)"')


@dataclass(frozen=True)
class Binding:
    """One live keybinding, already reduced to something displayable."""

    action: str
    label: str
    chord: str          # prefix-relative when `direct` is False, else the full chord
    direct: bool = False
    source: str = "default"


@dataclass(frozen=True)
class CustomCommand:
    """A `[[keys.command]]` entry. These carry their own `description`."""

    key: str
    type: str
    command: str
    description: str
    chord: str
    direct: bool


@dataclass
class Keymap:
    prefix: str = "ctrl+b"
    bindings: dict = field(default_factory=dict)
    custom: list = field(default_factory=list)
    sources_ok: bool = True
    warnings: list = field(default_factory=list)


def herdr_bin() -> str:
    return os.environ.get("HERDR_BIN_PATH") or "herdr"


def user_config_path() -> str:
    """The Herdr config file.

    `HERDR_CONFIG_PATH` is the documented override and names the file
    itself. Otherwise the file is `config.toml` in the config dir, so the
    two are one answer and not two: an XDG user keeps their config in
    `$XDG_CONFIG_HOME/herdr/`, and the plugin has to read it from there
    rather than from a hardcoded `~/.config/herdr/config.toml`.
    """
    override = os.environ.get("HERDR_CONFIG_PATH")
    if override:
        return override
    return os.path.join(config_dir(), "config.toml")


def config_dir() -> str:
    """The directory Herdr's config lives in.

    Herdr's own rule: `$XDG_CONFIG_HOME/herdr` when that is set, and the
    platform config directory otherwise -- which for Herdr is
    `~/.config/herdr` on macOS and Linux. `HERDR_CONFIG_PATH` points at
    the config *file*, so its directory is the config dir when it is set.

    One answer, in both plugins: the shared layer-state file is computed
    from it, and Herdr's own config is read from it. A module that
    hardcoded `~/.config/herdr/config.toml` while this said
    `$XDG_CONFIG_HOME/herdr` gave an XDG user two answers, and put the
    state file somewhere the config was not.

    The path cannot be injected: the status command and the popup binding
    are both launched by the Herdr server straight from `config.toml`, so
    neither gets a `HERDR_PLUGIN_*` environment and both have to work the
    path out for themselves.
    """
    override = os.environ.get("HERDR_CONFIG_PATH")
    if override:
        return os.path.dirname(os.path.abspath(override))
    xdg = os.environ.get("XDG_CONFIG_HOME")
    if xdg:
        return os.path.join(os.path.abspath(xdg), "herdr")
    return os.path.expanduser("~/.config/herdr")


def pretty_chord(spec: str) -> str:
    """Turn a Herdr binding string into something short enough to print.

    `prefix+shift+n` -> `S-n`, `prefix+minus` -> `-`, `prefix+1..9` -> `1-9`.
    """
    if not spec:
        return ""
    parts = [p.strip().lower() for p in spec.split("+") if p.strip()]
    if not parts:
        return ""

    out, mods = [], []
    for part in parts:
        if part in MODIFIER_WORDS:
            mods.append("C" if part in ("ctrl", "control") else part[0].upper())
        else:
            out.append(NAMED_KEYS.get(part, part))

    key = out[-1] if out else ""
    if key.endswith("..9") and len(key) > 3:
        key = key[:-3] + "-9"
    elif ".." in key:
        key = key.replace("..", "-")

    if mods:
        return "-".join(mods) + "-" + key
    return key


def split_binding(spec: str, prefix: str):
    """Return (chord, is_direct) for one binding string or list of them."""
    values = spec if isinstance(spec, list) else [spec]
    values = [v for v in values if isinstance(v, str) and v.strip()]
    if not values:
        return "", False

    prefix_chords = {pretty_chord(p) for p in (prefix if isinstance(prefix, list) else [prefix])}

    for value in values:
        low = value.strip().lower()
        if low.startswith("prefix+"):
            return pretty_chord(low[len("prefix+") :]), False
        if pretty_chord(low) in prefix_chords:
            return "", False
    return pretty_chord(values[0]), True


def _looks_like_binding(name: str, value: str) -> bool:
    """Reject the commented-out examples Herdr ships inside its `[keys]` block.

    Those examples (`type = "popup"`, `command = "lazygit"`, `width = "80%"`)
    look exactly like key lines, so we also require the value to read like a key.
    A single printable character is the navigate-mode form (`navigate_pane_up =
    "k"`); Herdr bans bare digits 1-9 there, but it still parses them, so we
    accept them and let `herdr config check` be the authority on validity.
    """
    low = value.strip().lower()
    if not low:
        return False
    if low.startswith("prefix+") or low == "prefix":
        return True
    head = low.split("+")[0]
    if head in MODIFIER_WORDS:
        return True
    if len(low) == 1 and low.isprintable():
        return True
    if re.fullmatch(r"f\d{1,2}", low):
        return True
    return len(low) <= 24 and low in SPECIAL_WORDS


def parse_default_keys(text: str) -> dict:
    """Pull `action -> binding` out of the commented `[keys]` block.

    Stops at the first table header, commented or not: past that point lives
    Herdr's commented-out `[[keys.command]]` and `[keys.indexed]` examples.
    """
    found = {}
    inside = False
    for raw in text.splitlines():
        line = raw.strip()
        if line == "[keys]":
            inside = True
            continue
        if not inside:
            continue
        if line.startswith("[") or line.startswith("# ["):
            break

        match = _BINDING_LINE_RE.match(line)
        if not match:
            continue
        name, value = match.group(1), match.group(2)
        if _looks_like_binding(name, value):
            found[name] = value
    return found


def load_installed_defaults(timeout: float = 4.0) -> tuple:
    """Read defaults from the `herdr` binary actually on PATH."""
    try:
        proc = subprocess.run(
            [herdr_bin(), "--default-config"],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return {}, "could not run %s --default-config (%s)" % (herdr_bin(), exc.__class__.__name__)

    if proc.returncode != 0:
        return {}, "%s --default-config exited %s" % (herdr_bin(), proc.returncode)

    parsed = parse_default_keys(proc.stdout)
    if len(parsed) < MIN_PLAUSIBLE_ACTIONS:
        return {}, "only %d actions parsed from --default-config (expected >= %d)" % (
            len(parsed),
            MIN_PLAUSIBLE_ACTIONS,
        )
    return parsed, None


def load_user_config() -> tuple:
    """Read the user's overrides and custom commands."""
    path = user_config_path()
    try:
        with open(path, "r", encoding="utf-8") as handle:
            text = handle.read()
    except FileNotFoundError:
        return {}, {}, None
    except OSError as exc:
        return {}, {}, "could not read %s (%s)" % (path, exc.__class__.__name__)

    try:
        data = tomlite.loads(text)
    except Exception as exc:  # a bad config must not take the bar down
        return {}, {}, "could not parse %s (%s)" % (path, exc)

    keys = data.get("keys")
    overrides = {}
    if isinstance(keys, dict):
        for name, value in keys.items():
            if name == "command" or isinstance(value, dict):
                continue
            if isinstance(value, (str, list)):
                overrides[name] = value

    commands = []
    raw_commands = keys.get("command") if isinstance(keys, dict) else None
    if isinstance(raw_commands, dict):
        raw_commands = [raw_commands]
    for entry in raw_commands or []:
        if not isinstance(entry, dict):
            continue
        key = entry.get("key")
        if not isinstance(key, str) or not key.strip():
            continue
        commands.append(
            {
                "key": key,
                "type": str(entry.get("type", "shell")),
                "command": str(entry.get("command", "")),
                "description": str(entry.get("description", "")),
            }
        )

    return overrides, commands, None


def build(labels: dict = None) -> Keymap:
    """Merge every source into one displayable keymap."""
    labels = labels or {}
    warnings = []
    sources_ok = True

    installed, warn = load_installed_defaults()
    if warn:
        warnings.append(warn)
        sources_ok = False
        merged = dict(DEFAULT_BINDINGS)
    else:
        merged = dict(installed)
        for name in KNOWN_UNLISTED_ACTIONS:
            merged.setdefault(name, DEFAULT_BINDINGS.get(name, ""))

    overrides, commands, warn = load_user_config()
    if warn:
        warnings.append(warn)
    merged.update(overrides)

    prefix_spec = merged.get("prefix", "ctrl+b")
    prefix_list = prefix_spec if isinstance(prefix_spec, list) else [prefix_spec]
    prefix = pretty_chord(prefix_list[0]) if prefix_list else "ctrl+b"

    bindings = {}
    for action, spec in merged.items():
        if not spec:
            continue
        chord, direct = split_binding(spec, prefix_list)
        if not chord:
            continue
        label = labels.get(action)
        if not label:
            label = action.replace("_", " ")
            # The missing label is its own signal: it says this plugin does
            # not know the action's name. It must not be silenced by an
            # unrelated problem -- a user config that cannot be read is a
            # different signal, and it is reported on its own.
            warnings.append("no label for %r" % action)
        bindings[action] = Binding(
            action=action,
            label=label,
            chord=chord,
            direct=direct,
            source="user" if action in overrides else "default",
        )

    custom = []
    for entry in commands:
        chord, direct = split_binding(entry["key"], prefix_list)
        if not chord:
            continue
        description = entry["description"] or entry["command"] or entry["type"]
        custom.append(
            CustomCommand(
                key=entry["key"],
                type=entry["type"],
                command=entry["command"],
                description=description,
                chord=chord,
                direct=direct,
            )
        )

    return Keymap(
        prefix=prefix,
        bindings=bindings,
        custom=custom,
        sources_ok=sources_ok,
        warnings=warnings,
    )
