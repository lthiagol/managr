# AGENTS.md — v2 (`managr`)

Working notes for this plugin. Read this before changing anything.

For the repository as a whole — the Herdr API walls, the constraints, the
lessons, and the roadmap — read the root
[`AGENTS.md`](../AGENTS.md). This file only covers what is specific to the
plugin.

`managr` is a **modal, layered keymap** for Herdr. One key opens a
popup; the popup has layers (navigation, panes, workspaces, tabs, session,
your own commands); each layer has its own bare-key vocabulary; actions run
over the socket API. There are two navigatrs as well: `j` for the places of
the session, `a` for the agents in it.

```
prefix+space  →  layers › panes
                  v   split right        C-a v
                  s   split down         C-a -
                  x   close pane         C-a x
                  z   zoom pane          C-a z
```

Its sibling, `v1/`, is gone: the ambient status-bar hint it held is covered
by this popup, which says more than a one-line hint ever could.

---

## The three ideas that make it work

**The overlay owns a private keyspace.** A popup is session-modal: it takes
every keystroke, so none reach Herdr's prefix handler. `p` and `v` inside the
overlay are the plugin's and cannot shadow a Herdr binding. This is the
decisive difference from which-key.nvim, which intercepts the *real* keymap
and therefore must never shadow anything.

**Actions are executed, not simulated.** `pane.send_keys` writes into a
pane's PTY and Herdr's key handling sits upstream of that, so a plugin cannot
fake a prefix press. This plugin calls the socket API directly.

**The tree is ours.** Herdr's keyspace is flat — `prefix` plus one chord. Ours
can be shaped the way the work actually reads.

---

## Long layers: the scroll and the filter

The "all bindings" view has more rows than a 70% popup has lines, and the
rows that fell off the bottom were simply gone. Two things fix that.

**The window scrolls.** The arrows move one row, `pgup`/`pgdn` and
`ctrl+d`/`ctrl+u` move half a page. `j` and `k` are *not* scroll keys:
they are actions in the panes and navigation layers, and taking them would
make those actions unreachable. The arrows were ignored before, so they are
free; ctrl+u is not in the decoder's control-key table, so it is recognised
in the overlay instead of changing the decoding. The same keys scroll
while the filter is open: a filter that matches thirty rows is still thirty
rows, and the user has to be able to reach the last one without accepting
the filter first.

**The rows can be filtered.** `/` opens the filter; the query narrows the
rows as it is typed, against the description, the key and the chord.
`enter` keeps the filter, `esc` clears it. The filter only hides rows: the
keys still run the same actions. A group heading in the reference view goes
when its group has nothing left under it, and it is shown once for its
group -- not once per row under it.

**Escape undoes the filter first.** A filter in force is the last thing
the user asked for, so `esc` clears it and stays in the layer; only a
second `esc` goes up a level, and a third closes the overlay at the root.
The footer says which of the two `esc` does right now: "esc close" with no
filter, "esc clear filter" with one.

The row budget is computed from the chrome -- the frame's two borders, the
hint, the filter line, the message -- not from a magic constant, so a row
can never be drawn over the frame's bottom border. It counts the row the
cursor ends up on as well: every line the overlay writes ends with a
newline, so the last newline leaves the cursor on the row *after* the last
line, and a frame that filled the screen exactly scrolled by one and took
the breadcrumb with it.

The bottom border says where we are (`12/44`) while the list is longer
than the window, and says nothing when everything fits -- and nothing while
a value is being typed, because then there is no list on the screen to be
in.

---

## The navigatrs: the session, as a tree and as a list

`j` at the root opens the **places navigatr**: the spaces, tabs and panes of
the running session, drawn as a tree with a cursor on it. `a` at the root
opens the **agents navigatr**: a flat list of every agent in the session,
the ones that want attention first. `esc` goes back to the root layer, and
the overlay stays open.

**The agent is folded into the pane that hosts it.** The tree is three
levels deep, not four: the pane's row carries the agent's name as its
description and the agent's status as its status. `agent.focus` and
`pane.focus` land on the same pane -- the agent's pane is the pane that
hosts it -- so folding the agent away loses nothing, and the tree is
shorter. The agents navigatr is where an agent that wants attention is
found.

**The panel is the window, and the navigatrs fill it.** The frame is drawn
to the popup's full height, so the keys are at the bottom of the *window*.
The space between the list and the keys is the **detail region**: the path
of the row under the cursor -- the space, the tab and the row's own name,
in full, because the row is cut to fit -- and the directory it is in. The
header carries the count, on the right: how many spaces and panes, or how
many agents and how many of them are blocked.

Herdr's own navigation surface has the same three things -- a count in the
header, the selection's path and directory at the bottom, and the keys at
the bottom of the window -- and the shape of ours is borrowed from it on
purpose. It is the same answer to the same question.

**Every row says what is inside it.** A space says how many tabs, panes and
agents it holds; a tab says how many panes and agents; a pane says which
agent is in it. The counts are computed from the four lists the plugin
already has and not from Herdr's own `tab_count` and `pane_count`: the
rows drawn and the counts must agree, and the session can change between
two of the four round trips. A count of zero is still said -- a row that
says nothing is a row the user cannot read.

**The two navigatrs can disagree about how many agents there are.** The
tree folds in the agent the *pane* reports when `agent.list` does not have
it -- the two lists are two round trips apart -- and counts it in the
space's "N agents". The agents list only lists what `agent.list` holds. So
a space can say "1 agent" while the agents list shows nothing for it.
That is the price of "nothing is dropped", and it is the right way round:
a place the user cannot go is worse than a count that is one too high.

**A pane belongs to the tab it names, wherever that tab lives.** A pane
whose space and tab disagree -- a pane that says it is in space A but names
a tab of space B -- is drawn under space B, with that tab, and NOT again
as a loose pane of space A. Drawing it twice put the same destination in
the tree twice and counted it in two spaces.

**The agents list is sorted by attention.** Blocked first, then working,
then done, then idle, then unknown -- the order of the status categories in
`navigatr.STATUS_CATEGORIES`, which is the attention order and not an
accident of the order they happen to be written in. Within one level the
order is the space's label, then the tab's label, then the agent's name, so
the list does not shuffle itself between two renders of the same session.

**The agents list marks the focused agent**, the same way the tree marks
the space, tab and pane that are current. The marker is not decoration: it
says which one you are on.

**The status is a glyph and a word, and the word goes first.** The glyph
alone needs the vocabulary to be read, and the word alone needs the eye to
read seven characters on every row. When the row is narrow the word is
dropped before the glyph -- it is eight columns and the glyph is one --
and the name of the agent is the last thing to lose anything: it is the
row.

**The overlay re-reads its context, and closes when it is left.** The
workspace, the tab and the pane the overlay was opened on are a snapshot,
read once. So the context is re-read after every action: an action that
names a pane, a tab or a space names the *current* one, not the one from
when the overlay opened. ("Split right" after "focus right" split the wrong
pane until this was fixed.)

And the overlay closes itself when the workspace or the tab changed. A
popup is a session singleton that takes every keystroke *wherever it was
opened*: an overlay left open on the old workspace holds the keyboard there,
and the plugin cannot be opened again on the new one. Moving the focus
between panes does not close it -- that is the whole point of the navigation
layer -- and neither does an action that fails.

**The root lists them.** The navigatrs are modes, not layers, but their
keys are in the root with the other entries, so the user can see that `j`
and `a` do something: a key that does something and is nowhere on the
screen is a key the user cannot find. The keys come from `navigatr_key`
and `agents_key` in `layers.toml`, like `reference_key`, so a user who
wants them somewhere else moves them there. A layer that binds that key
keeps it -- the layer's own binding wins, exactly like the filter's key --
and then the navigatr is not reachable by it and the root does not list an
entry that would promise it is.

**A tab is a heading, not a place.** The tree is spaces, tabs and panes,
and the tab is the only one of the three that is not a destination: a tab
always has at least one pane, and focusing a tab lands on a pane anyway.
So the cursor **skips** the tab rows -- a pane to a pane is one key, not
two -- and `enter` never goes to one; it says so instead. The tab row is
still drawn, with the counts and the status of the tab: it is the group
label of the panes under it, and "this tab has a blocked agent" is worth
reading at a glance.

The exception is a tab whose space is gone: it has nothing above it and
nothing under it, so it is the only row that names that place, and a
place the user cannot go to is a place that is dropped.

**The three tiers are told apart by their colour.** The space is the
section title and is drawn in the accent, the tab is the group heading
and is muted, and the pane is the item the user jumps to and is in the
plain colour. The markers and the indentation were not enough: the three
of them were a wall of labels in the same colour, and the eye had to
work out the structure of the tree row by row. The cursor's row is still
in the accent, but it is on a band -- a background -- and a background
is a much stronger signal than a colour.

**The number is in a column of its own.** It used to sit at the front of
the *label*, and it pushed the name of a space two columns to the right
of the name of the tab under it: the tree's names did not march with its
depth. The number has a slot of two columns now, in front of the tree's
markers, and the slot is there whether the row has a number in it or
not. So the numbers are a column, and the names march.

**A key that cannot be pressed is not listed either.** The overlay's
keyspace is single characters: `handle()` compares the character that was
typed with the key of an entry. So a `navigatr_key` of two characters is
an entry that promises a key no keypress could ever reach, and it is
refused with a word on stderr, exactly like a key a layer already took.
The same rule covers the reference key. Both warnings are printed once:
the tree is built once per popup, and the popup is opened over and over.

**The spaces are numbered, and a number moves the cursor to one.** `1` to
`9` aim at the Nth space. The rules, and why they are these:

- **The number does NOT jump.** It moves the cursor, so the user can see
  where they are pointing before committing, and `enter` does what enter
  has always done: it goes to the row under the cursor. With several
  spaces, aiming with a number and confirming with enter is faster than
  hunting with the arrow keys, and it cannot land on the wrong space.
- **Only the spaces.** A number on every row would be noise; the point is
  to move between spaces. In the agents list every row is an agent, so
  every row is numbered there.
- **The number has a column of its own**, in front of the tree's markers.
  It used to sit at the FRONT OF THE LABEL, where it pushed the name of a
  space two columns to the right of the name of the tab under it: the
  tree's names did not march with its depth. The column is there whether
  the row has a number in it or not, so the numbers are a column and the
  names march. The number is never given up: it is the space's identity
  and the footer promises the keys that name it.
- **The numbers follow the filter.** A number must always name a row the
  user can see, so the visible spaces renumber from 1 when a filter is in
  force. A number never points at a space that is hidden.
- **A digit typed while the filter is being typed is query text**, not a
  move. The filter has the keyboard first; the navigatr has it once the
  filter is accepted.
- **More than nine spaces: only the first nine are numbered.** There is
  no tenth key.
- **In the agents list the number has a slot of its own.** Every row
  there is an agent, so every row is numbered -- but only the first
  nine are, and a row with no number in it must not shift the rest of
  the row: the status is a column, and a column that moves two columns
  to the left after the ninth agent is not a column. So the number sits
  in a slot two columns wide, and the slot is there whether the row has
  a number in it or not. The number is also never given up there: it is
  the identity of the agent and the footer promises the keys that name
  it.
- **The footer mentions the numbers only when they fit.** The footer
  shares its room with the position badge, so it is told the room it
  will really get; a hint that ends in "..." tells the user less than
  one that was never there.


**It is a mode, not a layer.** A layer maps keys to actions; the
navigatr is a list of destinations. So it has its own keys (`enter` jumps,
`j`/`k` and the arrows move the cursor, `/` filters), its own footer, and
its own cursor. The footer says which keys move the cursor, because the
footer is the only place the user can read what the keys do. The two
navigatrs share all of it: the same keys, the same shape of footer, the
same filter.

**The rows are composed, not measured into columns.** The aligned table
renderer is for the layers, where the columns *should* line up. A tree is
the opposite: the indentation *is* the alignment, so every row has a
different width. The tree markers used to be a narrow column on the left
and the content a flat wall of labels on the right, so the tree converged
instead of opening outward and the eye had to join the two up. Now the
marker and the content it marks move together: the kind, the marker of the
current row and the name all start one column after that row's own branch
marker ends, so the content's column *varies with the depth*. That is the
invariant the tests hold.

**The status is a column, not a decoration.** The status glyph is drawn at
the right-hand end of the row, so every status in a list is in the same
place and the colours form a band the eye can scan. Herdr's own navigation
surface does the same, and it is why the status is in a column rather than
floating in the middle of a row: a column says "this is the status of
whatever is in this row" once, for the whole list.

It used to sit next to the name, which was right when the rows were a pile
of ragged text: out at the right edge it was forty columns from the thing it
described. A column is the answer to that -- not a status forty columns
away, but a status every row has in the same column.

**The selected row is on a band.** The row under the cursor is drawn on a
band of the accent, the full width of the panel, with the row's text in the
panel's background colour: light text on a light band cannot be read. That
is Herdr's own navigation surface too.

A band is a *background*, and the reset that closes a colour closes a
background as well: the parts of a band row are therefore not closed
between them, and the band is closed once, at the end of the row. A row
that closed its first part would end its band after two columns.

**The parts painted *inside* the row are the ones that break the band.**
The kind glyph of the tree and the marker of the current row are drawn
inside the row, and they closed their own colour: the band ended after
them, the rest of the row was drawn in the panel's background colour on
the terminal's own background, and the row under the cursor was
invisible -- a short stripe of band with the row gone. Every part of a
band row is painted without closing its colour, and that includes the
cut of a row that does not fit.

**The cursor is hidden, and put where the user is typing.** Every line
the overlay writes ends with a newline, so the terminal's cursor is
parked on the row *after* the frame -- outside the panel, where it
blinks as a block under the window and reads as a piece of the overlay
that was left behind. So the cursor is hidden while a list is drawn,
shown again in the input state and in the filter, and put at the end of
the text the user is typing.

**The dependent elements are dropped, not cut off.** The status and the
description are *dependent* elements: at the width the popup really has --
53 columns on an 80 column terminal -- a pane title of 32 characters leaves
no room for them, and a status cut off at the edge of the frame is a
status the user cannot read. So the row is measured first, and they go
before the label is truncated: the status first, then the description, and
the label is cut last. The number is not one of them -- it has a column of
its own, so it is never given up. The row then ends cleanly instead
of in a bare ellipsis. "A note that outlives its chord is worse than no
note."

**The two navigatrs have two row shapes.** A tree and a flat list are not
the same shape, so they have two composers: the tree keeps its markers and
its kind glyph, and the agents list has neither -- every row is an agent,
so saying so on every row is noise. What they share is the cursor marker,
the number, the anti-wrap rule and the truncation helper. The agents list
draws the status as a glyph *and* as a word: the glyph alone needs the
vocabulary to be read, and the word alone needs the eye to read seven
characters on every row.

**The session's data is not the user's own file.** The tree draws socket
data now -- terminal titles -- and a pane title can hold an escape
sequence: a program running in a pane could colour the overlay. The trust
boundary moved, so the colour runs are stripped out of the *label*, and
only out of the label: the overlay's own colours have to survive.

**But the label is not the only channel the session's data takes.** The
description of a row is the agent's name and the names of the space and the
tab; the path in the detail region is the names of the space, the tab and
the pane; the directory is the pane's. All of them are the session's own
data, and all of them reach the terminal: an escape sequence in a pane's
title moves the cursor, clears the screen, or closes the colour run of the
band under the row and leaves the row unreadable. So the escape sequences
are stripped out of all of them -- and only out of them, because the
overlay's own colours are applied after.

**The kind survives `icons = false`.** The Nerd Font glyphs are what say
a row is a space, a tab or a pane, and the tree's shape does not say it: a
tab and a pane are drawn with the same markers. So the kind has a word as
well (`space`, `tab`, `pane`, in `icons.toml`), and the word is what is
drawn when the glyphs are off. There are three kinds and not four: the
agent is folded into the pane that hosts it.

**It is inside our overlay, not in another plugin's popup.** A popup is a
singleton session resource in Herdr: only one can be open, and a second
one fails with `ui_busy`. Our overlay already holds the one popup, so
opening the herdr-navigator's popup from inside it cannot work. The
navigatr borrows that plugin's *vocabulary* -- the tree markers and the
agent status glyphs, which are already in `icons.toml` -- and draws the
tree in our own frame.

**The data is fetched when the navigatr is opened, not on every render.**
A render happens on every keystroke, and a socket round trip per keystroke
would make the overlay feel like it is lagging. Re-entering the navigatr
fetches the session again, so the tree is never staler than the last
visit. The four calls are `workspace.list`, `tab.list`, `pane.list` and
`agent.list`; the tree is spaces -> tabs -> panes, and the agent of a
pane is folded into the pane's row.

**The jump is a socket call on the row under the cursor.** The parameter
names were read off the live server, not guessed: `workspace.focus` wants
`workspace_id`, `tab.focus` wants `tab_id`, `pane.focus` wants `pane_id`,
and `agent.focus` wants `target` -- the agent's pane. A call with the
wrong name is rejected with `missing field`.

**The cursor is clamped every time the rows are asked for.** The filter
can shrink the list under it, and a cursor past the end points at
nothing. It is also moved off a heading: the cursor never stops on a
tab row, so a filter that leaves it there moves it to the row it can
stop on -- the one below it first, because a heading is followed by the
rows it groups.

**The filter keeps the structure.** A space or a tab row is a heading,
and it is shown when one of the rows under it survived the filter: a
tree without its headings is a list, and the list is what the agents
navigatr is for. The cursor goes to what *matched*, not to the top of
the list, so the place the user asked for is under the cursor when the
filter is accepted.

**Nothing is dropped.** The four list calls are four round trips, and the
session can change between two of them: a tab closed in between leaves
its pane loose, and a space closed in between leaves its tab with no
space. Both are shown -- the loose pane under its space, the tab at the
top of the tree -- because a row that is not shown is a place the user
cannot go. The children of a space are its tabs *and* its loose panes
together, so exactly one of them is the last child and one branch is
drawn as the end of it.

**Escape undoes the filter first, here too.** A filter in force is the
last thing the user asked for, so `esc` clears it and the navigatr
stays; only a second `esc` goes back to the root layer. The footer says
which of the two `esc` does right now.

**A list call can fail.** The socket can be gone, or the server busy. The
navigatr says so and stays usable: Escape still works, because the user
still has to be able to leave.

---

## The folds: a space with its rows hidden

`h` folds the space the cursor is in, `l` opens it, `H` folds every space
and `L` opens every space. A folded space is one row with a `▸` on it, and
the rows under it are hidden.

**The navigatr opens folded, except for the space the session is in.** The
tree of every space is a lot of rows to read past when all the user wants
is to go somewhere else, and the numbers already solve the jumping: `1` to
`9` aim at a space. What was missing was *reading* -- getting from one
space to the next without scanning past a dozen pane rows that say nothing
about the space. So the navigatr opens as a list of spaces, with the one the
user is in open, and a space is opened when it is wanted.

**The fold state lives on the overlay, not in the rows.** The rows are
rebuilt every time the navigatr is opened -- `fetch()` reads the session
again -- so a fold kept in a row would be forgotten the moment the user
closed the navigatr and opened it again. The key is the **workspace id**,
not the row's place, so a fold survives the tree being rebuilt. The state
outlives the navigatr but not the overlay: a fresh overlay starts folded
again, which is right -- a new session wants a new view.

**The fold is a lens, not a drop.** `build()` keeps the invariant that
nothing is dropped: a row that is not shown is a place the user cannot go
to. A fold does not break it -- the space's own row stays, the numbers
still reach it, and its panes are one `l` away -- but the argument is
worth making, because the next reader will reach for `build()` and put
the fold in the wrong place.

**A filter suspends the folds.** A filter is an explicit request to see
something, and a place the filter cannot reach is a place the user cannot
go to. So while a filter is in force every space is open, and the folds
come back when the filter is cleared. Folding while a filter is in force
is refused rather than silently swallowed: the key would change nothing
the user can see.

**The cursor follows the fold.** The cursor is a place in the list of
visible rows, so folding takes rows out from under it. The cursor is put
back on the row it was on, and when that row was under the space that was
folded, the cursor goes to the space -- which is still there.

**The footer names the keys the user cannot guess.** The footer is one
line and it does not have room for everything: the room is the popup's own
width, and the position badge takes some of it. So the hints are a
ladder, widest first, and the first one that fits is the one that is
shown -- a hint that gets cut in half says less than one that is
complete.

The keys that cannot be guessed come first in the ladder, because they are
the ones nobody finds any other way: `h` and `l` fold a space, `H` and
`L` fold them all, and `r` starts the rearranging. The numbers and the
movement keys have been in the footer since the beginning and the user
knows them. So at the width an eighty-column terminal gives -- a popup of
seventy percent of it, so a room of forty-nine columns -- the footer says
`esc back · h/l fold · r move · enter jump`, and the fold keys and the
rearranging are both in it.

**The agents list has its own footer.** It is a flat list of agents:
there are no spaces in it to fold and nothing in it to rearrange, so it
does not offer the keys that do those things. A footer that names a key
that does nothing is worse than no footer.

## The rearranging: a tab or a pane put down somewhere else

`r` starts the rearranging. `enter` picks the tab or the pane under the
cursor up, and `enter` again puts it down on the row the cursor is on.
`esc` leaves it.

**While the rearranging is on, the tab rows are targets.** A tab is a
heading, not a destination, so the cursor never stops on one -- the rule
that keeps "a pane to a pane is one key" true. But a tab is exactly what
the user wants to aim at when a pane is to be dropped into it, or when
another tab is to take its place. So the tab rows become selectable for
as long as the rearranging is on, and only for that long.

**A tab stays in its space.** Herdr's `tab.move` takes an `insert_index`
and nothing else: the index is counted inside the tab's own space. The
`tab.moved` event says so as well -- it carries "the updated ordered
`tabs` list for that workspace". There is no call that moves a tab into
another space, so the plugin refuses it and says why, rather than
pretending.

**The index is not the target's index.** `tab.move` puts the tab *before*
the tab at that index, and it counts the moved tab as still being in the
list. So the index that lands a tab where another one is, is the target's
own index -- or one more, when the moved tab is in front of the target.
This was measured against a live server, not read off the docs: a
four-tab space with `insert_index = 3` put the tab third, and
`insert_index` equal to the number of tabs was the way to put one last.

**A pane goes anywhere.** `pane.move` takes a destination: an existing
tab (with the split the user chose), a new tab in a space, or a new
space. A pane that crosses a workspace keeps its process alive -- Herdr
gives it a new public id on the other side. A pane dropped on a space
gets a tab of its own there; a pane dropped on a tab becomes a split of
it.

**Herdr refuses some of it, and the plugin says which.** A pane that is
already in the target tab, or a tab that is zoomed, comes back as
`changed: false` with a reason, and the reason is what the overlay
shows. A silent no-op is a real failure: the user pressed a key and got
nothing back.

**The tree is fetched again after a move.** It is a picture of the
session, and the session has just changed. The context is re-read too: a
pane that moved is not in the place it was, and the overlay closes if the
space or the tab it was opened in is gone -- the same rule every other
action follows.

## The custom layer: your commands, and the plugins' actions

`c` at the root opens the **custom layer**. Its rows are not in
`layers.toml`: they are the user's own `[[keys.command]]` entries and the
actions of every installed plugin.

**The rows are fetched when the layer is opened, not on every render.** A
render happens on every keystroke, and a socket round trip per keystroke
would make the overlay feel like it is lagging. `plugin.action.list` is the
call, and `plugin.list` says which of the plugins are enabled. Entering the
layer again fetches them again, so the list is never staler than the last
visit -- the same rule the navigatr follows.

**The entries have a key space of their own.** They are not keymap actions,
so they cannot have the tree's keys: each one gets a single character, and
never the filter's `/` or the reference view's `?`. The rest of the layer is
unchanged: the same scroll keys, the same filter, the same frame, the same
position indicator.

**The pool of keys is finite, and the layer says so.** It is every
printable character except the space, the apostrophe, `/` and `?` -- 93 of
them. An install with many plugins has more actions than that, and the
entries past the end are listed -- the filter finds them -- but no key
runs them. So the layer says how many of them there are, the way it says
what it left out. A row that looks runnable and is not is worse than a
row that is not there.

**A qualified plugin action id is split at the right dot.** Herdr's
qualified id is `<plugin id>.<action id>`, and the rules for the two
halves are **not symmetric**: a plugin id may hold dots
(`normalize_identifier` allows them) and an action id may not
(`normalize_local_identifier` does not -- the server answers
`invalid_plugin_action_id` for `layout.apply`). So
`example.worktree-bootstrap.bootstrap` is the plugin
`example.worktree-bootstrap` and the action `bootstrap`, and splitting it
at the *first* dot asks for an action id that cannot exist. The whole id
is matched against the qualified ids of the installed plugins' actions
first -- that is what Herdr's own keybind path does -- and the last dot is
the fallback, because it is the only one that can be the separator.

The plugin-action rows from `plugin.action.list` are not affected: the
server returns the bare action id and a separate plugin id, which is the
form the invoke wants.

**A popup is a singleton in Herdr.** The overlay *is* a popup, so a command
of type "popup" cannot be run from inside it -- the second popup is refused
with `ui_busy`. A command of type "pane" cannot be run from here either:
there is no socket method that opens a pane and runs a command in it. Both
say so, and both name the key to press in Herdr instead. They are not
attempted, and they do not fail silently.

**A plugin action is started, not awaited.** `plugin.action.invoke`
launches the action's own process and returns as soon as it is launched:
the log record it hands back says `status: "running"`, and the outcome
only turns up later in `plugin.log.list`. So whatever happens *inside*
that process is not in the invoke's answer -- a plugin action that wants a
popup of its own is refused with `ui_busy` in the action's process, while
the invoke itself succeeds. The overlay therefore says the action was
**started**, not that it ran: it cannot know the outcome, and a message
that claims the action ran is a lie the user acts on. There is no special
case for `ui_busy` here, because the invoke cannot answer it.

**A `type = "shell"` command runs detached**, the way Herdr runs it: in the
background, through `/bin/sh -lc`, from the focused pane's directory, with
Herdr's context in the environment. The overlay does not wait for it.

The context is the six variables Herdr's documentation promises a custom
command: `HERDR_SOCKET_PATH`, `HERDR_BIN_PATH`, `HERDR_ACTIVE_WORKSPACE_ID`,
`HERDR_ACTIVE_TAB_ID`, `HERDR_ACTIVE_PANE_ID` and `HERDR_ACTIVE_PANE_CWD`.
They are set by the overlay rather than inherited, because the overlay is
the server's child and not the user's shell: the binary path in particular
would otherwise be there by accident.

**Nothing is dropped in silence.** An action of a plugin that is disabled, or
one that is not for this platform, is left out -- and the reason is on the
screen. So is the fact that there is nothing to run at all, and the fact
that some of the entries have no key.

---

## What can and cannot be executed

Most of Herdr's actions are reachable. These are not, because they are
Herdr-owned UI surfaces with no socket method, and the overlay shows them as
**hint rows** — the chord to press, and a note that it must be pressed in
Herdr:

`help` · `settings` · `goto` (session navigator) · `toggle_sidebar` ·
`detach` · `copy_mode` · `resize_mode` · `open_notification_target`

`make explain` prints the live mapping. The unreachable set is in
`actions.UNREACHABLE`; when Herdr grows a method, remove the entry and add an
executor in the same change.

**Note the API moved under us.** The prior-art plugin
(`cowboyvang/herdr-which-key`) targeted Herdr 0.7.5 and found only 34 of 52
actions executable. On 0.9.5 there are `pane.copy_motion`, `pane.copy_search`,
`pane.selection.read`, `pane.edit_scrollback` and `pane.clear` — copy mode
*is* drivable now. Do not trust any action list that predates 0.9.

---

## The action map is data, and it is checked

Every action is described by a `Call` — a socket method and the parameters it
would send — and the executor is a thin wrapper around that description. The
point is that a tool can ask *"what would this action do?"* without doing it.

That matters because the calls were written by reading the documentation, and
**Herdr does not forbid extra fields in a request**: a call with the wrong
parameter name is *accepted and ignored*. `pane.close` with `pane` instead of
`pane_id` is not refused — it closes nothing, and the overlay says "pane
closed" all the same.

Two flags on every action:

- **destructive** — it creates or closes something (split, close, create,
  clear). The probe never runs one.
- **safe** — it only moves the focus or reads the state. The probe runs
  these against the live server. Anything that is neither is left alone:
  *if you are unsure whether an action is safe, treat it as destructive.*

`scripts/api_check.py` checks the whole map against the running server's
schema and reports every action: which method it uses, whether it was
executed or only schema-checked, and the result. It also searches the schema
for a method for each hint row, so *"there is no socket method for this"* is
a measurement and not a claim. The probe **does not change the user's
session**: it closes nothing, creates nothing, splits nothing and starts no
agent, and the focus moves it makes are put back.

`src/managr/data/api-schema.json` is a snapshot of `herdr api schema
--json` from 0.9.5. `tests/test_api_check.py` runs the schema half of the
check against it, so the check runs with no server and no network.

The hint rows are claims about Herdr's API, and each one says what to search
for: `copy_mode` searches for "copy" and finds `pane.copy_motion`,
`pane.copy_search` and `pane.selection.read`. They are the copy
*primitives* — they move a cursor and read a selection, and none of them
enters the mode — so the row stays, and the reasons are recorded next to it.


---

## Layout

```
herdr-plugin.toml        2 actions, 1 popup pane (titled "Managr")
bin/managr               open | overlay | tree | explain
src/managr/
  client.py              the socket client
  actions.py             action -> socket call, and what is not reachable
  apicheck.py            the action map, checked against Herdr's schema
  tree.py                the layer tree, merged from bundled + user files
  navigatr.py           the two navigatrs: the tree of places, the list of agents
  custom.py              the custom layer: your commands, the plugins' actions
  table.py               the layout engine: measured columns, visible width
  overlay.py             the modal TUI
  visuals.py             the accent, the palette, the glyphs
  merge.py               the one merge rule for every user-editable file
  keymap.py, tomlite.py  the keymap engine, and a tolerant TOML reader
  data/layers.toml       the built-in tree
  data/labels.toml       action -> human label
  data/visuals.toml      the look: accent, icons, frame, theme
  data/icons.toml        the Nerd Font glyphs
  data/api-schema.json   herdr 0.9.5's schema, recorded
scripts/
  configure.py           manages the keybinding
  live_check.py          drives the real overlay against the running Herdr
  live_workspace_check.py the overlay closes itself when the workspace moves
  verify_claims.py       breaks the code on purpose, to prove the guards
  api_check.py           checks the action map against the running Herdr
tests/
  test_layers.py         the tree, the action map, the overlay's state machine
  test_table.py          the layout engine
  test_visuals.py        the accent, the palette, the glyphs
  test_configure.py      the config mutator
  test_api_check.py      the action map, against the recorded schema
  test_entrypoint.py     the entry point, as Herdr runs it
  test_overlay_pty.py    a real-pty run of the overlay
  test_shared.py         the config dir and the merge rule
```

The suites are **discovered**, not listed: `make test` globs
`tests/test_*.py`, so a new file is picked up by the next run.

`keymap.py`, `tomlite.py` and `merge.py` used to be copies of v1's. They are
not copies any more: v1 is gone, and these three are the only owners of their
behaviour. They were copies because a Herdr plugin is a self-contained
directory with its manifest at its root, and `plugin link` points at that
directory -- sharing would have meant one plugin depending on another, which
the manifest cannot express.

---

## Commands

```sh
make help          # every target
make install       # link + binding + reload
make uninstall     # restores config.toml exactly
make tree          # print the layer tree
make explain       # executable actions vs hint rows
make open          # open the overlay right now
make live-check    # drive the real overlay against the running Herdr
make live-workspace-check  # the overlay closes itself when the workspace moves
make api-check     # check the action map against the running Herdr
make verify-claims # prove the regression guards are real, by breaking the code
make test          # 553 tests, no Herdr server needed
make check         # lint + test
```

**`make verify-claims` is a background job.** It is 146 guards, and each one
copies the whole tree into a scratch directory, breaks the code there and
runs a suite — so it takes **fifteen minutes**. Run it in the background:

```sh
make verify-claims > /tmp/guards.log 2>&1 &
```

The output is buffered until the end, so an empty log means it is still
running, not that it has failed. The line that matters is the last one:
`146 passed, 0 failed`. `make check` is the quick one — about a minute, the
pty suite is most of it — and it is the one to run after every change.

The guards' revert strings are pinned to the source they break. A change to
`_navigatr_line`, `_agent_line` or `_fit_row` moves them, and a guard whose
revert no longer matches reports "could not find the code to revert" — which
is the guard telling you it needs re-pointing, not a broken guard.

---

## Rules for changing this plugin

**A binding names an action, never a path.** A `type = "popup"` binding is
launched by the Herdr server, not the plugin runtime, so it gets no
`HERDR_PLUGIN_*` environment and cannot find its own root -- and a
GitHub-installed plugin lives under a content-hashed path that moves on
every update. So the binding is a `type = "plugin_action"` entry that
invokes the plugin's own `open` action: the plugin runtime runs it with the
plugin root as the working directory and the full environment, and it
opens the plugin's own pane. Never point a binding at a path inside the
plugin directory.

**The popup's title lives in the manifest.** `[[keys.command]]` has no
`title` field, so a popup opened by a config binding is called "popup" and
there is nothing the plugin can do about it. The plugin's own pane has one,
and that is the title Herdr puts on the frame.

**The tree file is merged, not replaced.** A `layers.toml` in the plugin
config dir merges over the bundled one: a layer you define is added, a layer
you redefine takes your title/hint/key, its items replace the built-in ones
if you give it any, and layers you do not mention are inherited.

**One merge rule for every user-editable file.** `layers.toml`,
`visuals.toml` and `icons.toml` are all merged by `merge.py`: the user's
value wins for a setting the plugin knows, a named entry the user names has
its fields merged over the bundled entry's, a list the user names replaces the
bundled list, and a name the plugin does not know is dropped with a warning
on stderr that names it and the file it came from. A *name* is free -- a layer
the user writes gets a glyph in `icons.toml` -- but a *setting* is not:
`icon = true` instead of `icons = true` is a setting the plugin does not
know, and it is reported.

**Items are a flat `[[item]]` table with a `layer =` reference.** TOML's
`[[layer.item]]` nested inside `[[layer]]` is ambiguous once there is more
than one layer.

**`key` in the tree is the overlay's own keyspace, not a Herdr binding.** The
Herdr chord shown beside it is looked up from the live keymap for reference.
`keep = true` leaves the overlay open after the action; the default is to
close, because that is right for one-shots and wrong for repeatable verbs.

**An action with no executor is a hint row, not a bug.** Do not add a fake
executor for something Herdr owns.

**Never trust a zero-sized terminal.** `os.get_terminal_size` reports zero
rows on a pty with no window size, and a negative row budget renders an
empty overlay. The fallback in `overlay._size` is load-bearing.

---

## Lessons (bugs that actually happened)

**A tab that says it moved and did not.** `move_tab_next` reported "moved"
and the tab stayed exactly where it was. Herdr's `tab.move` puts the tab
*before* the tab at that index, and it counts the moved tab as still being
in the list -- so the index that lands a tab one place along the bar is not
the index of the tab it should end up at, it is one more. The action had
the right index for "previous" and the wrong one for "next", so half of it
worked and half of it was a no-op that said it had not.

What caught it was not a test of the message. The test that had been there
checked that the action *said* "moved", which it did, lying. The test that
found it models the server: a tab bar that behaves the way Herdr's does,
so the tab's *place* is checked and not what the action said about it. A
guard that checks what the code says is not a guard on what the code does.

**The overlay's context is a snapshot, and it has to be re-read.** The
workspace, the tab and the pane the overlay was opened on are read once.
An action that moves the focus leaves them stale, so "split right" after
"focus right" split the pane that was focused when the overlay opened --
the action named it from the snapshot. The context is re-read after every
action now.

**A popup is a singleton, and it holds the keyboard where it was opened.**
Switching the workspace from the overlay and leaving it open stranded the
popup on the old workspace: the user was in the new one and the keyboard was
not, and the plugin could not be opened again until the overlay was closed
by hand. The overlay closes itself now when the workspace or the tab
changes -- and not when only the pane changed, because moving the focus
between panes is what the navigation layer is for.

**A config binding has nowhere to put a title.** `[[keys.command]]` has no
`title` field, so a popup opened by one is called "popup". The title is the
one thing a plugin cannot set from the outside, and the only way to get a
name on the window is to open the plugin's own pane, which has one.

**A row of its own is not a row of its own.** The agent used to be a row
under the pane that hosts it, and the tree said the same thing twice: the
pane's row and the agent's row were the same place, and the tree was four
levels deep because of it. `agent.focus` and `pane.focus` land on the same
pane -- the agent's pane is the pane that hosts it -- so the agent is
folded into the pane's row: its name is the row's description and its
status is the row's status. The agents list is where an agent that wants
attention is found.

**The counts come from the lists, not from the server's own counts.**
Herdr reports `tab_count` and `pane_count` for a space, but they are a
snapshot of a session that can change between two of the four list calls,
and the rows drawn and the counts have to agree. So the counts are
computed from the rows, and a count of zero is still said: a row that
says nothing is a row the user cannot read.

**A setting of the wrong type is ignored with a word, not used.**
`agents_key = 5` in the user's `layers.toml` killed the popup with a
traceback: the key was measured with `len()` and compared with a set, and
neither takes an integer. The setting is hand-written, so it is the most
likely one in the file to be wrong, and a wrong setting must not take the
popup down. The merge now says what it expected and what it got, and the
bundled value stands -- which is what "a setting that is ignored must say
so" means.

**A zero-sized terminal renders nothing.** The overlay computed its row
budget from the terminal size; a pty with no window size reports zero rows,
the budget went negative, and the popup came up *blank*. The pty test caught
it. The fallback to 80x24 is load-bearing, not defensive padding.

**Raw mode turns off ONLCR, so "\n" does not go to column 0.** `termios`'s
`cfmakeraw` clears `OPOST`, which disables the terminal's newline-to-CRLF
translation. Every line then starts where the previous one ended and the
overlay renders as a diagonal staircase. The fix is to write `"\r\n"`
explicitly (`overlay.NL`), *not* to switch to `tty.setcbreak`: cbreak would
work only while Herdr's popup pty happens to leave OPOST on. The pty test
asserts there is no bare newline in the output.

**A socket connection can go stale while the overlay sits idle.** The server
drops it, and the next write fails with EPIPE. That is a stale connection,
not a dead server, so `HerdrClient.call` reconnects and retries once. Treat
EPIPE as "reconnect", never as "herdr is down".

**Herdr says "nothing happened" with `changed: false` and a `reason`.** The
socket methods do not raise for "there is no pane that way" — they return a
successful response with `changed: false, reason: "no_neighbor"`. Ignoring
that made a no-op look exactly like a successful move, and the user was left
wondering whether the key had registered. `actions._describe` turns it into a
message and keeps the overlay open, because the useful next move is another
key, not closing the menu. Some methods nest the outcome under `focus` or
`swap`; `_result_of` finds it.

**A popup has no pane id, so the API cannot test it.** `pane.send_input`
cannot reach a popup. That is why `test_overlay_pty.py` exists: it is the
only way to drive the overlay end to end. `scripts/live_check.py` goes
further and drives it against the *running* Herdr over the real socket, which
is how the `changed: false` gap was found. Use it after touching the
executors.

**A read from the terminal can hold several keys, or half of one.** `os.read`
returns whatever is available, so `b"pv"` arrives as one chunk. Decoding the
whole read as one key loses all of it: the bytes become a single unknown key
and both keystrokes vanish, with no message. `overlay.KeyReader` keeps a
pending buffer, decodes one key at a time, and uses
`codecs.getincrementaldecoder("utf-8")` so a character split across two reads
survives. A lone ESC is ambiguous — it is either Escape or the start of
`\x1b[A` — so the reader waits briefly for more bytes before deciding.

**`keep = true` in the tree is what keeps a repeatable verb open.** The
overlay closes when *neither* `outcome.keep_open` nor `item.keep` says
otherwise. When the executors were written to return `keep_open=True` and the
tree's `keep` flag was ignored, the nav layer closed on the first focus move
and could not move twice.

**A restore that only writes escape codes is not a restore.** The pty test
asserted that the output contained `\x1b[0m`, which the `finally` block
writes whether or not `tcsetattr` ran. Deleting the restore left every test
green. The test now keeps the *slave* fd open and asserts the child's termios
was really restored — and on macOS the master's termios does not reliably
reflect it.

**A signal handler must not block, and it must not write either.**
`tcsetattr(TCSADRAIN)` waits for pending output to drain; inside a signal
handler, with a reader that is not keeping up, that wait can block forever
and the process hangs instead of dying. The handler uses `TCSANOW`. It also
writes nothing: a `write` plus a `flush` blocks on a full pty buffer exactly
the same way, and a blocked handler means the process ignores SIGTERM and
needs SIGKILL. The screen is wiped on the normal exit path instead, where
the popup is on its way out anyway. The pty test reproduces it: fill the
pty's output queue, never read from it, then SIGTERM.

**A control key interrupts a half-decoded character.** The incremental
decoder holds the bytes of a character that has not arrived in full. A
control key in the middle of one has to reset it, or the *next* keystroke
decodes to garbage and is dropped without a word: the user pressed a key and
nothing happened. The same goes for an ESC found part-way through the
buffer -- the half character is garbage, but the Escape is a key.

**A CSI sequence ends on a final byte, not on any byte.** ECMA-48 lets any
byte in 0x40-0x7E end a CSI sequence. Accepting any of them means ESC "["
"x" is read as one unknown sequence, and the "x" is swallowed. The final
byte has to be one a terminal actually sends (`overlay.CSI_FINALS`); when it
is not, the "[" was a literal "[" the user typed, so the Escape is given
back and the rest are ordinary keys.

**A check that greps the source for a string is not a check.** The audit
script did that, and it printed PASS while the bug it was checking for was
back in the tree. Every check now exercises the code and observes what
happens, and `make verify-claims` breaks the code on purpose to confirm the
guard fires.

**The audit script is gone; `verify_claims.py` does the job.** It audited
*past review rounds* — "the first review reported nine issues and the
second seven more" — which is a list of findings, not a property of the
code. `verify_claims.py` checks the same guards the better way: it reverts
each fix in a scratch copy of the tree and the copied suite has to fail. A
guard that stays green when the bug is back proves nothing, and a script
that checks a list of past findings cannot notice a new bug at all.

**Items that name a layer nobody declared are unreachable.** The root lists
a layer by its key, so items for a layer that does not exist load into a
layer nothing can reach. The tree says so on stderr: silence looks like the
plugin lost the work.

**A copied test tree still imports the original source.** The test computes
`ROOT` from its own `__file__`, so running a copy from a scratch directory
still exercises the *original* `src/`. When a verification script copies the
tree to break something, it must run the copy from the copy.

**A stale `.pyc` can hide a revert.** A copied tree keeps its compiled
caches, and Python reuses a `.pyc` whose recorded timestamp and size still
match the file beside it. A revert that swaps two strings of the same length,
applied and restored inside the same second, is therefore never compiled --
and the suite passes for the wrong reason. The scratch copy leaves the
`__pycache__` directories out.

**Blocks before the first table header have no header.** The config mutator
crashed on `block["header"].strip()` for the preamble above the first
`[table]`. Flatten through a helper that skips `None`.

**The CLI and the socket disagree on action ids.** `herdr plugin action
invoke` wants the *bare* action id with `--plugin`; the socket accepts the
qualified `plugin.action` form. The config binding uses the qualified form.
Do not assume one convention.

**A plugin can open its own popup.** `plugin.pane.open` with your own
`plugin_id` works, which is how the `open` action works. But the second
attempt while the first popup is up fails with `ui_busy` — popups are a
singleton session resource.

**A stripped line that is not written is not removed.** The first version of
the legacy-help cleanup stripped the line from the in-memory blocks and then
took the "binding already correct" early return, so the file was never
written and the message lied. An early return has to account for every
pending change, not just the one it was checking.

**A line as wide as the terminal wraps the cursor.** A line of exactly `cols`
columns leaves the cursor in the last column, and the next character wraps
it to column 0 of a new row: every line after it lands a column to the left
and the layout falls apart. Every line the overlay writes is therefore
capped at `cols - 1` visible columns, and `table.visible_width` is what
counts them -- ANSI escape sequences take no space, and an East Asian wide
character takes two. The width a test checks is measured in the test, not
with the module's own function: a test that asks the code under test how
wide its output is proves nothing.

**The columns are measured, not typed in.** The overlay's rows used to put
two spaces between the description and the chord, so the chord started in a
different column on every row. `table.render` measures the columns from the
rows it is given, and drops the optional ones -- the note first, then the
chord -- before it truncates anything.

**A note that outlives its chord is worse than no note.** The note says
"press this in herdr", so it means nothing without the chord to press: it is
a *dependent* column, and a dependent column is the first to go. Between 51
and 57 columns the session layer used to render "Herdr's own keybind panel
press this in herdr" with no chord at all -- an instruction with no key --
and that window includes the default, because the popup asks for 70% of the
terminal and an 80 column terminal gives a 53 column pty.

**Two halves of one line share one budget.** The input prompt and the input
buffer were each truncated to the full width, so the line came out up to
2*width+1 columns wide: the cursor wrapped and the next line landed in the
middle of a row. The prompt is a fixed label and the buffer is what the user
is typing, so the prompt gets at most half the line and the buffer gets the
rest.

**A guard whose sample cannot fail is not a guard.** The chord-alignment
test compared the columns of every line holding a chord, but the test
keymap bound one action, so the sample held one chord and the assertion
held for any layout at all: the ragged layout it was written for passed it.
A guard needs a sample big enough to mean something, and it needs to be
checked by breaking the code.

**A stray escape in user data swallows the rest of the line.** The cells
hold text from `layers.toml`, which a user can hand-edit. An escape that
starts no sequence makes the terminal wait for a final byte, and a vertical
tab or a form feed moves the cursor. `table.one_line` removes both.

**A check that looks for text has to look at the text the user sees.** The
overlay is coloured now, and the breadcrumb's last crumb is highlighted, so
the escape sequences sit in the middle of the words. `live_check.py` and
`verify_claims.py` strip them before searching, or a check for "layers ›
navigation" fails on a rendering that is perfectly correct.

**A refused candidate does not end the chain.** The accent is a list of
candidates, and an unreadable one was refused with a `break`, so a perfectly
readable candidate *after* it was never tried: a user with an unreadable
`theme.custom.accent` and dracula got the built-in cyan instead of dracula's
pink. A refusal skips the candidate and says so.

**A setting that is ignored must say so.** `icon = true` is not `icons =
true`, and the misspelled key was merged into the settings and then never
read: the user set it, saw no change, and had no word about why. An unknown
key is dropped with a warning on stderr that names the key and the file it
came from.

**A default that is written out twice is written out wrong eventually.** The
bundled `visuals.toml` duplicated `DEFAULT_SETTINGS` exactly, so removing it
from the merge loop left every test green. The defaults are read from the
file now, and a test says every key in it survives into the merged settings.

**A frame that fills the screen scrolls by one row.** Every line the
overlay writes ends with a newline, so the last newline leaves the cursor
on the row *after* the last line: a frame that used every row of the
screen scrolled by one, and the top border -- the breadcrumb -- went off
the top of the screen. The row budget counts the cursor's row, and the
test models the scroll instead of counting the bytes: in the output the
frame is all there, and it is the terminal that scrolls. A test that only
looked at the bytes would have called the frame perfect.

**A badge that outlives its state lies.** The position in the bottom border
said `6/41` while a value was being typed and no list was on the screen:
the counters were left over from the layer the overlay came from. The
input state clears them, and the badge of a list that is not shown is
worse than no badge at all.

**A popup is a singleton, so a second one cannot be opened.** Herdr's
popups are a session resource: only one can be open, and the second
attempt fails with `ui_busy`. That is why the navigatr is drawn inside
our own overlay rather than by opening the herdr-navigator's popup -- ours
is already up, and it takes every keystroke.

**A dataclass's fields are in order, and a positional argument is a
field.** `Navigatr([], "herdr unreachable")` set the *cursor* to the
message and left the error empty, so a failed list call looked like an
empty session. The error is a keyword argument now, and the test says
the failure is reported.

**A colour that is opened has to be closed.** The cursor in the
navigatr's tree was drawn in the accent and bold, and the cell did not
close the run: the tree markers, the glyph and the name of the row
under the cursor were all drawn in the accent too, while every other row
was plain. Every cell that opens a colour closes it -- the layer's path
already did; the tree's did not.

**The last child is decided over all the children.** The children of a
space are its tabs *and* the loose panes, and the flag that says which
one ends the branch was decided over the tabs alone: the last tab and the
last loose pane both claimed to be the end, and the tree drew two
"last" branches under one space.

**A marker is not decoration.** `icons = false` turned the Nerd Font
glyphs off, and the arrow that marks the current space, tab and pane
went with them, so the row you are on was not marked at all. The glyphs
are decoration; the marker says which row you are on.

**The kind of a row is not decoration either.** The glyphs are the only
thing that says a row is a space, a tab, a pane or an agent, and the
tree's own shape does not say it. With the glyphs off the rows were
indistinguishable, so the kind has a word as well.

**A tree's alignment is its indentation.** The navigatr's rows were
drawn with the aligned table renderer, like the layers: the tree markers
in a column of their own and the content in a flat wall of labels beside
them. The tree converged instead of opening outward, and the eye had to
join the marker up with the content. A tree's rows are composed one by
one: the marker and the content it marks move together, and the content's
column varies with the depth.

**A status that is not next to its label is not next to its row.** The
agent's status was a column of its own, right-aligned to the frame: forty
columns away from the name of the pane it belongs to.

**Escape has to mean one thing at a time.** In the navigatr `esc` left
the navigatr even with a filter in force, while everywhere else it
cleared the filter first and stayed.

**"Nothing is dropped" has to be true.** The navigatr's docstring
claimed it, and a tab whose space was closed between the two list calls
was dropped: a place the user cannot go to.

**A parameter the schema does not define is accepted and ignored.** Herdr's
request objects do not forbid extra fields, so a call with the wrong
parameter name is not refused — it does nothing, and the action says it
worked. The socket calls were written by reading the documentation and
nothing ever checked them against a running server; that is why the action
map is data now, and why `scripts/api_check.py` checks every method and
every parameter against the schema, and runs the safe ones for real.

**A plugin id may hold dots and an action id may not.** Herdr's id rules
are asymmetric, and the qualified id of the plugin
`example.worktree-bootstrap` and the action `bootstrap` is
`example.worktree-bootstrap.bootstrap`. Splitting that at the first dot
gives the plugin `example` and the action `worktree-bootstrap.bootstrap` --
and the server answers `invalid_plugin_action_id`, because an action id
cannot hold a dot. The live server confirms both halves: a plugin id with
a dot is accepted, and an action id with one is refused.

**An invoke starts the action; it does not run it.**
`plugin.action.invoke` launches the action's own process and returns as
soon as it is launched: the log record it hands back says `status:
"running"`, and the outcome only turns up later in `plugin.log.list`. So
the refusal of a plugin action that wants a popup of its own -- `ui_busy`
from `plugin.pane.open`, *inside* the action's process -- is not in the
invoke's answer, and the invoke succeeds. An overlay that says "ran" is
claiming something it cannot know.

**The scroll's budget and the render's budget have to be the same
budget.** The custom layer's own lines -- the "left out" line, the error
line -- are rows on the screen too. The render counted them and the scroll
did not, so the scroll's clamp was k rows too small and the last k rows of
the layer could never be shown, while the badge said they were there.

**A guard on the sample is not a guard on the pool.** The test that the
entry keys do not take the navigation keys looked at the keys that
happened to be *assigned*: with one command the only key was "a", so a
pool holding "/" passed it -- and entry 63 would own the filter's key, and
the overlay looks the item up before the filter. The guard has to be on
the pool itself.

**A key that cannot be pressed is a promise the plugin cannot keep.** The
root listed the navigatr with whatever `navigatr_key` said, and the
overlay's keyspace is single characters: a key of two characters was an
entry nobody could ever reach, and pressing "j" answered "no binding for
'j'". The rule a shadowed key already followed -- say so, and do not list
it -- now covers a key that is malformed. The same goes for the reference
key.

**A dependent element is dropped, not cut off.** The status was appended
to the composed row, so the truncation cut it first: at the width the
popup really has, an agent name of 33 characters left a row that ended in
"..." and no status at all. The row is measured first, and the status
goes before the label is cut.

**Session data is not the user's own file.** The navigatr draws terminal
titles, and a title can hold an escape sequence: a program running in a
pane could colour the overlay. The colour runs are stripped out of the
*label*, and only out of the label, because the overlay's own colours are
the overlay's.

**But the label is not the only channel the outside text takes.** The
description of a row is the agent's name and the names of the space and
the tab; the path in the detail region is the names of the space, the
tab and the pane; the directory is the pane's; the error the server sent
and the message it answered with are the server's. All of them reach the
terminal, and an escape sequence in any of them can move the cursor,
clear the screen, or close the colour run of the band under the row and
leave the row unreadable. So they are all stripped -- and only they are,
because the overlay's own colours are applied after.

**A character is not a column.** The unit guard on the width of a row
counted characters with `len()`: a CJK title is one character and two
columns, so the guard under-counted the row it was written to protect.
The width is counted in columns, the way the pty test counts them.

**A tree's levels have to be told apart by something the eye can see.**
The markers and the indentation were not enough: the space, the tab and
the pane were three rows in the same colour with the same shape, and the
eye had to work out the structure of the tree row by row. So the tiers
are told apart by their colour -- the space in the accent, the tab
muted, the pane plain -- and the cursor's row stays in the accent on a
band, because a background is a much stronger signal than a colour.

**A number in front of the label pushes the label out of step.** The
number of a space was drawn at the front of the *label*, so it pushed
the name of a space two columns to the right of the name of the tab
under it, and the tree's names stopped marching with its depth. The
number has a column of its own now, in front of the tree's markers, and
the column is there whether the row has a number in it or not.

---

## What we decided, and why

**Popup, not bottom split.** A popup is session-modal and simple. A bottom
split keeps the layout visible while you pick, which is more Zellij-like, but
opening and closing it reflows every pane in the tab twice. The split is the
obvious next experiment, and `placement` in the manifest is where it would
go.

**The binding invokes the plugin's own `open` action**, rather than opening
the popup directly as a `type = "popup"` command. That was the other way
round once: one process instead of two, and a failure appeared in the popup
rather than disappearing into the plugin log. But a config binding has no
`title` field -- Herdr calls that floating window "popup" and there is
nothing the plugin can do about it -- and a popup binding gets no
`HERDR_PLUGIN_*` environment, so it cannot name a path inside the plugin
directory and needs a generated launcher to stand in for one.

Going through the plugin fixes both: the popup is the plugin's own pane, so
it is titled "Managr", and the plugin runtime runs the pane's command
with the plugin root as the working directory, so there is no launcher to
keep. The cost is the one that was traded away: a failure to open the popup
lands in the plugin log rather than on the screen. It is a rare failure --
it takes another modal to be open -- and the title is worth it. The `open`
action is also the route for anyone who would rather not use a custom
command binding at all.

**Beside Herdr's native help, not instead of it.** The native panel answers
"what is bound"; this answers "what do I want to do". The trigger is
`prefix+space`, so the native panel stays on `prefix+?` and this plugin never
touches the native keymap.

**The trigger is `prefix+space`, and that is a layout decision, not an
arbitrary one.** Herdr matches a binding against the *character*, not the
physical key. On a "/?" layout, `prefix+?` means shift+slash, and on a
kitty-protocol terminal that same keystroke also matches `prefix+shift+?`
(Herdr's `key_parts_match_combo` accepts the shifted codepoint against either
expected modifier set) — so the two bindings are ambiguous. An earlier
version of this plugin used `prefix+?` and moved the native panel to
`prefix+shift+?`, which created exactly that collision. `prefix+space` needs
no shift, is the same on every layout, and leaves the native help alone.

**Colour works here.** A popup is a real terminal, so ANSI works. (It would
not in Herdr's status area: Herdr strips ANSI from a status entry, which is
why the sibling plugin that lived there could not be coloured.)

---

## The look

The overlay is drawn from three things: the accent colour, the palette
derived from it, and the glyphs.

**The accent is the user's own.** It comes from Herdr's theme, in this order:
the plugin's own `accent` setting, Herdr's `theme.custom.accent`, Herdr's
`ui.accent`, the plugin's `theme` setting, and Herdr's `theme.name`. A user
who runs dracula gets dracula's pink without configuring anything. The rest
of the palette is derived from it -- the chords are a dimmed variant, the
hints a muted one -- so the overlay reads as one hue instead of a mix of
cyan and magenta.

**The candidates are a chain, not a queue.** An accent that cannot be read is
refused and the chain goes on, because a readable candidate later in the list
is better than the built-in one: a user with an unreadable
`theme.custom.accent` and dracula still gets dracula's pink. The refusal is
reported on stderr, once.

**The background is unknown, so the accent is checked.** The popup is drawn
over whatever the terminal shows and no plugin can ask. An accent that fails
4.5:1 against *both* a dark and a light background is unreadable on either,
so it is refused: the next candidate is tried and the user is told, once, on
stderr. An accent that works on one of the two is used as it is.

**The frame is drawn inside the anti-wrap budget.** The box, the breadcrumb
in the top border and the key hints in the bottom border are all part of the
line, so the content gets two columns less than the terminal and the right
border is at the same column on every row. The frame never pads a line that
is too wide: that would hide the bug the anti-wrap guard exists to catch.

**The glyphs are Nerd Font v3 icons**, one per layer and one per group in the
reference view, and they are one column wide: `table.char_width` counts the
private use area as one column, because a terminal that cannot draw the
glyph shows a box, which is one column too. They are opt-out -- `icons =
false` -- for a font that does not have them.

**The navigatrs are the one place with a colour that is not the accent.**
The agent's status is the thing the eye should go to -- an agent that is
blocked wants the user now -- so the status is coloured by severity, in
the theme's own colours: blocked in the error colour, working in the
theme's yellow (the way Herdr's own sidebar draws a busy agent), done in
the ok colour, and the quiet ones muted. The rest of the row is quiet so
the status stands out:

- the row under the cursor is on a **band**: the accent is the band's
  background and the row's text is the panel's background colour. It is
  the clearest statement of "this is the one you are on" a terminal can
  make, and Herdr's own navigation surface makes it.
- the kind marker is coloured by depth -- the deeper, the brighter, so the
  leaves (the things you can jump to) stand out and the structure recedes.
- the row's number is in the dimmed accent, and the description (the
  counts, the location) is muted. The location is a step brighter than the
  counts: a place is worth more than a count of things.
- the current row's arrow is in the accent.
- the **status is a column**: the glyph is at the right-hand end of the
  row, so the colours of a whole list line up.

**Every colour the overlay opens is closed**, in one place: `overlay._paint`.
A colour that is opened and not closed runs on into everything after it.

**The palette is the theme's, not ours.** Herdr's own `[theme.custom]` table
holds the accent, the green, the red, the yellow, the panel background and
the selection background, and the plugin reads all of them. The rest comes
from the palette of the theme the user is running, which the plugin ships
because Herdr's built-in themes are compiled into its binary and there is
nothing to read them from.

That table was wrong for dracula: the plugin had dracula's **pink**
`#ff79c6` where Herdr uses its **purple** `#bd93f8`, so the overlay and
Herdr's own chrome were two different hues in the same window. The
dracula palette was MEASURED -- sampled out of Herdr's own rendering of its
navigation surface -- and the others are the published palettes of their
themes. The copy can drift, so Herdr's own `[theme.custom]` colours win
over it, and the plugin's own `visuals.toml` wins over both.

**The settings are the user's.** `visuals.toml` (accent, icons, frame,
theme) and `icons.toml` (the glyphs) live in the plugin config dir and are
merged over the bundled copies, key by key: one setting can be overridden
without copying the whole file.

**The bundled `visuals.toml` is where the defaults come from.** They are
read from it, not written out in the code as well, so the two cannot drift
apart. The *names* of the settings the plugin reads are in the code, and a
setting that is not one of them is dropped with a word on stderr: a
misspelled setting (`icon` instead of `icons`) that is ignored in silence
looks exactly like a setting that did not work. The warning is once per key,
because the overlay renders on every keystroke.

---

## What we can do next

The repo-wide roadmap is in the root [`AGENTS.md`](../AGENTS.md). v2-specific,
roughly in order of value:

- **Probe `command.invoke` and `client_shell.surface.set`.** Both are in the
  0.9.5 schema and neither is on the website. `command.invoke` takes "an
  opaque endpoint-issued command identifier from the client-shell projection";
  `client_shell.surface.set` "updates whether the requesting client shell
  receives and controls pane presentation". If a plugin can drive that, a
  *native* layer bar becomes conceivable rather than a TUI overlay. Probe
  before designing around it.
- **A copy-mode layer**, using `pane.copy_motion`, `pane.copy_search` and
  `pane.selection.read`. All three exist in 0.9.5. Copy mode is currently a
  hint row.
- **The navigatrs could show more of the session.** The places tree draws
  the spaces, the tabs and the panes, and the agents list draws the agents.
  A pane's scrollback, or a copy-mode layer of its own, would be the next
  thing to add.
- **One thing in the tree is still not settled**, and it is visible in the
  rendering:
  - **The description is still ragged.** The status is a column now, but
    the counts ("2 tabs · 3 panes") follow the name, so they do not line
    up. Right-aligning them would give the tree a second column.
- **Windows.** The socket client speaks a Unix domain socket; Herdr uses a
  named pipe on Windows. A `WindowsPipeTransport` would slot in beside
  `SocketTransport` without touching callers.

---

## Conventions

- Python 3.9-compatible, stdlib only.
- Comment *why*, not *what*. Cite the constraint by name so the next reader
  can check it.
- Tests for anything that edits user files, guesses at formats, or claims a
  terminal is a terminal.
- Diagnostics to stderr; the overlay is the only thing that writes to stdout.
