"""The session navigatr: the places and the agents of the running session.

The overlay's layers map keys to actions. The navigatr is something else: a
list of destinations with a cursor on it. A layer answers "press this to do
that"; the navigatr answers "this is where you are, and this is where you
could go".

**There are two of them.** The *places* navigatr is the tree: the spaces,
the tabs and the panes. The *agents* navigatr is a flat list of every agent
in the session, the ones that need attention first. Both are lists of
destinations with a cursor on them, so they share the keys, the footer and
the filter, and the overlay keeps one navigatr state and asks it which kind
it is.

**The agent is folded into the pane that hosts it.** The tree is three
levels deep, not four: the agent's name is the pane row's description and
the agent's status is the pane row's status. `agent.focus` and `pane.focus`
land on the same pane -- the agent's pane is the pane that hosts it -- so
folding the agent away loses nothing, and the agent navigatr is where an
agent that wants attention is found.

**It lives inside our own overlay, not in another plugin's popup.** A popup
is a singleton session resource in Herdr: only one can be open, and a second
one fails with `ui_busy`. Our overlay already holds the one popup, so
opening somebody else's navigatr from inside it cannot work.

The data comes from the live session over the socket API, in four calls:
`workspace.list`, `tab.list`, `pane.list` and `agent.list`. The tree is
spaces -> tabs -> panes, and the agent of a pane is folded into the pane's
row.

**The data is fetched when the navigatr is opened, not on every render.**
A render happens on every keystroke, and a socket round trip per keystroke
would make the overlay feel like it is lagging. Re-entering the navigatr
fetches it again, so the tree is never staler than the last visit.
"""

import os
from dataclasses import dataclass, field

from .client import HerdrError, HerdrUnavailable

# The kinds of row in the tree, and the socket method that goes to each one.
#
# The parameter names were read off the live server, not guessed: a call
# with the wrong name is rejected with `missing field`. `workspace.focus`
# wants `workspace_id`, `tab.focus` wants `tab_id`, `pane.focus` wants
# `pane_id`, and `agent.focus` wants `target` -- the agent's pane id.
WORKSPACE = "workspace"
TAB = "tab"
PANE = "pane"
AGENT = "agent"

FOCUS_METHODS = {
    WORKSPACE: "workspace.focus",
    TAB: "tab.focus",
    PANE: "pane.focus",
    AGENT: "agent.focus",
}

# The two navigatrs. The value is what `Navigatr.kind` says, and the
# overlay asks it which of the two it is drawing.
#
# PLACES is the tree, AGENTS is the flat list. The row kind of an agent is
# AGENT -- one letter apart, and a different thing.
PLACES = "places"
AGENTS = "agents"

# Herdr reports five agent states -- idle, working, blocked, done and
# unknown -- and the plugin's glyph vocabulary names all of them. A state
# the vocabulary does not name is drawn as `unknown`, which is what the
# herdr-navigator does too.
#
# The needles are matched inside the status text, because Herdr reports
# things like "blocked" and "needs_attention".
#
# **The order of the categories is the attention order.** A blocked agent
# wants the user now, a working one is busy, a done one is finished, an
# idle one is waiting for something, and an unknown one says nothing. The
# agents navigatr is sorted by it, so the rows that want attention are at
# the top.
STATUS_CATEGORIES = (
    ("blocked", ("block", "attention", "error", "fail", "request", "wait")),
    ("working", ("working", "running", "busy")),
    ("done", ("done", "complete")),
    ("idle", ("idle",)),
)

# The attention order, with the fallback at the end. This is what the
# agents list is sorted by.
ATTENTION_ORDER = ("blocked", "working", "done", "idle", "unknown")


def status_category(status):
    """The vocabulary entry for a status string.

    One of blocked, working, done, idle or unknown. The order of the
    categories is the attention order, so the first one that matches wins.
    """
    text = (status or "").lower()
    for category, needles in STATUS_CATEGORIES:
        if any(needle in text for needle in needles):
            return category
    return "unknown"


def attention_rank(status):
    """How urgent a status is: 0 is the most urgent."""
    category = status_category(status)
    try:
        return ATTENTION_ORDER.index(category)
    except ValueError:  # pragma: no cover - the fallback is in the order
        return len(ATTENTION_ORDER)


@dataclass
class Row:
    """One row of a navigatr.

    `depth` is how deep the row sits: in the places tree a space is 0, a
    tab 1 and a pane 2. `is_last` says whether the row is the last of its
    siblings, and `ancestors_last` says the same for each of its ancestors
    -- that is what draws the tree. `search` is the whole text of the row:
    the names of the spaces, tabs, panes and agents above it, plus its own.
    The filter matches against that, so a query finds a pane by the name of
    the space it is in.

    `desc` is what the row holds -- the counts of a space or a tab, the
    agent of a pane, the place of an agent -- and `agent` is the name of the
    agent folded into the row.
    """

    kind: str
    label: str
    method: str = ""
    params: dict = field(default_factory=dict)
    depth: int = 0
    is_last: bool = True
    ancestors_last: tuple = ()
    current: bool = False
    status: str = ""
    search: str = ""
    desc: str = ""
    agent: str = ""
    # Whether the cursor can stop on this row.
    #
    # A tab row is not a destination: it is the group label of the panes
    # under it, and a tab always has at least one pane, so focusing a
    # tab lands on a pane anyway. The cursor skips it, and `enter` never
    # goes to one. The space and the pane rows are destinations: the
    # spaces are what the number keys aim at, and the panes are what the
    # user is really navigating to.
    selectable: bool = True
    # Where this row is, as a path: the space, the tab and the row's own
    # name. It is the detail region at the bottom of the panel, and it is
    # the full name -- the row itself is cut to fit.
    path: str = ""
    # The directory the row's pane is in. The row does not show it; there
    # is no room for it there.
    cwd: str = ""


@dataclass
class Navigatr:
    """The navigatr's state: the rows, the cursor on them, and the error."""

    rows: list = field(default_factory=list)
    cursor: int = 0
    error: str = ""
    # Which navigatr this is: the tree of places, or the list of agents.
    kind: str = PLACES
    # What the list holds, in one line. It goes in the header, on the
    # right, the way Herdr's own navigation surface counts its terminals.
    summary: str = ""


def _listing(result, key):
    """The list in a socket result, or an empty one."""
    if not isinstance(result, dict):
        return []
    value = result.get(key)
    if not isinstance(value, list):
        return []
    return [entry for entry in value if isinstance(entry, dict)]


def _id(entry, key):
    """The id in `entry`, as a string.

    The ids are compared with each other -- a pane's tab against the tab's
    own id -- so they have to be in one form. An entry without the id has
    the empty one, which matches no tab.
    """
    return str(entry.get(key) or "")


def _pane_label(pane):
    """What a pane is called: its label, its title, its directory, its id.

    Herdr does not give a pane a name of its own, so the title of its
    terminal is the closest thing to one, and the directory is the next.
    """
    for key in ("label", "terminal_title_stripped"):
        value = pane.get(key)
        if value:
            return str(value)
    cwd = pane.get("cwd") or ""
    if cwd:
        return os.path.basename(cwd.rstrip("/")) or cwd
    return str(pane.get("pane_id") or "pane")


def _agent_label(agent):
    """What an agent is called: its name, or the kind of agent it is.

    Herdr reports the agent's name in `agent`, and `name` is the other
    field it may use for it. An agent with neither is called "agent": a row
    that says nothing is a row the user cannot read.
    """
    for key in ("agent", "name"):
        value = agent.get(key)
        if value:
            return str(value)
    return "agent"


def _plural(count, word):
    """`count` of `word`: "1 tab", "2 tabs", "0 panes".

    A count of zero is still said: a row that says nothing is a row the
    user cannot read.
    """
    return "%d %s%s" % (count, word, "" if count == 1 else "s")


def _counts(*pairs):
    """What a row holds, as one string: "2 tabs · 3 panes · 1 agent"."""
    return " · ".join(_plural(count, word) for count, word in pairs)


def _pane_row(
    pane,
    agents_by_pane,
    depth,
    is_last,
    ancestors_last,
    search_prefix,
    path_prefix="",
):
    """A pane's row, with the agent in it folded into the row.

    `depth` is how deep the pane sits, `is_last` whether it is the last of
    its siblings and `ancestors_last` the same for each of its ancestors.
    `search_prefix` is what the filter matches the row against, before the
    pane's own name, and `path_prefix` is the same path written for a
    person to read: "space / tab".

    Returns (row, agents): the number of agents folded into the row is
    counted by the space and the tab above it.
    """
    pane_id = _id(pane, "pane_id")
    pane_label = _pane_label(pane)

    names = []
    status = ""
    for agent in agents_by_pane.get(pane_id, []):
        names.append(_agent_label(agent))
        if not status:
            status = str(agent.get("agent_status") or "")
    if not names:
        # The pane may report an agent that the agent list does not hold --
        # the two lists are two round trips apart. The agent is still there,
        # and "nothing is dropped" still holds.
        name = str(pane.get("agent") or "")
        if name:
            names.append(name)
            status = str(pane.get("agent_status") or "")

    row = Row(
        kind=PANE,
        label=pane_label,
        method=FOCUS_METHODS[PANE],
        params={"pane_id": pane_id},
        depth=depth,
        is_last=is_last,
        ancestors_last=ancestors_last,
        current=bool(pane.get("focused")),
        status=status,
        desc=", ".join(names),
        agent=names[0] if names else "",
    )
    # The filter finds the pane by its own title *and* by the agent in it:
    # the agent is on the row, so it has to be in the row's text.
    row.search = "%s %s %s" % (search_prefix, pane_label, " ".join(names))
    # The path of the row, with the pane's own name at the end: the
    # detail region shows it in full, and the row cannot.
    row.path = "%s / %s" % (path_prefix, pane_label) if path_prefix else pane_label
    row.cwd = str(pane.get("cwd") or pane.get("foreground_cwd") or "")
    return row, len(names)


def build(workspaces, tabs, panes, agents):
    """The places navigatr's rows, from the four list responses.

    The tree is spaces -> tabs -> panes, and the agent of a pane is folded
    into the pane's row.

    **Every row says what is inside it.** A space says how many tabs, panes
    and agents it holds, a tab says how many panes and agents, and a pane
    says which agent is in it. The counts are computed from the four lists
    the plugin already has, not from Herdr's own `tab_count` and `pane_count`
    fields: the rows drawn and the counts must agree, and the session can
    change between two of the four round trips.

    **Nothing is dropped.** A row that is not shown is a place the user
    cannot go, and the four calls are four round trips: the session can
    change between two of them. So:

    - a pane whose tab Herdr did not list is put under its space, with the
      tabs -- a tab closed in between leaves its pane loose;
    - a tab whose space Herdr did not list is put at the top level, with
      no space above it, because the user may have closed that space a
      moment ago;
    - a pane whose space *and* tab were both gone is put at the top level
      too.
    """
    rows = []

    tabs_by_space = {}
    for tab in tabs:
        tabs_by_space.setdefault(_id(tab, "workspace_id"), []).append(tab)
    panes_by_tab = {}
    for pane in panes:
        panes_by_tab.setdefault(_id(pane, "tab_id"), []).append(pane)
    agents_by_pane = {}
    for agent in agents:
        agents_by_pane.setdefault(_id(agent, "pane_id"), []).append(agent)

    # The id of EVERY tab the server listed, in every space.
    #
    # A pane belongs to the tab it names, wherever that tab lives. So a
    # pane whose space and tab disagree -- a pane that says it is in
    # space A but names a tab of space B -- is drawn under space B, with
    # that tab, and NOT again as a loose pane of space A. Drawing it
    # twice put the same destination in the tree twice and counted it in
    # two spaces, and a tree that shows a place twice is a tree the user
    # cannot trust.
    every_tab_id = set(_id(tab, "tab_id") for tab in tabs)

    for space in workspaces:
        space_id = _id(space, "workspace_id")
        space_label = str(space.get("label") or space_id or "?")

        space_tabs = tabs_by_space.get(space_id, [])

        # A pane whose tab Herdr did not list. It is put under its space
        # rather than dropped: a row that is not shown is a place the user
        # cannot go.
        loose = [
            pane
            for pane in panes
            if _id(pane, "workspace_id") == space_id
            and _id(pane, "tab_id") not in every_tab_id
        ]

        # The children of the space are the tabs *and* the loose panes, so
        # the last-child flag is decided over both of them. Deciding it
        # over the tabs alone drew two last children under one space: the
        # last tab and the last loose pane both claimed to be the end of
        # the branch.
        children = len(space_tabs) + len(loose)

        space_panes = 0
        space_agents = 0
        # The rows under the space. The space's own row comes first, so
        # they are collected and added after it.
        under = []

        for index, tab in enumerate(space_tabs):
            tab_id = _id(tab, "tab_id")
            tab_label = str(tab.get("label") or tab_id or "?")
            tab_last = index == children - 1
            tab_panes = panes_by_tab.get(tab_id, [])

            pane_rows = []
            tab_agents = 0
            for pane_index, pane in enumerate(tab_panes):
                pane_row, folded = _pane_row(
                    pane,
                    agents_by_pane,
                    depth=2,
                    is_last=pane_index == len(tab_panes) - 1,
                    ancestors_last=(tab_last,),
                    search_prefix="%s %s" % (space_label, tab_label),
                    path_prefix="%s / %s" % (space_label, tab_label),
                )
                tab_agents += folded
                pane_rows.append(pane_row)

            tab_row = Row(
                kind=TAB,
                label=tab_label,
                method=FOCUS_METHODS[TAB],
                params={"tab_id": tab_id},
                depth=1,
                is_last=tab_last,
                current=bool(tab.get("focused")),
                # A tab is a group label, not a place: the cursor skips it
                # and `enter` never goes to one. The panes under it are
                # the places.
                selectable=False,
                # The tab's own status: the aggregate Herdr reports for it.
                status=str(tab.get("agent_status") or ""),
                desc=_counts((len(tab_panes), "pane"), (tab_agents, "agent")),
            )
            tab_row.search = "%s %s" % (space_label, tab_label)
            tab_row.path = "%s / %s" % (space_label, tab_label)
            under.append(tab_row)
            under.extend(pane_rows)

            space_panes += len(tab_panes)
            space_agents += tab_agents

        for index, pane in enumerate(loose):
            pane_row, folded = _pane_row(
                pane,
                agents_by_pane,
                depth=1,
                # The loose panes come after the tabs, so their place in
                # the branch is after them too.
                is_last=len(space_tabs) + index == children - 1,
                ancestors_last=(),
                search_prefix=space_label,
                path_prefix=space_label,
            )
            under.append(pane_row)
            space_panes += 1
            space_agents += folded

        space_row = Row(
            kind=WORKSPACE,
            label=space_label,
            method=FOCUS_METHODS[WORKSPACE],
            params={"workspace_id": space_id},
            depth=0,
            current=bool(space.get("focused")),
            # The space's own status: the aggregate Herdr reports for it.
            status=str(space.get("agent_status") or ""),
            desc=_counts(
                (len(space_tabs), "tab"),
                (space_panes, "pane"),
                (space_agents, "agent"),
            ),
        )
        space_row.search = space_label
        space_row.path = space_label
        rows.append(space_row)
        rows.extend(under)

    # The spaces that are gone. A tab whose space Herdr did not list is
    # still a place the user can go -- the user may have closed that
    # space a moment ago -- so it is kept, at the top level with no space
    # above it, rather than dropped.
    listed_space_ids = set(_id(space, "workspace_id") for space in workspaces)
    listed_tab_ids = set(_id(tab, "tab_id") for tab in tabs)
    orphan_tabs = [
        tab for tab in tabs if _id(tab, "workspace_id") not in listed_space_ids
    ]
    orphan_panes = [
        pane
        for pane in panes
        if _id(pane, "workspace_id") not in listed_space_ids
        and _id(pane, "tab_id") not in listed_tab_ids
    ]
    orphans = len(orphan_tabs) + len(orphan_panes)

    for index, tab in enumerate(orphan_tabs):
        tab_id = _id(tab, "tab_id")
        tab_label = str(tab.get("label") or tab_id or "?")
        tab_last = index == orphans - 1
        tab_panes = panes_by_tab.get(tab_id, [])

        pane_rows = []
        tab_agents = 0
        for pane_index, pane in enumerate(tab_panes):
            pane_row, folded = _pane_row(
                pane,
                agents_by_pane,
                depth=1,
                is_last=pane_index == len(tab_panes) - 1,
                ancestors_last=(tab_last,),
                search_prefix=tab_label,
                path_prefix=tab_label,
            )
            tab_agents += folded
            pane_rows.append(pane_row)

        tab_row = Row(
            kind=TAB,
            label=tab_label,
            method=FOCUS_METHODS[TAB],
            params={"tab_id": tab_id},
            depth=0,
            is_last=tab_last,
            current=bool(tab.get("focused")),
            # This tab's space is gone, so there is nothing above it and
            # nothing under it: it is the only row that names that place,
            # and a place the user cannot go to is a place that is
            # dropped. So it stays a destination, even though a tab with
            # a space above it is only a heading.
            status=str(tab.get("agent_status") or ""),
            desc=_counts((len(tab_panes), "pane"), (tab_agents, "agent")),
        )
        # There is no space to put the space's name in: the space is gone.
        tab_row.search = tab_label
        tab_row.path = tab_label
        rows.append(tab_row)
        rows.extend(pane_rows)

    for index, pane in enumerate(orphan_panes):
        pane_row, _folded = _pane_row(
            pane,
            agents_by_pane,
            depth=0,
            is_last=len(orphan_tabs) + index == orphans - 1,
            ancestors_last=(),
            search_prefix="",
            path_prefix="",
        )
        rows.append(pane_row)

    return rows


def build_agents(workspaces, tabs, panes, agents):
    """The agents navigatr's rows: every agent in the session.

    A flat list: no tree, no indentation, no branch markers. Every row is
    an agent, so the row says which agent it is, what state it is in, and
    where it is.

    **The rows are sorted by attention.** A blocked agent wants the user
    now, so it is at the top; then the working ones, the done ones, the
    idle ones and the unknown ones. Within one level the order is the
    space's label, then the tab's label, then the agent's name, so the
    list does not shuffle itself between two renders of the same session.

    **Nothing is dropped.** A space or a tab that is not in the lists any
    more is shown as "?", and an agent whose pane is not in the list still
    has a row: `agent.focus` takes the pane id, so the row's target is the
    agent's own pane.
    """
    space_labels = {}
    for space in workspaces:
        space_id = _id(space, "workspace_id")
        space_labels[space_id] = str(space.get("label") or space_id or "?")
    tab_labels = {}
    for tab in tabs:
        tab_id = _id(tab, "tab_id")
        tab_labels[tab_id] = str(tab.get("label") or tab_id or "?")
    panes_by_id = {}
    for pane in panes:
        panes_by_id[_id(pane, "pane_id")] = pane

    ordered = []
    for agent in agents:
        pane_id = _id(agent, "pane_id")
        pane = panes_by_id.get(pane_id, {})
        # The agent says which space and tab it is in. The pane says so
        # too, and one of the two lists may be the one that is behind.
        space_id = _id(agent, "workspace_id") or _id(pane, "workspace_id")
        tab_id = _id(agent, "tab_id") or _id(pane, "tab_id")
        space_label = space_labels.get(space_id, "?")
        tab_label = tab_labels.get(tab_id, "?")
        label = _agent_label(agent)
        status = str(agent.get("agent_status") or "")

        row = Row(
            kind=AGENT,
            label=label,
            method=FOCUS_METHODS[AGENT],
            # `agent.focus` takes the agent's pane: the agent is in a
            # pane, and that is what can be focused.
            params={"target": pane_id},
            depth=0,
            current=bool(agent.get("focused")),
            status=status,
            desc="%s › %s" % (space_label, tab_label),
            agent=label,
        )
        row.search = "%s %s %s %s" % (
            label,
            space_label,
            tab_label,
            str(agent.get("terminal_title_stripped") or ""),
        )
        # The path of the row, and the directory the agent works in. The
        # row itself shows the location and is cut to fit; these are the
        # whole of it.
        row.path = "%s / %s / %s" % (space_label, tab_label, label)
        row.cwd = str(
            agent.get("cwd")
            or agent.get("foreground_cwd")
            or pane.get("cwd")
            or pane.get("foreground_cwd")
            or ""
        )
        ordered.append(
            ((attention_rank(status), space_label, tab_label, label), row)
        )

    ordered.sort(key=lambda pair: pair[0])
    return [row for _key, row in ordered]


def summary(rows, kind=PLACES):
    """What the list holds, in one line: the count in the header.

    Herdr's own navigation surface says how many terminals it is showing,
    on the right of its header, and it is the cheapest thing in the window
    to be useful: it tells you how much you are looking at before you
    start looking at it.
    """
    if kind == AGENTS:
        total = len(rows)
        blocked = sum(1 for row in rows if status_category(row.status) == "blocked")
        if total and blocked:
            return "%d agents · %d blocked" % (total, blocked)
        return "%d agents" % total
    spaces = sum(1 for row in rows if row.kind == WORKSPACE)
    panes = sum(1 for row in rows if row.kind == PANE)
    return "%d spaces · %d panes" % (spaces, panes)


def fetch(client, kind=PLACES):
    """The navigatr, with the live session in it. It never raises.

    A list call can fail: the socket can be gone, or the server busy. The
    navigatr says so and stays usable -- the overlay has to keep working,
    because the user still has to be able to press Escape.
    """
    try:
        workspaces = _listing(client.call("workspace.list"), "workspaces")
        tabs = _listing(client.call("tab.list"), "tabs")
        panes = _listing(client.call("pane.list"), "panes")
        agents = _listing(client.call("agent.list"), "agents")
    except HerdrError as exc:
        return Navigatr([], error="cannot read the session: %s" % (exc.message or exc.code), kind=kind)
    except HerdrUnavailable as exc:
        return Navigatr([], error="herdr unreachable: %s" % exc, kind=kind)
    except Exception as exc:  # noqa: BLE001 - an overlay must not die
        return Navigatr([], error="cannot read the session: %s" % exc.__class__.__name__, kind=kind)
    if kind == AGENTS:
        rows = build_agents(workspaces, tabs, panes, agents)
        return Navigatr(rows, kind=AGENTS, summary=summary(rows, AGENTS))
    rows = build(workspaces, tabs, panes, agents)
    return Navigatr(rows, kind=PLACES, summary=summary(rows, PLACES))


def jump(client, row):
    """Go to the row's target. Returns (ok, message).

    A target that has gone away -- a pane closed while the navigatr was
    open -- is reported, not raised: the user has to be told that the place
    they picked is not there any more.
    """
    if not row.method:
        return False, "there is nothing to go to"
    try:
        client.call(row.method, dict(row.params))
    except HerdrError as exc:
        return False, "cannot go there: %s" % (exc.message or exc.code)
    except HerdrUnavailable as exc:
        return False, "herdr unreachable: %s" % exc
    except Exception as exc:  # noqa: BLE001 - an overlay must not die
        return False, "cannot go there: %s" % exc.__class__.__name__
    return True, row.label


# -- the rearranging ---------------------------------------------------------
#
# The two moves the session can do that are not a jump.
#
# **A tab stays in its space.** Herdr's `tab.move` takes an `insert_index`
# and nothing else: the index is counted within the tab's own space, and
# there is no call that moves a tab into another space. The `tab.moved`
# event says so as well -- it carries "the updated ordered `tabs` list for
# that workspace". So a tab can be reordered inside its space and nowhere
# else, and the plugin says that instead of pretending.
#
# **A pane goes anywhere.** `pane.move` takes a destination: an existing
# tab, a new tab in a space, or a new space. A pane that crosses a
# workspace keeps its process alive; Herdr gives it a new public id on the
# other side.

# The two ways a pane can be split into a tab it is dropped into. Herdr
# requires one of them: a pane that lands in a tab is a split of it.
SPLIT_DIRECTIONS = ("right", "down")


def tab_order(rows):
    """Where each tab sits among the tabs of its own space.

    The tree is built from `tab.list` in the order Herdr returned the tabs,
    so the tabs under a space are in that space's order -- which is the
    order `tab.move` moves a tab within.

    A tab whose space is gone is not in here: it has no space of its own,
    and there is nowhere to move it to.
    """
    order = {}
    space = ""
    counts = {}
    for row in rows:
        if row.depth == 0:
            # A top-level row is a space, or a tab whose space is gone.
            space = row.params.get("workspace_id", "") if row.kind == WORKSPACE else ""
            continue
        if row.kind != TAB:
            continue
        index = counts.get(space, 0)
        counts[space] = index + 1
        order[row.params.get("tab_id", "")] = (space, index)
    return order


def move_tab(client, carried, target, rows):
    """Move the carried tab to the target tab's place. Returns (ok, message).

    The two tabs have to be in the same space: that is the one thing
    `tab.move` can do.
    """
    order = tab_order(rows)
    carried_id = carried.params.get("tab_id", "")
    target_id = target.params.get("tab_id", "")
    if carried_id not in order or target_id not in order:
        return False, "that tab has no space of its own"
    carried_space, carried_index = order[carried_id]
    target_space, target_index = order[target_id]
    if carried_space != target_space:
        return False, "a tab stays in the space it is in"
    if carried_id == target_id:
        return False, "that is the same tab"

    # The index Herdr wants is the place BEFORE the tab at that index, and
    # it counts the moved tab as still sitting in the list. So the index
    # that puts the carried tab where the target tab is is the target's
    # own index, or one more when the carried tab is in front of it --
    # otherwise the moved tab would land one place short.
    #
    # Measured against a live server, not read off the docs: `tab.move`
    # with `insert_index = 3` on a four-tab space put the tab third, not
    # fourth, and `insert_index` equal to the number of tabs was the way
    # to put one last.
    insert_index = target_index if target_index <= carried_index else target_index + 1

    try:
        client.call(
            "tab.move",
            {"tab_id": carried_id, "insert_index": insert_index},
        )
    except HerdrError as exc:
        return False, "cannot move the tab: %s" % (exc.message or exc.code)
    except HerdrUnavailable as exc:
        return False, "herdr unreachable: %s" % exc
    except Exception as exc:  # noqa: BLE001 - an overlay must not die
        return False, "cannot move the tab: %s" % exc.__class__.__name__
    return True, carried.label


def move_pane(client, carried, target, split="right"):
    """Move the carried pane to the target. Returns (ok, message).

    A tab is the place a pane goes: the pane is split into it. A space is
    the place a pane starts again: it gets a tab of its own there.
    """
    pane_id = carried.params.get("pane_id", "")
    if not pane_id:
        return False, "there is nothing to move"
    if target.kind == TAB:
        destination = {
            "type": "tab",
            "tab_id": target.params.get("tab_id", ""),
            "split": split,
        }
    elif target.kind == WORKSPACE:
        destination = {
            "type": "new_tab",
            "workspace_id": target.params.get("workspace_id", ""),
            "label": carried.label,
        }
    else:
        return False, "drop it on a tab"

    try:
        result = client.call(
            "pane.move",
            {"pane_id": pane_id, "destination": destination, "focus": False},
        )
    except HerdrError as exc:
        return False, "cannot move the pane: %s" % (exc.message or exc.code)
    except HerdrUnavailable as exc:
        return False, "herdr unreachable: %s" % exc
    except Exception as exc:  # noqa: BLE001 - an overlay must not die
        return False, "cannot move the pane: %s" % exc.__class__.__name__

    # Herdr answers with what it did, and it says no for a move it did not
    # make. A pane that is already in the target tab, or a zoomed tab, is
    # refused -- and the user is told which.
    if isinstance(result, dict) and not result.get("changed", True):
        reason = str(result.get("reason") or "the server refused it")
        if reason == "same_tab":
            return False, "it is already in that tab"
        if reason == "zoomed_tab":
            return False, "that tab is zoomed"
        return False, "the pane was not moved: %s" % reason
    return True, carried.label
