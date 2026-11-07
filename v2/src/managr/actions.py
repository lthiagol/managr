"""Map Herdr action names onto socket calls.

The overlay shows Herdr's own actions, but it cannot press Herdr's keys:
`pane.send_keys` writes into a pane's PTY and Herdr's key handling sits
upstream of that. So instead, each action we want to run from the overlay gets
an executor here that performs the equivalent socket call.

Actions with no executor are *hint rows*: the overlay shows the Herdr chord and
says to press it yourself. That covers Herdr's own UI surfaces -- the settings
panel, the session navigator, the native help, the sidebar toggle, detach --
which have no socket method at all.

Each executor returns an `Outcome` telling the overlay what to show and
whether to stay open. Staying open is right for repeatable verbs (move focus
around) and wrong for one-shots (split, close), so the default is to close and
the repeatable ones opt back in.

**The map is data, so it can be checked.** Every action is described by
`Call`s -- a method and the parameters it would send -- and the executor is a
thin wrapper around that description. That is what lets `scripts/api_check.py`
ask a running server whether the call would be accepted, instead of the map
being a pile of hand-written socket calls that were only ever checked against
the documentation.

The reason to describe the parameters rather than pass them inline: Herdr's
parameter objects do not forbid extra fields, so a call with a wrong name is
*accepted and ignored*. `pane.close` with `pane` instead of `pane_id` would
not be refused -- it would close nothing, and the overlay would say "pane
closed" all the same.
"""

import os
import shutil
import subprocess

from .client import HerdrError, HerdrUnavailable

# The actions Herdr has but no socket method exposes, and therefore no
# executor. Listed here so the overlay can say so honestly instead of looking
# broken.


class Unreachable:
    """An action Herdr owns, with no socket method to run it.

    `search` is what to look for in the method names: the noun of the action
    ("copy" for `copy_mode`, "settings" for `settings`). `known` holds the
    methods that do match but do not reach the action -- the copy primitives
    are not a way to enter copy mode -- and why each one is not enough.

    The point of the search is that the claim is *measured*: when Herdr grows
    a method for one of these, the method shows up and the list is wrong.
    """

    def __init__(self, note, search=(), known=None):
        self.note = note
        self.search = tuple(search)
        self.known = dict(known or {})

    def __str__(self):
        return self.note


UNREACHABLE = {
    "help": Unreachable("Herdr's own keybind panel", ("help", "keybind")),
    "settings": Unreachable("Herdr's settings UI", ("settings",)),
    "goto": Unreachable("the session navigator", ("goto", "navigator")),
    "toggle_sidebar": Unreachable("the sidebar toggle", ("sidebar",)),
    "detach": Unreachable("detaching the client", ("detach",)),
    "copy_mode": Unreachable(
        # 0.9.5 has the copy *primitives*: pane.copy_motion, pane.copy_search
        # and pane.selection.read move a cursor and read a selection. None of
        # them enters the mode, so the mode is still Herdr's to press.
        "copy mode (0.9.5 has the copy primitives, not a way to enter the mode)",
        ("copy", "selection"),
        {
            "pane.copy_motion": "moves a cursor you give it, it does not enter the mode",
            "pane.copy_search": "searches a selection, it does not enter the mode",
            "pane.selection.read": "reads a selection, it does not enter the mode",
        },
    ),
    "resize_mode": Unreachable(
        "interactive resize mode",
        ("resize",),
        {
            "pane.resize": "a one-shot resize, not the interactive mode",
        },
    ),
    "open_notification_target": Unreachable(
        "the notification target",
        ("notification",),
        {
            "notification.show": "posts a notification, it does not open one",
        },
    ),
}


class Outcome:
    """What happened, and what the overlay should do next.

    `kind` is "ok" or "err": a refusal is not a success, and the overlay
    colours the message to say which one it was.
    """

    def __init__(self, message="", keep_open=False, close=False, kind="ok"):
        self.message = message
        self.keep_open = keep_open
        self.close = close
        self.kind = kind


# Herdr reports "nothing happened" as `changed: false` plus a reason. Without
# this, pressing a direction that has no neighbour looks exactly like a
# successful move, and the user is left wondering whether the key registered.
REASONS = {
    "no_neighbor": "nothing that way",
    "same_pane": "already there",
    "not_found": "that is gone",
    "cross_tab": "not in this tab",
    "single_pane": "only one pane",
    "already_zoomed": "already zoomed",
    "already_unzoomed": "not zoomed",
    "not_zoomed": "not zoomed",
    "no_pane": "no pane",
    "invalid": "not possible right now",
}


def _result_of(result):
    """Some methods nest their outcome under a key; find the one that has it."""
    if not isinstance(result, dict):
        return {}
    if "changed" in result:
        return result
    for value in result.values():
        if isinstance(value, dict) and "changed" in value:
            return value
    return result


def _describe(result, ok_message):
    """Turn a socket result into an Outcome, saying so when nothing happened.

    When an action was a no-op the overlay stays open: the user meant to move
    somewhere, and the useful next move is another key, not closing the menu.
    """
    payload = _result_of(result)
    if payload.get("changed") is False:
        reason = payload.get("reason")
        text = REASONS.get(reason, reason or "nothing happened")
        return Outcome(text, keep_open=True)
    return Outcome(ok_message)


# -- the map ----------------------------------------------------------------


class Call:
    """One socket call, described as data.

    `params` maps a parameter name to either a literal or a reference to the
    context (`"$pane_id"` and friends -- see `CONTEXT_VALUES`). `result_key`
    is the field of the response the call reads, for the calls that list.
    """

    def __init__(self, method, params=None, result_key=""):
        self.method = method
        self.params = dict(params or {})
        self.result_key = result_key

    def __repr__(self):
        return "Call(%r, %r)" % (self.method, self.params)


# A parameter that comes from the context rather than from a literal: the
# focused pane, the value the user typed, the id an action worked out from an
# earlier call. The type is what Herdr's schema has to accept.
CONTEXT_VALUES = {
    "$pane_id": ("string", lambda ctx, value, computed: ctx.pane_id),
    "$tab_id": ("string", lambda ctx, value, computed: ctx.tab_id),
    "$workspace_id": ("string", lambda ctx, value, computed: ctx.workspace_id),
    "$value": ("string", lambda ctx, value, computed: value),
    "$title": ("string", lambda ctx, value, computed: value or "managr"),
    "$target": ("string", lambda ctx, value, computed: computed.get("target")),
    "$index": ("integer", lambda ctx, value, computed: computed.get("index")),
    # The plugin action the custom layer is running. The rows of that layer
    # are not keymap actions: they are the user's own commands and the
    # plugins' actions, and the overlay says which entry a key stands for
    # before it runs it.
    "$plugin_id": ("string", lambda ctx, value, computed: _entry_field(ctx, "plugin_id")),
    "$plugin_action_id": (
        "string",
        lambda ctx, value, computed: _entry_field(ctx, "action_id"),
    ),
}


def _entry_field(ctx, name):
    """A field of the custom entry the overlay is running, or ""."""
    entry = getattr(ctx, "entry", None)
    return str(getattr(entry, name, "") or "")


def _resolve(params, ctx, value, computed=None):
    """The parameters a call would send, with the context's values filled in."""
    computed = computed or {}
    out = {}
    for name, spec in params.items():
        if isinstance(spec, str) and spec.startswith("$"):
            out[name] = CONTEXT_VALUES[spec][1](ctx, value, computed)
        else:
            out[name] = spec
    return out


def resolve(params, ctx=None, value=None, computed=None):
    """The parameters a call would send.

    The public face of `_resolve`, for the callers that hold a call and no
    context to run it in -- the list calls, whose parameters are literals.
    """
    return _resolve(params, ctx, value, computed)


class Action:
    """What an action does, described as data.

    `calls` are the socket calls it makes, in order. `destructive` says it
    creates or closes something. `safe` says it may be called on a live
    session: it only moves the focus or reads the state. `needs_input` is the
    prompt the overlay shows before it runs, if any.

    `runner` is the one thing that is not data: the few actions whose second
    call depends on the answer to the first. It is handed the action, so it
    takes the method and the parameter names from the declaration above.
    """

    def __init__(
        self,
        name,
        calls,
        destructive=False,
        safe=False,
        needs_input=None,
        message="",
        keep_open=False,
        describe=False,
        runner=None,
        offset=0,
    ):
        self.name = name
        self.calls = list(calls)
        self.destructive = destructive
        self.safe = safe
        self.needs_input = needs_input
        self.message = message
        self.keep_open = keep_open
        self.describe = describe
        self.runner = runner
        self.offset = offset

    @property
    def method(self):
        """The method that does the work: the last call of the action.

        An action that makes no socket call -- a shell command is a local
        process -- has none, and says so.
        """
        if not self.calls:
            return "(local)"
        return self.calls[-1].method

    def __repr__(self):
        return "Action(%r, %r)" % (self.name, self.method)


# The calls that read the session. They are shared by the actions that need
# them, so the method and its parameters are written down once.
LIST_TABS = Call("tab.list", {"workspace_id": "$workspace_id"}, result_key="tabs")
LIST_WORKSPACES = Call("workspace.list", {}, result_key="workspaces")
LIST_AGENTS = Call("agent.list", {}, result_key="agents")

# The calls the custom layer fetches its rows with: the actions of every
# installed plugin, and the plugins themselves -- the list says which of them
# are enabled.
LIST_PLUGIN_ACTIONS = Call("plugin.action.list", {}, result_key="actions")
LIST_PLUGINS = Call("plugin.list", {}, result_key="plugins")

# The calls the navigatr makes when something is rearranged. They are not
# actions: the destination is picked in the tree and not in a layer, so the
# call is written down here with a placeholder for the id the tree picked,
# and the check covers the shape of it -- a method Herdr does not have is a
# method Herdr does not have, whatever it is called from.
MOVE_PANE = Call(
    "pane.move",
    {
        "pane_id": "$pane_id",
        "destination": {"type": "tab", "tab_id": "", "split": "right"},
        "focus": False,
    },
)
MOVE_TAB = Call("tab.move", {"tab_id": "", "insert_index": 0})

# The calls that are not part of any action: they fetch data rather than
# doing something. The check covers them too, because a method Herdr does not
# have is a method Herdr does not have, whatever it is called from.
EXTRA_CALLS = {
    "list_plugin_actions": LIST_PLUGIN_ACTIONS,
    "list_plugins": LIST_PLUGINS,
    "move_pane": MOVE_PANE,
    "move_tab": MOVE_TAB,
}


class Context:
    """Everything an executor needs: the client and where we are."""

    def __init__(self, client, workspace_id=None, tab_id=None, pane_id=None, cwd=None):
        self.client = client
        self.workspace_id = workspace_id
        self.tab_id = tab_id
        self.pane_id = pane_id
        # The focused pane's working directory. Herdr runs a shell command
        # from there, and so does the overlay.
        self.cwd = cwd
        # The custom entry the overlay is running, if any: the plugin action
        # to invoke, or the user's own command. The overlay sets it before it
        # runs an entry, and the executors read what they need from it.
        self.entry = None

    def refresh(self):
        """Re-read the focused ids. Focus changes under us after any action."""
        try:
            result = self.client.call("pane.current")
        except (HerdrError, HerdrUnavailable):
            return
        pane = result.get("pane") or {}
        self.pane_id = pane.get("pane_id") or self.pane_id
        self.tab_id = pane.get("tab_id") or self.tab_id
        self.workspace_id = pane.get("workspace_id") or self.workspace_id
        self.cwd = pane.get("cwd") or pane.get("foreground_cwd") or self.cwd

    # -- lookups used by more than one executor ----------------------------

    def tabs(self):
        result = self.client.call(LIST_TABS.method, _resolve(LIST_TABS.params, self, None))
        return result.get(LIST_TABS.result_key, [])

    def workspaces(self):
        result = self.client.call(
            LIST_WORKSPACES.method, _resolve(LIST_WORKSPACES.params, self, None)
        )
        return result.get(LIST_WORKSPACES.result_key, [])


# -- the runners ------------------------------------------------------------
#
# One per action that cannot be a single call. Each one takes the method and
# the parameter names from the declaration, so the two cannot drift apart.


def _format(template, value):
    """The message an action shows, with the typed value in it if it takes one."""
    if "%" in template:
        return template % value
    return template


def _outcome(action, result, value):
    """The Outcome of an action that made its call."""
    if action.describe:
        # The move did nothing: say so, and stay open, because the useful
        # next move is another key.
        return _describe(result, action.message)
    return Outcome(_format(action.message, value), keep_open=action.keep_open)


def _run_relative_tab(action, ctx, value):
    """Focus the tab before or after this one, wrapping around."""
    list_call, focus_call = action.calls
    tabs = ctx.client.call(
        list_call.method, _resolve(list_call.params, ctx, value)
    ).get(list_call.result_key, [])
    if not tabs:
        return Outcome("no tabs")
    index = next((i for i, t in enumerate(tabs) if t.get("tab_id") == ctx.tab_id), 0)
    target = tabs[(index + action.offset) % len(tabs)]
    ctx.client.call(
        focus_call.method,
        _resolve(focus_call.params, ctx, value, {"target": target["tab_id"]}),
    )
    return Outcome(target.get("label") or target["tab_id"], keep_open=True)


def _run_relative_workspace(action, ctx, value):
    """Focus the space before or after this one, wrapping around."""
    list_call, focus_call = action.calls
    spaces = ctx.client.call(
        list_call.method, _resolve(list_call.params, ctx, value)
    ).get(list_call.result_key, [])
    if not spaces:
        return Outcome("no workspaces")
    index = next(
        (i for i, w in enumerate(spaces) if w.get("workspace_id") == ctx.workspace_id), 0
    )
    target = spaces[(index + action.offset) % len(spaces)]
    ctx.client.call(
        focus_call.method,
        _resolve(focus_call.params, ctx, value, {"target": target["workspace_id"]}),
    )
    return Outcome(target.get("label") or target["workspace_id"], keep_open=True)


def _run_switch_tab(action, ctx, value):
    """Jump to a tab by its 1-based number in the current workspace."""
    list_call, focus_call = action.calls
    tabs = ctx.client.call(
        list_call.method, _resolve(list_call.params, ctx, value)
    ).get(list_call.result_key, [])
    if not tabs:
        return Outcome("no tabs")
    index = max(0, min(len(tabs) - 1, int(value or 1) - 1))
    target = tabs[index]
    ctx.client.call(
        focus_call.method,
        _resolve(focus_call.params, ctx, value, {"target": target["tab_id"]}),
    )
    return Outcome(target.get("label") or target["tab_id"], keep_open=True)


def _run_switch_workspace(action, ctx, value):
    """Jump to a workspace by its 1-based number."""
    list_call, focus_call = action.calls
    spaces = ctx.client.call(
        list_call.method, _resolve(list_call.params, ctx, value)
    ).get(list_call.result_key, [])
    if not spaces:
        return Outcome("no workspaces")
    index = max(0, min(len(spaces) - 1, int(value or 1) - 1))
    target = spaces[index]
    ctx.client.call(
        focus_call.method,
        _resolve(focus_call.params, ctx, value, {"target": target["workspace_id"]}),
    )
    return Outcome(target.get("label") or target["workspace_id"], keep_open=True)


def _run_focus_agent(action, ctx, value):
    """Focus an agent by its 1-based number."""
    list_call, focus_call = action.calls
    agents = ctx.client.call(
        list_call.method, _resolve(list_call.params, ctx, value)
    ).get(list_call.result_key, [])
    if not agents:
        return Outcome("no agents")
    index = max(0, min(len(agents) - 1, int(value or 1) - 1))
    agent = agents[index]
    ctx.client.call(
        focus_call.method,
        _resolve(
            focus_call.params,
            ctx,
            value,
            {"target": agent.get("pane_id") or agent.get("agent")},
        ),
    )
    return Outcome(agent.get("agent", "agent"), keep_open=True)


def _run_move_tab(action, ctx, value):
    """Move this tab one place along the tab bar."""
    list_call, move_call = action.calls
    tabs = ctx.client.call(
        list_call.method, _resolve(list_call.params, ctx, value)
    ).get(list_call.result_key, [])
    index = next((i for i, t in enumerate(tabs) if t.get("tab_id") == ctx.tab_id), 0)
    target = max(0, min(len(tabs) - 1, index + action.offset))
    if target == index:
        return Outcome("already at the end", keep_open=True)
    # `tab.move` puts the tab BEFORE the tab at that index, and it counts
    # the moved tab as still being in the list. So the index that lands the
    # tab AT `target` is `target` when it is moving backwards, and one more
    # when it is moving forwards.
    #
    # Measured against a live server: a tab at index 1 with `insert_index`
    # 2 stayed at index 1, and with `insert_index` 3 moved to index 2.
    insert_index = target if target < index else target + 1
    ctx.client.call(
        move_call.method, _resolve(move_call.params, ctx, value, {"index": insert_index})
    )
    return Outcome("moved", keep_open=True)


def _run_declared(action):
    """The thin runner: make the declared call with the declared parameters."""
    call = action.calls[0]

    def run(ctx, value=None):
        result = ctx.client.call(call.method, _resolve(call.params, ctx, value))
        return _outcome(action, result, value)

    return run


# -- the custom layer's runners ---------------------------------------------
#
# The rows of the custom layer are not keymap actions: they are the user's
# own commands and the actions of the installed plugins. They still run
# through this map, so there is one mechanism and not two.


def _run_plugin_action(action, ctx, value):
    """Invoke a plugin action.

    **The action is started, not awaited.** `plugin.action.invoke`
    launches the action's own process and returns as soon as it is
    launched: the log record it hands back says `status: "running"`, and
    the outcome only turns up later in `plugin.log.list`. So whatever
    happens *inside* that process is not in the invoke's answer -- a
    plugin action that wants a popup of its own is refused with
    `ui_busy` in the action's process, while the invoke itself succeeds.

    So the overlay says the action was *started*, not that it ran: it
    cannot know the outcome, and a message that claims the action ran is
    a lie the user acts on.
    """
    call = action.calls[0]
    ctx.client.call(call.method, _resolve(call.params, ctx, value))
    entry = getattr(ctx, "entry", None)
    title = str(getattr(entry, "desc", "") or "")
    return Outcome(("started %s" % title).strip(), keep_open=True)


def _herdr_bin_path(env):
    """The path of Herdr's own binary, the way a custom command wants it.

    Herdr's docs say a custom command receives `HERDR_BIN_PATH`. The
    overlay is launched by the server, so it usually inherits the
    variable -- but "usually" is not a contract, and the user's own
    command is the thing that needs it, so the path is worked out rather
    than inherited by accident.
    """
    existing = str(env.get("HERDR_BIN_PATH") or "").strip()
    if existing:
        return existing
    return shutil.which("herdr") or ""


def _run_shell_command(action, ctx, value):
    """Run the user's own shell command, the way Herdr runs it.

    A `type = "shell"` command runs detached in the background, through
    `/bin/sh -lc`, and it does not block the key that started it. The
    overlay does the same: the command is a child process in a session of
    its own, and the overlay does not wait for it.

    It runs from the focused pane's working directory, the way Herdr runs
    it: a command that means "where am I" has to answer the same question
    Herdr would answer.

    The child's environment is Herdr's context: `HERDR_SOCKET_PATH`,
    `HERDR_BIN_PATH`, `HERDR_ACTIVE_WORKSPACE_ID`, `HERDR_ACTIVE_TAB_ID`,
    `HERDR_ACTIVE_PANE_ID` and `HERDR_ACTIVE_PANE_CWD` -- the six
    variables Herdr's docs promise a custom command. They are set here
    rather than inherited, because the overlay is the server's child and
    not the user's shell.

    The child's output goes nowhere: the overlay is a popup, and a line of
    the command's output would land in the middle of the frame.
    """
    entry = getattr(ctx, "entry", None)
    command = str(getattr(entry, "command", "") or "")
    if not command:
        return Outcome("there is no command to run", keep_open=True, kind="err")

    env = dict(os.environ)
    socket_path = str(getattr(ctx.client, "socket_path", "") or "")
    if socket_path:
        env["HERDR_SOCKET_PATH"] = socket_path
    bin_path = _herdr_bin_path(env)
    if bin_path:
        env["HERDR_BIN_PATH"] = bin_path
    for name, identifier in (
        ("HERDR_ACTIVE_WORKSPACE_ID", ctx.workspace_id),
        ("HERDR_ACTIVE_TAB_ID", ctx.tab_id),
        ("HERDR_ACTIVE_PANE_ID", ctx.pane_id),
        # The working directory is the same value the command runs from,
        # so a command that asks "where am I" gets the same answer from
        # the environment that it gets from the process.
        ("HERDR_ACTIVE_PANE_CWD", ctx.cwd),
    ):
        if identifier:
            env[name] = str(identifier)

    try:
        subprocess.Popen(
            ["/bin/sh", "-lc", command],
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            cwd=ctx.cwd or None,
            # Its own session: the command is detached from the overlay's
            # terminal, and it outlives the overlay.
            start_new_session=True,
        )
    except OSError as exc:
        return Outcome(
            "could not run %r: %s" % (command, exc),
            keep_open=True,
            kind="err",
        )
    return Outcome("ran %r" % command, keep_open=True)


def _run_window_command(action, ctx, value):
    """A popup or a pane command cannot be run from inside the overlay.

    The overlay *is* a popup, and a popup is a singleton in Herdr: only one
    can be open, and the second attempt is refused with `ui_busy`. A pane
    command cannot be run from here either -- there is no socket method
    that opens a pane and runs a command in it.

    So the command is not attempted, and it does not fail silently: the
    user is told where the key is, because the key has to be pressed in
    Herdr.
    """
    entry = getattr(ctx, "entry", None)
    kind = str(getattr(entry, "command_type", "") or "window")
    chord = str(getattr(entry, "chord", "") or "the key it is bound to")
    reasons = {
        "popup": "the overlay is a popup, and only one can be open",
        "pane": "the overlay is a popup, and there is no way to open a pane from it",
    }
    reason = reasons.get(kind, "the overlay is a popup")
    return Outcome(
        "a %s command cannot be run from here: %s; press %s in herdr"
        % (kind, reason, chord),
        keep_open=True,
        kind="err",
    )


def _executor(action):
    """The function the overlay runs for this action."""
    if action.runner is not None:
        return lambda ctx, value=None: action.runner(action, ctx, value)
    return _run_declared(action)


# -- the actions ------------------------------------------------------------


ACTIONS = {
    # -- pane actions ------------------------------------------------------
    "split_vertical": Action(
        "split_vertical",
        [Call("pane.split", {"direction": "right", "target_pane_id": "$pane_id", "focus": True})],
        destructive=True,
        message="split right",
    ),
    "split_horizontal": Action(
        "split_horizontal",
        [Call("pane.split", {"direction": "down", "target_pane_id": "$pane_id", "focus": True})],
        destructive=True,
        message="split down",
    ),
    "close_pane": Action(
        "close_pane",
        [Call("pane.close", {"pane_id": "$pane_id"})],
        destructive=True,
        message="pane closed",
    ),
    "focus_pane_left": Action(
        "focus_pane_left",
        [Call("pane.focus_direction", {"direction": "left"})],
        safe=True,
        describe=True,
        message="focus left",
    ),
    "focus_pane_down": Action(
        "focus_pane_down",
        [Call("pane.focus_direction", {"direction": "down"})],
        safe=True,
        describe=True,
        message="focus down",
    ),
    "focus_pane_up": Action(
        "focus_pane_up",
        [Call("pane.focus_direction", {"direction": "up"})],
        safe=True,
        describe=True,
        message="focus up",
    ),
    "focus_pane_right": Action(
        "focus_pane_right",
        [Call("pane.focus_direction", {"direction": "right"})],
        safe=True,
        describe=True,
        message="focus right",
    ),
    "swap_pane_left": Action(
        "swap_pane_left",
        [Call("pane.swap", {"direction": "left", "pane_id": "$pane_id"})],
        describe=True,
        message="swap left",
    ),
    "swap_pane_down": Action(
        "swap_pane_down",
        [Call("pane.swap", {"direction": "down", "pane_id": "$pane_id"})],
        describe=True,
        message="swap down",
    ),
    "swap_pane_up": Action(
        "swap_pane_up",
        [Call("pane.swap", {"direction": "up", "pane_id": "$pane_id"})],
        describe=True,
        message="swap up",
    ),
    "swap_pane_right": Action(
        "swap_pane_right",
        [Call("pane.swap", {"direction": "right", "pane_id": "$pane_id"})],
        describe=True,
        message="swap right",
    ),
    "resize_pane_left": Action(
        "resize_pane_left",
        [Call("pane.resize", {"direction": "left", "amount": 0.05})],
        describe=True,
        message="resize left",
    ),
    "resize_pane_down": Action(
        "resize_pane_down",
        [Call("pane.resize", {"direction": "down", "amount": 0.05})],
        describe=True,
        message="resize down",
    ),
    "resize_pane_up": Action(
        "resize_pane_up",
        [Call("pane.resize", {"direction": "up", "amount": 0.05})],
        describe=True,
        message="resize up",
    ),
    "resize_pane_right": Action(
        "resize_pane_right",
        [Call("pane.resize", {"direction": "right", "amount": 0.05})],
        describe=True,
        message="resize right",
    ),
    "zoom": Action(
        "zoom",
        [Call("pane.zoom", {"mode": "toggle", "pane_id": "$pane_id"})],
        describe=True,
        message="zoom toggled",
    ),
    "clear_pane": Action(
        "clear_pane",
        [Call("pane.clear", {"pane_id": "$pane_id"})],
        destructive=True,
        message="cleared",
    ),
    "edit_scrollback": Action(
        "edit_scrollback",
        [Call("pane.edit_scrollback", {"pane_id": "$pane_id"})],
        destructive=True,
        message="scrollback in $EDITOR",
    ),
    "rename_pane": Action(
        "rename_pane",
        [Call("pane.rename", {"pane_id": "$pane_id", "label": "$value"})],
        needs_input="pane name",
        message="renamed to %r",
    ),
    # -- tab actions -------------------------------------------------------
    "new_tab": Action(
        "new_tab",
        [Call("tab.create", {"workspace_id": "$workspace_id", "focus": True})],
        destructive=True,
        message="tab created",
    ),
    "close_tab": Action(
        "close_tab",
        [Call("tab.close", {"tab_id": "$tab_id"})],
        destructive=True,
        message="tab closed",
    ),
    "rename_tab": Action(
        "rename_tab",
        [Call("tab.rename", {"tab_id": "$tab_id", "label": "$value"})],
        needs_input="tab name",
        message="renamed to %r",
    ),
    "next_tab": Action(
        "next_tab",
        [LIST_TABS, Call("tab.focus", {"tab_id": "$target"})],
        safe=True,
        keep_open=True,
        runner=_run_relative_tab,
        offset=1,
    ),
    "previous_tab": Action(
        "previous_tab",
        [LIST_TABS, Call("tab.focus", {"tab_id": "$target"})],
        safe=True,
        keep_open=True,
        runner=_run_relative_tab,
        offset=-1,
    ),
    "move_tab_previous": Action(
        "move_tab_previous",
        [LIST_TABS, Call("tab.move", {"tab_id": "$tab_id", "insert_index": "$index"})],
        keep_open=True,
        runner=_run_move_tab,
        offset=-1,
    ),
    "move_tab_next": Action(
        "move_tab_next",
        [LIST_TABS, Call("tab.move", {"tab_id": "$tab_id", "insert_index": "$index"})],
        keep_open=True,
        runner=_run_move_tab,
        offset=1,
    ),
    "switch_tab": Action(
        "switch_tab",
        [LIST_TABS, Call("tab.focus", {"tab_id": "$target"})],
        safe=True,
        keep_open=True,
        runner=_run_switch_tab,
    ),
    # -- workspace actions --------------------------------------------------
    "new_workspace": Action(
        "new_workspace",
        [Call("workspace.create", {"focus": True})],
        destructive=True,
        message="workspace created",
    ),
    "close_workspace": Action(
        "close_workspace",
        [Call("workspace.close", {"workspace_id": "$workspace_id"})],
        destructive=True,
        message="workspace closed",
    ),
    "rename_workspace": Action(
        "rename_workspace",
        [Call("workspace.rename", {"workspace_id": "$workspace_id", "label": "$value"})],
        needs_input="space name",
        message="renamed to %r",
    ),
    "new_worktree": Action(
        "new_worktree",
        [Call("worktree.create", {"branch": "$value", "focus": True})],
        destructive=True,
        needs_input="branch name",
        message="worktree %r",
    ),
    "previous_workspace": Action(
        "previous_workspace",
        [LIST_WORKSPACES, Call("workspace.focus", {"workspace_id": "$target"})],
        safe=True,
        keep_open=True,
        runner=_run_relative_workspace,
        offset=-1,
    ),
    "next_workspace": Action(
        "next_workspace",
        [LIST_WORKSPACES, Call("workspace.focus", {"workspace_id": "$target"})],
        safe=True,
        keep_open=True,
        runner=_run_relative_workspace,
        offset=1,
    ),
    "switch_workspace": Action(
        "switch_workspace",
        [LIST_WORKSPACES, Call("workspace.focus", {"workspace_id": "$target"})],
        safe=True,
        keep_open=True,
        runner=_run_switch_workspace,
    ),
    # -- session actions ----------------------------------------------------
    "reload_config": Action(
        "reload_config",
        [Call("server.reload_config", {})],
        message="config reloaded",
    ),
    "notify": Action(
        "notify",
        [Call("notification.show", {"title": "$title"})],
        needs_input="message",
        message="notified",
    ),
    "focus_agent": Action(
        "focus_agent",
        [LIST_AGENTS, Call("agent.focus", {"target": "$target"})],
        safe=True,
        needs_input="agent number",
        keep_open=True,
        runner=_run_focus_agent,
    ),
    # -- the custom layer ---------------------------------------------------
    #
    # The rows of the custom layer are the user's own commands and the
    # actions of the installed plugins. They are not keymap actions, but
    # they run through this map like everything else.
    #
    # The two that are not socket calls say so: a shell command is a local
    # process, and a popup or a pane command cannot be run from inside the
    # overlay at all.
    "invoke_plugin_action": Action(
        "invoke_plugin_action",
        [
            Call(
                "plugin.action.invoke",
                {"action_id": "$plugin_action_id", "plugin_id": "$plugin_id"},
            )
        ],
        keep_open=True,
        runner=_run_plugin_action,
    ),
    "run_shell_command": Action(
        "run_shell_command",
        [],
        keep_open=True,
        runner=_run_shell_command,
    ),
    "run_window_command": Action(
        "run_window_command",
        [],
        keep_open=True,
        runner=_run_window_command,
    ),
}

# The names of the actions the custom layer runs. The custom layer's rows
# are built at runtime, and they name the action they run.
PLUGIN_ACTION = "invoke_plugin_action"
SHELL_COMMAND = "run_shell_command"
WINDOW_COMMAND = "run_window_command"

# The runners that are not Herdr actions.
#
# They are what the custom layer runs: a plugin's action, a shell command
# and a window command. None of them is one of Herdr's own actions -- they
# are local processes and a plugin invocation -- so they are not part of
# the public set that `make explain` counts, and they are listed apart.
INTERNAL_ACTIONS = (PLUGIN_ACTION, SHELL_COMMAND, WINDOW_COMMAND)

# The functions the overlay runs, one per action. They are thin: the method
# and the parameters come from the declaration above.
EXECUTORS = {name: _executor(action) for name, action in ACTIONS.items()}


def public_executors():
    """The executors that are Herdr's own actions.

    Everything but the internal runners: the count in `make explain` is the
    number of Herdr actions the overlay can reach, and a local shell
    command is not one of them.
    """
    return {
        name: executor
        for name, executor in EXECUTORS.items()
        if name not in INTERNAL_ACTIONS
    }

# Actions that ask the user for a value before running.
NEEDS_INPUT = {
    name: action.needs_input for name, action in ACTIONS.items() if action.needs_input
}
