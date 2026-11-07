# AGENTS.md

Working notes for this repository. Read this before changing anything.

## What this is

`managr` is a **modal, layered keymap for Herdr**. One key opens a
popup; the popup is a menu of *layers* (navigation, panes, workspaces, tabs,
session, your own commands), plus **two navigatrs** for the session, and
every layer scrolls, filters and jumps.

```
prefix+space  →  layers
                  n  navigation      p  panes
                  w  workspaces      t  tabs
                  s  session         c  custom
                  j  navigatr       a  agents
                  ?  all bindings
```

It exists because Herdr's own keymap is **flat**: every binding is `prefix`
plus exactly one chord, so there is no way to *discover* what is bound
without leaving the terminal. This plugin is the layer under the prefix —
a tree you can walk, with the Herdr chord shown beside every action so you
learn the real binding while you use it.

Two navigatrs, because "where am I" and "what needs me" are different
questions:

- **`j` — places.** The spaces, tabs and panes of the running session, as a
  tree. Every row says what is inside it (`2 tabs · 3 panes · 1 agent`).
- **`a` — agents.** Every agent in the session, in one flat list, the ones
  that want attention first: blocked, then working, then done, then idle.

The places navigatr **folds** the spaces it is not showing — `h` and `l`
fold and open the space under the cursor, `H` and `L` every space — so it
opens as a list of spaces rather than a wall of panes to read past. And it
**rearranges**: `r` picks a tab or a pane up and puts it down somewhere
else. A tab takes another tab's place in its own space, because that is
the one move Herdr's `tab.move` can make; a pane goes into any tab, or
into a new tab of any space, because `pane.move` can do that. See
[`v2/AGENTS.md`](v2/AGENTS.md) for why each of those is the way it is.

The plugin is **one directory** (`v2/`) and **one install**. It was two
plugins until the ambient status-bar hint was dropped: the popup holds
everything the bar held, and more.

---

## The one thing to understand first

**Herdr has no keybinding API.** Three independent walls, all verified against
a live 0.9.5 install:

1. **A plugin cannot declare or claim a key.** The manifest schema has
   `[[build]]`, `[[startup]]`, `[[actions]]`, `[[events]]`, `[[panes]]`,
   `[[link_handlers]]`. There is no `[[keys]]`. Docs are explicit: *"Runtime
   action registration and native non-terminal plugin UI are not part of
   plugin v1."*
2. **No key event exists.** `herdr api schema --json | grep -i key` returns only
   `pane.send_keys` and `agent.send_keys`, which write *into* a pane's PTY.
   Herdr's own key layer sits upstream of that, so a plugin can neither observe
   a prefix press nor fake one to fire a binding.
3. **The keyspace is flat.** Every binding is `prefix` plus exactly one chord.
   There is no `<leader>ff` tree to descend into.

To re-verify after a Herdr upgrade:

```sh
herdr api schema --json | grep -oE '"[a-z_]+\.[a-z_]+"' | sort -u | grep -i key
```

If a `keys.*` method ever appears, a lot of the scraping in `keymap.py` becomes
unnecessary and this file should be updated.

**Consequence:** hold-a-prefix-to-reveal is not buildable. This plugin is
explicitly invoked instead — and a *popup* is what makes that work, because a
popup takes every keystroke and so owns a private keyspace.

---

## Constraints this design is built around

From Herdr's own docs, not from caution. If any of these change, re-read them:

| Constraint | Consequence |
|---|---|
| Commands run via `/bin/sh -lc`, **not** as plugin invocations | no `HERDR_PLUGIN_ROOT` in that process |
| A `type = "popup"` binding is likewise launched by the server | same reason — so the binding invokes the plugin's own action instead |
| A `[[keys.command]]` popup has **no title field** | a popup opened by a config binding is called "popup"; the plugin's own pane has a title, so the binding goes through the plugin |
| A popup is **session-modal** and takes every keystroke | the overlay owns a private keyspace and cannot shadow a Herdr binding |
| A popup is a **session singleton**, wherever it was opened | an action that moves the workspace or the tab must close the overlay, or it holds the keyboard on the old one |
| A second popup fails with `ui_busy` | the navigatrs are drawn in our own overlay, never in another plugin's popup |
| A popup has **no pane id** | no socket method can send it keystrokes. This is why there is a pty test. |
| The popup's pty is sized to Herdr's **inner text area** (`outer - 3`) | `get_terminal_size` already reports the text width; there is no border to subtract |
| Herdr's **status area strips ANSI** and renders one line | no colour there, ever. The popup *is* a real terminal, so it has colour. |

---

## The structure

```
Makefile              delegates to v2/
AGENTS.md             this file
v2/                   the plugin — the whole of it
  herdr-plugin.toml   2 actions, 1 popup pane (titled "Managr")
  bin/managr          open | overlay | tree | explain
  src/managr/
    tomlite.py        tolerant TOML subset (macOS python3 is 3.9, no tomllib)
    keymap.py         the keymap engine: the chords shown beside the actions
    client.py         the socket client
    actions.py        action -> socket call, and what is not reachable
    apicheck.py       the action map, measured against the live schema
    tree.py           the layer tree, merged from bundled + user files
    table.py          the layout engine: measured columns, anti-wrap
    overlay.py        the modal TUI: the render path and the row composers
    navigatr.py       the two navigatrs: the tree of places, the list of agents,
                      the folds, and the moves
    custom.py         the user's commands and other plugins' actions
    visuals.py        the theme: the palette, the glyphs, the accent
    merge.py          one merge rule for every user-editable file
    data/             layers.toml, visuals.toml, icons.toml, the 0.9.5 schema
  scripts/            configure, live_check, live_workspace_check,
                      verify_claims, api_check
  tests/              553 tests in 8 suites, including a real-pty run
```

### How a row is drawn

The overlay composes every row by hand, because a tree is not a table:

- **`overlay._fit_row`** decides what fits. A row is a list of *parts*, each
  with a priority (what is given up first), a place (before the label, after
  it, or at the right-hand end) and a colour. The dependent elements go
  first and the label is cut last. The number is not one of them: it is in
  a column of its own, in the prefix, so it is never given up.
- **`overlay._navigatr_line`** composes a row of the places tree; the branch
  marker, the kind glyph and the name move together, so the content's
  column *varies with the depth*.
- **`overlay._agent_line`** composes a row of the agents list: flat, no
  markers, the status word in a column and the location at the right.
- **`table.visible_width`** is the only way to measure anything. A CJK title
  is one character and two columns.

### How a change flows

1. **Write the guard first.** A test that fails without the change, and a
   `check_revert` in `scripts/verify_claims.py` that breaks the code on
   purpose and confirms the suite catches it. A guard that stays green when
   the bug is back proves nothing.
2. **Change the code**, then `make check` (every suite, on this Python *and*
   on the system 3.9 — 3.9 is the interpreter with no `tomllib`, so it is the
   one that exercises the fallback parser).
3. **`make -C v2 verify-claims`** — 146 guards, each one breaking the code
   on purpose. It is a **background job** (fifteen minutes; see Commands).
4. **`make -C v2 live-check`** — the real overlay, driven through a pty
   against the running server. A change that only the unit tests see is a
   change that was not tested.
5. **A review that reads the diff and breaks the code itself**, not one that
   reads the tests. The review findings in this repo's history were real
   bugs: a `keep = true` that was dead code, and a read that threw away
   every keystroke but the first.

---

## Commands

```sh
make help          # every target
make test          # the test suites
make check         # lint + test, on every python
make install       # link, add the binding, reload
make uninstall     # remove the binding, then unlink
make status        # a one-screen summary
make tree          # print the layer tree
make explain       # the executable actions and the hint rows
make open          # open the overlay right now, without the keybinding
make live-check    # drive the real overlay against the running Herdr
make -C v2 live-workspace-check  # the overlay closes itself when the workspace moves
make verify-claims # prove the regression guards are real, by breaking the code
make api-check     # measure the action map against the running Herdr
```

**`make -C v2 verify-claims` is a background job.** It is 146 guards, and
each one copies the whole tree into a scratch directory, breaks the code
there and runs a suite — so it takes **fifteen minutes**, not three. Run it
in the background and do something else:

```sh
make -C v2 verify-claims > /tmp/guards.log 2>&1 &
```

The output is buffered until the end, so an empty log means it is still
running, not that it has failed. The line that matters is the last one:
`146 passed, 0 failed`.

`make check` is quick enough to run in the foreground (about a minute; the
pty suite is most of it), and it is the one to run after every change.
`verify-claims` is the one to run before a change is called done.

Testing a config change without touching the real one:

```sh
make -C v2 install HERDR_CONFIG_PATH=/tmp/test.toml
```

---

## Rules for changing this repo

**Never hand-edit `~/.config/herdr/config.toml` for this plugin.** Use
`make install` / `make uninstall`. Hand edits produce configs that uninstall
cannot clean up — this already happened once.

**A rename has to carry the old id with it.** The plugin has been renamed
four times — `herdr-nextkey`, `herdr-layers`, `herdr-keybar`, `herdr-chord`,
and now `managr` — and the id is not just a label: it is what a binding in
`config.toml` names, and what `bin/managr` asks Herdr for. Rename the id and
the old one has to be *recognised*, not forgotten: `configure.py` keeps the
earlier qualified ids in `LEGACY_ACTION_IDS` so an upgrade **replaces** the
binding the old id wrote instead of adding a second one beside it, and the
uninstall removes both. The same list is what stops the plugin from asking
Herdr for a plugin that does not exist — the `layers` → `chord` rename left
`herdr-layers` in the default and the plugin could not start at all, which
is why `test_entrypoint.py` now refuses every name the plugin has ever had.

**A rename is not a find-and-replace.** "chord" is both the old name and the
domain word for a key combination, and the two live in the same files: the
overlay's column is called `chord`, `keymap.pretty_chord()` formats a
binding, `palette.chord_rgb` is the dimmed accent. Only the *identity*
tokens — `herdr-chord`, `Herdr Chord`, `herdrchord` — are renamed. A blanket
`s/chord/managr/g` rewrites the plugin's core vocabulary.

**The footer is one line, and it is a ladder.** The room is the popup's own
width — seventy percent of the terminal — and the position badge takes some
of it, so at an eighty-column terminal the footer has forty-nine columns and
at a hundred-column terminal it has sixty-three. The hints are therefore a
ladder, widest first, and the first one that fits is the one shown: a hint
that gets cut in half says less than one that is complete. The keys that
cannot be guessed come first in the ladder, because they are the ones
nobody finds any other way — `h`/`l` fold a space, `H`/`L` fold them all,
`r` rearranges. The numbers and the movement keys have been in the footer
since the beginning and the user knows them. A footer that names a key that
does nothing is worse than no footer: the agents list has neither the
spaces nor the rearranging, so it does not offer those keys.

**The config mutator finds its own table and touches nothing else.** The
binding is a `[[keys.command]]` entry, and it is found by its `command`
value, not by position, so the user's own custom commands survive it and
their order does not matter. The mutator only ever rewrites the fields it
owns — `key`, `type`, `command`, `description` — and never a line the user
wrote. A binding from an earlier version (the `type = "popup"` one) is
recognised and replaced, so an upgrade does not leave two bindings for one
key.

**Every value written to config stays on one line.** A multi-line array is
unrecoverable — its continuation lines are not `key = value` lines, so a
second install cannot find the array and a later uninstall strands the rest.

**Check before writing.** The mutator checks the result and leaves the
original untouched when it fails. The check is a real TOML parse when the
interpreter has `tomllib` (3.11+) and a structural check on every Python —
and the mutator runs on whatever `python3` is on PATH, which on macOS is the
system 3.9. So the guarantee is "the result passes the checks this
interpreter can make", not "the result is valid TOML".

**One merge rule for every user-editable file.** `layers.toml`,
`visuals.toml`, `icons.toml` are all merged over the bundled copies by
`merge.py`: the user's value wins for a setting the plugin knows, a named entry
the user names has its fields merged over the bundled entry's, a list the user
names replaces the bundled list, and a name the plugin does not know is
dropped with a warning on stderr that names it and the file it came from. A
setting that is ignored in silence looks exactly like a setting that did not
work.

**No new dependencies.** Stdlib only, Python 3.9+. macOS ships a `python3` with
no `tomllib`, which is why `tomlite.py` exists.

---

## Lessons (bugs that actually happened)

**The overlay's context is a snapshot, and it has to be re-read.** The
workspace, the tab and the pane the overlay was opened on are read once.
An action that moves the focus leaves them stale, so "split right" after
"focus right" split the pane that was focused when the overlay opened. The
context is re-read after every action now — and the re-read is also what
tells the overlay that the workspace or the tab moved under it, which is
when it has to close itself.

**A popup is a singleton, and it holds the keyboard where it was opened.**
Switching the workspace from the overlay and leaving it open stranded the
popup on the old workspace: the user was in the new one and the keyboard was
not, and the plugin could not be opened again until the overlay was closed
by hand. The overlay closes itself when the workspace or the tab changes.

**A config binding has nowhere to put a title.** `[[keys.command]]` has no
`title` field, so a popup opened by one is called "popup" — and the user
has to live with that, because the title is the one thing a plugin cannot
set from the outside. The plugin's own pane in the manifest has one, and a
`plugin_action` binding opens that.

**A band is a background, and a reset closes a background.** The row under
the cursor is drawn on a band of the accent. The reset that closes a colour
closes the background too, so the parts of a band row are not closed between
them: a row that closed its first part ended its band after two columns.

**The parts painted *inside* the row are the ones that break the band.** The
kind glyph of the tree and the marker of the current row are drawn inside the
row, and they closed their own colour: the band ended after them, the rest of
the row was drawn in the panel's background colour on the terminal's own
background, and the row under the cursor was invisible — a short stripe of
band with the row gone. A band row is ONE run: every part of it is painted
without closing its colour, and the band is closed once, at the end of the
row. That includes the cut of a row that does not fit: the cut must not close
the run either.

**Raw mode turns off ONLCR.** `termios`'s `cfmakeraw` clears `OPOST`, so a
bare `"\n"` no longer returns the cursor to column 0 and every line starts
where the previous one ended — a diagonal staircase. Write `"\r\n"`
explicitly; do not rely on the terminal to translate it.

**A `ps` poll is not a verification method.** The status process lives ~40 ms.
Polling every 50 ms for 12 s missed it every time, and the obvious conclusion
— "the server never runs it" — was wrong. Use a command with an observable
side effect, or nothing.

**Markers that wrap too much delete the user's data.** The first
`configure.py` put the marker block *after* `[ui]` and *before* the next
section, so the block enclosed the user's own `sidebar_collapsed_mode` and
`status_indicators`. `strip_markers` then deleted them on uninstall. The fix
was to invert the ownership model: the block contains only lines we added.

**Be suspicious of a heuristic that rejects valid input.** `_looks_like_binding`
originally required a value to be a known special key, which silently dropped
the navigate-mode plain letters (`navigate_pane_up = "k"`).

**A zero-sized terminal renders nothing.** The overlay computed a row budget
from the terminal size. A pty with no window size reports zero rows, the
budget went negative, and the overlay rendered *no items at all* — a blank
popup. Found by the pty test, not by inspection.

**A grep-based check is a fake guard.** The audit script looked for a string
in the source. With the fix genuinely reverted and the old string left in a
comment, it still printed PASS. Every guard must be behavioural: break the
code and confirm something fails.

**A copied test tree still imports the original source.** A test computes
`ROOT` from its own `__file__`, so running a copy from a scratch directory
still exercises the original `src/`. When a script copies the tree to break
something, it must run the copy *from* the copy.

**A stale `.pyc` can hide a revert.** A copied tree keeps its compiled
caches, and Python reuses a `.pyc` whose recorded timestamp and size still
match the file beside it. The scratch copy leaves the `__pycache__`
directories out.

**Verify claims against the machine, not the README.** The prior-art plugin's
README said 52 actions; a direct parse of `--default-config` found 39 real
ones. Do not relay numbers from documentation without running them. The same
goes for a theme: the plugin's dracula accent was dracula's *pink*, and
Herdr's own rendering says it is the *purple*.

---

## What we decided, and why

**A popup, not a bottom split.** A popup is session-modal and simple. A bottom
split keeps the layout visible while you pick, which is more Zellij-like, but
opening and closing it reflows every pane in the tab twice.

**The binding invokes the plugin's own action.** A `[[keys.command]]` popup
has no title field, so Herdr calls that floating window "popup"; the
plugin's own pane in the manifest has a title, and it is the only way to
name the window. A `type = "plugin_action"` binding opens it — and as a
bonus the plugin runtime runs the pane's command with the plugin root as the
working directory, so the generated launcher that the popup binding needed
is gone.

**The overlay closes itself when the workspace or the tab moves.** A popup
is a session singleton that takes every keystroke *wherever it was opened*.
An overlay left open on the old workspace holds the keyboard there, and the
plugin cannot be opened again on the new one. So the overlay re-reads its
context after every action and closes when the workspace or the tab changed.
Moving the focus between panes does not close it: that is what the
navigation layer is for.

**The overlay owns a private keyspace.** A popup takes every keystroke, so the
keys inside it never reach Herdr's prefix handler. That is the decisive
difference from which-key.nvim, which intercepts the *real* keymap and
therefore must never shadow anything.

**Actions are executed, not simulated.** `pane.send_keys` writes into a pane's
PTY and Herdr's key handling sits upstream of that. The plugin calls the socket
API directly instead.

**A navigatr row is one composed string, not aligned columns.** A tree's
alignment is its indentation; the aligned table renderer is for the layers,
where the columns SHOULD line up. It flattened the tree into a wall of labels.
(The one exception is the status, which is a *column* on purpose — see
below.)

**A tab is a heading, not a place.** The tree is spaces, tabs and panes,
and the tab is the only one of the three that is not a destination: a tab
always has at least one pane, and focusing a tab lands on a pane anyway.
So the cursor skips the tab rows — a pane to a pane is one key, not two —
and `enter` refuses one. The tab row is still drawn, with the counts and
the status of the tab.

**The three tiers of the tree are told apart by their colour.** The space
is the section title and is in the accent, the tab is the group heading and
is muted, the pane is the item the user jumps to and is in the plain
colour. The markers and the indentation were not enough: the three of them
were a wall of labels in the same colour, and the eye had to work out the
structure of the tree row by row.

**The number of a space has a column of its own**, in front of the tree's
markers. It used to sit at the front of the *label*, and it pushed the name
of a space two columns to the right of the name of the tab under it: the
tree's names no longer marched with its depth. A column of its own keeps
the numbers in one column and the names marching -- and the number is
never given up, because the footer promises the keys that name the spaces.

**A number moves the cursor; `enter` selects.** Aiming with a number and
confirming with enter is faster than hunting with the arrow keys, and it
cannot land on the wrong space.

**The agent is folded into the pane's row.** `agent.focus` and `pane.focus`
land on the same pane — the agent's pane is the pane that hosts it — so an
agent row of its own was a second name for the same destination. Herdr's own
navigation surface puts the agent in a column for the same reason.

**The panel is the window.** The overlay draws its frame to the popup's full
height, so the keys are at the bottom of the *window* and not at the bottom
of the content. A frame that wraps the content leaves the keys half way
down the popup, which is where nobody looks for them, and leaves the rest
of the window empty.

**The colour is the theme's.** The plugin reads Herdr's own
`[theme.custom]` colours — the accent, the green, the red, the yellow, the
panel background — and falls back to the palette of the theme the user is
running. Dracula's accent is its **purple** `#bd93f8`, not its pink: the
pink is what the plugin used to assume, and the two sat side by side in
the same window. The plugin's own `visuals.toml` overrides everything.

**In the navigatrs, the colour says how urgent something is.** The agent's
status is coloured by severity, in the theme's own colours: blocked in the
error colour, working in the theme's yellow — the way Herdr's own sidebar
draws a busy agent — done in the ok colour, and the quiet ones muted. The
rest of the row is quiet so the status stands out.

**The status is a column.** It is drawn at the right-hand end of the row,
so every status in a list is in the same place and the colours form a band
the eye can scan. It used to sit next to the name, which was right when
the rows were a pile of ragged text -- out at the right edge, the status was
forty columns from the thing it described. A column answers that: not a
status far away, but a status every row has in the same place.

**The selected row is on a band.** The row under the cursor is drawn on a
band of the accent, the full width of the panel, with the row's text in the
panel's background colour: light text on a light band cannot be read.

**The navigatr's shape follows Herdr's.** The count in the header, the
status in a column, the band under the selected row, the path and the
directory under the list, and the agent folded into the pane's row are all
the patterns Herdr's own navigation surface uses. It is not a copy: it is
the same answer to the same question, by the same author who asked the
question.

**Not competing with `cowboyvang/herdr-which-key`.** It is a genuinely good
plugin and is the right answer for a press-to-drill-down *of Herdr's own
bindings*. This one is a task-oriented tree that executes over the socket and
owns a private keyspace. The two complement.

---

## Prior art and references

- [Herdr plugin docs](https://herdr.dev/docs/plugins/) — manifest, panes, actions
- [Socket API](https://herdr.dev/docs/socket-api/) — the method list that proves
  there is no `keys.*`
- [Configuration](https://herdr.dev/docs/configuration/) — `[[keys.command]]`,
  `theme.custom.*`, `ui.*`
- [`cowboyvang/herdr-which-key`](https://github.com/CowboyVang/herdr-which-key)
  — the interactive half for Herdr's *own* bindings
- [`thanhdat77/herdr-navigator`](https://github.com/thanhdat77/herdr-navigator)
  — a fuzzy navigatr; we borrowed its glyph vocabulary, not its code

---

## What we can do next

**Ship.**
- The repository has a `.git` and **no commits**. That is the highest-value
  item by far: nothing is revertible and nothing can be published.
- Then a public GitHub repo with the `herdr-plugin` topic, which gets it
  listed in Herdr's marketplace (indexed automatically, ~30 min).
- CI on Python 3.9 and 3.13, Linux and macOS. 3.9 matters: it has no
  `tomllib`, so it is the interpreter that exercises the fallback parser.

**Finish the navigatrs.** One thing is visible in the current rendering
and it is not settled:
- **The places tree's description is still ragged.** The status is a column
  now, but the counts ("2 tabs · 3 panes") follow the name, so they do not
  line up. Right-aligning them would make the second column of the tree.
- **A tab cannot be moved into another space, and the plugin says so.**
  Herdr's `tab.move` takes an `insert_index` and nothing else: the index
  is counted inside the tab's own space, and the `tab.moved` event carries
  "the updated ordered `tabs` list for that workspace". There is no call
  that crosses a space. It *could* be emulated -- `tab.create` in the
  target space, `pane.move` every pane into it, `tab.close` the old one,
  rename the new one -- but the tab's identity changes, the panes get new
  ids, and it is three calls that can half-fail. Worth deciding before
  it is offered, not after.

**Make it better.**
- A copy-mode layer, using `pane.copy_motion` / `pane.copy_search` /
  `pane.selection.read`. These exist in 0.9.5 and `api_check` has already
  measured what each of them does; copy mode is currently a hint row.
- The `command.invoke` and `client_shell.surface.set` socket methods are
  undocumented on the website and sound like a scripting surface for a client
  shell. If a plugin can drive that, a *native* layer bar becomes conceivable
  rather than a TUI overlay. Probe them before designing around them.
- The navigatr fetches four lists when it opens. A session with many panes
  makes that a big payload; a summary-then-detail fetch would be cheaper.

**Upstream.**
- File a Herdr feature request for the two APIs the ecosystem needs: a
  keymap-introspection socket method, and manifest key declaration. Until
  those exist, no which-key-style plugin can hold a prefix to reveal. Point at
  `cowboyvang/herdr-which-key`'s "Upstream" section, which already argues this
  case.

**Maintenance.**
- Re-check the keymap gaps on every Herdr release; `make verify-claims` and
  the `no label for` warning are the early signals.
- `make api-check` measures the action map against the running server: every
  method and parameter against Herdr's schema, and the safe actions for
  real. Run it after every Herdr upgrade — it is the thing that would catch a
  changed parameter name.
- The theme palettes in `visuals.py` are ours, and Herdr's are compiled into
  its binary: they can drift. Re-measure them when Herdr changes a theme
  (the dracula one was measured out of Herdr's own rendering), and keep
  `[theme.custom]` winning over them.
- Windows: the socket client speaks a Unix domain socket; Herdr uses a named
  pipe there.

---

## Conventions

- Python 3.9-compatible, stdlib only.
- Comment *why*, not *what*. The constraints above are the "why" that matters;
  cite them by name so the next reader can check them.
- Tests for anything that edits user files, guesses at formats, or claims a
  terminal is a terminal.
- Diagnostics to stderr; the overlay is the only thing that writes to stdout.
