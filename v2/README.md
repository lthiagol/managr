# Managr for Herdr

A modal, layered keymap for Herdr. One key opens a tree of layers; each layer
has its own bare-key vocabulary; actions run directly over the socket API.

```
prefix+space  →  layers
                  n  navigation      p  panes
                  w  workspaces      t  tabs
                  s  session         c  custom
                  j  navigatr       a  agents
                  ?  all bindings
               → press p
                  v  split right     s  split down
                  x  close pane      z  zoom
                  h  resize left     n  rename pane
               → press v  →  the pane splits
```

## Why this works when hold-to-reveal cannot

Herdr has no keybinding API. A plugin cannot declare or claim a key, there is
no key event to observe, and the keyspace is flat — every binding is `prefix`
plus exactly one chord. Hold-a-prefix-to-reveal is therefore not buildable.

This sidesteps all three:

**The overlay has a private keyspace.** A popup is session-modal: it takes
every keystroke, so none of them reach Herdr's prefix handler. `p` and `v`
inside the overlay are the plugin's, and they cannot shadow a Herdr binding.
This is the decisive difference from which-key.nvim, which intercepts the
*real* keymap and therefore must never shadow anything.

**Actions are executed, not simulated.** `pane.send_keys` writes into a pane's
PTY and Herdr's key handling sits upstream of that, so a plugin cannot fake a
prefix press. This plugin calls the socket API directly instead.

**The tree is ours.** Herdr's flat prefix map is one narrow column. Ours can
be shaped the way the work actually reads.

## What it can and cannot do

Most of Herdr's actions are reachable. These are **not**, because they are
Herdr-owned UI surfaces with no socket method — the overlay shows them as hint
rows and tells you which chord to press yourself:

`help` · `settings` · `goto` (session navigator) · `toggle_sidebar` ·
`detach` · `copy_mode` · `resize_mode` · `open_notification_target`

`make explain` prints the full mapping.

## Install

```sh
make install
```

That links the plugin and adds this to `~/.config/herdr/config.toml`:

```toml
[[keys.command]]
key = "prefix+space"
type = "plugin_action"
command = "managr.open"
description = "layered keymap"
```

`make uninstall` restores your config exactly.

### Why the binding goes through the plugin

A `type = "popup"` binding has nowhere to put a title: `[[keys.command]]`
has no `title` field, so Herdr calls that floating window **"popup"**. The
plugin's own pane — the one in the manifest — has a title, and that is what
Herdr puts on the frame, so the window says **"Managr"**.

A `plugin_action` binding is also the simpler route: a popup binding is
launched by the server with no `HERDR_PLUGIN_*` environment, so it cannot
name a path inside the plugin directory and needs a generated launcher to
stand in for one. A plugin action is launched by the plugin runtime, with
the plugin root as the working directory, so the binding names an action and
there is no launcher to keep.

The size of the popup is in the manifest, next to the pane entry, where
Herdr reads it from.

### Why `prefix+space` and not `prefix+?`

Herdr matches a binding against the *character*, not the physical key. On a
layout where "?" and "/" share a key, `prefix+?` means "shift plus slash" —
and on a kitty-protocol terminal that same keystroke also matches
`prefix+shift+?`, so the two bindings become ambiguous.

`prefix+space` needs no shift, is identical on every layout, and leaves
Herdr's native help panel on `prefix+?` where it belongs. This plugin never
touches the native keymap.

To use a different key:

```sh
make install HERDR_MANAGR_KEY="prefix+/"
```

### Doing it by hand

```sh
herdr plugin link /path/to/v2
herdr plugin action invoke open --plugin managr   # opens it now
```

## Using it

| Key | Does |
| --- | --- |
| a layer key | descend into that layer |
| an item key | run the action |
| `j` (at the root) | open the places navigatr |
| `a` (at the root) | open the agents navigatr |
| `h` `l` (in the places navigatr) | fold / unfold the space the cursor is in |
| `H` `L` (in the places navigatr) | fold / unfold every space |
| `r` (in the places navigatr) | start the rearranging: pick a tab or a pane up and put it down |
| `esc` | clear the filter, else back one level, else close |
| `⌫` | back one level |
| `↑` `↓` | scroll a long layer |
| `pgup` `pgdn`, `ctrl+d` `ctrl+u` | scroll half a page |
| `/` | filter the layer by description, key or chord |
| `ctrl+c` | close |

Actions that are repeatable verbs (moving focus) leave the overlay open.
One-shots (split, close) close it. Renames and other actions that need a
value prompt for it inline.

A layer with more rows than the popup has lines scrolls: the bottom border
says where you are (`12/44`) while the list is longer than the window.

`/` opens the filter, the letters you type narrow the list as you type, and
`enter` keeps the filter while `esc` clears it. The filter only hides rows —
the keys still do exactly what they did before. The scroll keys keep
working while the filter is open, so a long filtered list is still
reachable. With a filter in force, `esc` clears the filter first and stays
in the layer; a second `esc` goes back a level. The footer says which of
the two `esc` will do.

## The two navigatrs

Both are lists of places to go, so both are browsed the same way: a cursor on
a list, `enter` to jump, `/` to filter, `esc` to go back. Neither is a layer —
a layer maps keys to actions, and a navigatr is a list of destinations.

### `j` — the places navigatr

The spaces, tabs and panes of the running session, drawn as a tree. Every row
says what is inside it, and the agent of a pane is folded into that pane's
row rather than taking a row of its own: an agent's pane *is* the pane that
hosts it, so a separate row was a second name for the same destination.

```
╭─ layers › navigatr ──────────────────────────────────────────────────────╮
│  the spaces, tabs and panes of this session                               │
│  > ▾ → 1 tmp  2 tabs · 2 panes · 1 agent  ◐                               │
│    ├─ → next-key  1 pane · 1 agent  ◐                                     │
│    │ └─ → OC | Herdr which-key plugin proposal  opencode  ◐               │
│    ▾   2 master rewrite  2 tabs · 3 panes · 1 agent  ✓                    │
╰─ esc back · 1-9 space · enter jump · ↑↓ j/k move · /filter ───────────────╯
```

- A space says how many tabs, panes and agents it holds; a tab says how many
  panes and agents; a pane says which agent is in it.
- `→` marks the space, tab and pane that are current. A status glyph from the
  same vocabulary the `all bindings` view uses sits next to the name.
- `1`–`9` move the cursor to a **space**, numbered in the order they are
  shown. The number does not jump: it aims, so you can see where you are
  pointing, and `enter` does the selecting — the same as it does for any
  other row. The number is drawn on the space, so the key always names a
  row you can see, and it follows the filter: a number never points at a
  space that is hidden. Digits typed while filtering are query text, not
  a move.
- The number is drawn at the **front of the space's name**, not in the tree.
  Anywhere earlier would push the content out of step with the marker that
  describes it, and the depth of a row is exactly what the tree is for.
- The spaces are **folded**: `h` folds the space the cursor is in and `l`
  opens it, `H` folds every space and `L` opens every space. A folded
  space is one row with a `▸` on it, and the rows under it are hidden.
  The navigatr opens with every space folded except the one the session
  is in, so it is a list of spaces to jump between rather than a wall of
  panes to read past. A filter suspends the folds — a filter is an
  explicit request to see something, and a place the filter cannot reach
  is a place you cannot go to.
- `r` starts the **rearranging**: `enter` picks the tab or the pane under
  the cursor up, and `enter` again puts it down on the row the cursor is
  on. A tab is put down on another tab and takes its place; Herdr cannot
  move a tab into another space at all, so a tab stays in the space it is
  in. A pane is put down on a tab and becomes a split of it — `d` says
  which way — or on a space, where it gets a tab of its own. While the
  rearranging is on, the tab rows are targets: a tab is a heading and
  the cursor never stops on one, except when a tab is the thing to aim
  at.
- The hints at the bottom name the keys that cannot be guessed — `h/l`
  folds, `H/L` folds them all, `r` rearranges — and the line is a
  ladder: the widest set that fits in the popup is the one shown, and it
  is never cut in half.

### `a` — the agents navigatr

Every agent in the session, in one flat list, **the ones that want attention
first**: blocked, then working, then done, then idle, then unknown.

```
╭─ layers › agents ─────────────────────────────────────────────────────────╮
│  every agent in this session, the urgent ones first                       │
│  > → 1 ◐ working opencode  tmp › next-key                                 │
│      2 ✓ done opencode  master rewrite › 1                                │
│      3 ● idle cursor  training-prod › 1                                   │
╰─ esc back · 1-9 agent · enter jump · ↑↓ j/k move · /filter ───────────────╯
```

- The list is flat: every row is an agent, so there is no tree to draw.
- The status is a glyph **and** a word. The glyph alone needs the vocabulary
  to be read, and the word alone needs the eye to read seven characters on
  every row. The word goes first when the row is narrow: it is eight
  columns and the glyph is one.
- `→` marks the agent that is focused.
- `1`–`9` move the cursor to an agent, the same way the spaces are numbered:
  it aims, `enter` selects.
- `/` filters against the agent's name, its space, its tab and its title.

### How they are built

Both are built from the live session with four socket calls
(`workspace.list`, `tab.list`, `pane.list`, `agent.list`), made when the
navigatr is opened — not on every keystroke. Re-entering fetches the session
again.

They live inside this overlay rather than in another plugin's popup because a
popup is a singleton in Herdr: only one can be open, and ours already is.

### The colours

The navigatrs are the one place in the overlay with a colour that is not
the accent hue. **The status is what the eye should go to** — an agent that
is blocked wants you now — so it is coloured by severity, in your theme's
own colours: blocked in red, working in yellow (the way Herdr's own sidebar
draws a busy agent), done in green, and the quiet ones muted.

The rest of the row is quiet, so the status stands out: the counts and the
location are muted, the row numbers are dimmed, and the kind marker gets
brighter the deeper the row is.

The status is a **column**: it is drawn at the right-hand end of the row, so
every status in the list is in the same place and the colours form a band
you can scan. The row under the cursor is on a **band** of the accent, the
full width of the panel, with its text in the panel's background colour.

The colours come from **your Herdr theme**: the plugin reads Herdr's own
`[theme.custom]` colours (the accent, the green, the red, the yellow, the
panel background) and falls back to the palette of the theme you are
running. So the overlay is the same hue as the rest of Herdr's chrome
rather than a stranger in the window. You can override any of it in the
plugin's own `visuals.toml`.

### The panel is the window

The popup is a window, and the overlay draws its frame to the **full height
of it** — the breadcrumb at the top, the keys at the bottom of the window,
and the space between them is the panel's body. The keys used to sit at
the bottom of the *content*, half way down the window, which is where
nobody looks for them.

The navigatrs use that space for the selection's details: the path of the
row under the cursor (in full — the row itself is cut to fit) and the
directory it is in. The header carries the count, on the right: how many
spaces and panes, or how many agents and how many of them are blocked.

### The overlay closes itself when the workspace moves

A popup in Herdr is a session singleton: it takes every keystroke *wherever
it was opened*. So if you switch the workspace from the overlay and it
stayed open, the keyboard would still be in the old workspace and the plugin
could not be opened again on the new one.

The overlay re-reads its context after every action and closes itself when
the workspace or the tab changed. Moving the focus between panes does not
close it — that is what the navigation layer is for.

## The custom layer

`c` at the root opens the **custom layer**: the things that are yours rather
than the plugin's.

```
╭─ layers › custom ───────────────────────────────────────────────────────────╮
│  your own commands, and the actions of every plugin                         │
│  a  layered keymap                C-a space     press this in herdr         │
│  b  say hi                        C-a l                                       │
│  c  Open the layered keymap       managr                                │
╰─ esc close · ⌫ up · ? reference · /filter ──────────────────────────────────╯
```

- **Your own commands** are the `[[keys.command]]` entries in your
  `config.toml`. The row shows the description, or the command when there is
  none.
- **The plugins' actions** are every action of every installed plugin, from
  `plugin.action.list`. The row shows the title and the plugin it belongs to.

The rows are not in `layers.toml`: they are fetched when the layer is opened —
not on every keystroke — and they are given a key space of their own, one
character each. `/` is still the filter's and the arrows still scroll, so the
layer behaves like every other layer: it scrolls, it filters, and the bottom
border says where you are.

What happens when you press a key:

- a **plugin action** is invoked with `plugin.action.invoke`. If that action
  wants a popup of its own, Herdr refuses with `ui_busy` — a popup is a
  singleton, and ours is already up — and the overlay says so: *close the
  overlay first*.
- a **`type = "shell"` command** runs detached, the way Herdr runs it: in the
  background, through `/bin/sh -lc`, from the focused pane's directory. The
  overlay does not wait for it.
- a **`type = "popup"` or `"pane"` command** cannot be run from inside the
  overlay: it *is* a popup, and there is no socket method that opens a pane
  and runs a command in it. The row says which key to press in Herdr instead,
  and the key does it there.

An action of a plugin that is disabled, or one that is not for this platform,
is left out — and the reason is on the screen, because a row that is not
shown is a thing you cannot find.

## Configure

Copy `src/managr/data/layers.toml` into the plugin config dir and edit
it:

```sh
cp src/managr/data/layers.toml "$(herdr plugin config-dir managr)/layers.toml"
```

It is **merged** over the bundled file:

- a layer you define is added to the root;
- a layer you redefine takes your title, hint and key;
- if you give it items, they replace the built-in ones; if you give it none,
  it keeps them;
- layers you do not mention are inherited untouched.

```toml
[[layer]]
id = "git"
key = "g"
title = "git"
hint = "worktrees and branches"

[[item]]
layer = "git"
key = "w"
action = "new_worktree"
desc = "new worktree"

[[item]]
layer = "git"
key = "z"
action = "zoom"
desc = "zoom pane"
```

`key` is the overlay's own keyspace, not a Herdr binding. The Herdr chord is
looked up from your live keymap and shown for reference.

## Commands

```sh
make help          # every target
make install       # link + binding + reload
make uninstall     # restores config.toml exactly
make tree          # print the layer tree
make explain       # executable actions vs hint rows
make open          # open the overlay right now
make live-check    # drive the real overlay against the running Herdr
make api-check     # check the action map against the running Herdr
make verify-claims # prove the regression guards are real, by breaking the code
make test          # 457 tests, no Herdr server needed
make check         # lint + test
```

## How it is put together

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
  keymap.py, tomlite.py  the keymap engine and a tolerant TOML reader
  data/layers.toml       the built-in tree
  data/labels.toml       action -> human label
  data/visuals.toml      the look: accent, icons, frame, theme
  data/icons.toml        the Nerd Font glyphs
  data/api-schema.json   herdr 0.9.5's schema, recorded
scripts/
  configure.py           manages the keybinding
  live_workspace_check.py the overlay closes itself when the workspace moves
  live_check.py          drives the real overlay against the running Herdr
  api_check.py           checks the action map against the running Herdr
  verify_claims.py       breaks the code on purpose, to prove the guards
tests/                   457 tests, including a real-pty run of the overlay
```

The suites are discovered, not listed: `make test` globs `tests/test_*.py`,
so a new file is picked up by the next run.

### The action map is checked against the server

Every action is described by a socket method and the parameters it would
send, so `scripts/api_check.py` can ask a running server whether the call
would be accepted -- instead of the map being a pile of hand-written calls
that were only ever checked against the documentation. It checks every
method and every parameter against Herdr's schema, runs the actions that only
move the focus or read the state for real, and searches the schema for a
method for each hint row. It never closes, creates or splits anything, and
the focus it moves is put back.

The 0.9.5 schema is recorded in `src/managr/data/api-schema.json`, so
the check also runs with no server: `tests/test_api_check.py`.

### Why the overlay is a popup, not a bottom split

A popup is session-modal and simple. A bottom split would keep the layout
visible while you pick, which is more Zellij-like, but opening and closing it
reflows every pane in the tab twice. The popup is the default; the split is
the obvious next thing to try.

## Requirements

Herdr 0.9.0+, `python3` (3.9+, stdlib only), Linux or macOS. Windows is out
of scope: the socket client speaks a Unix domain socket, and Herdr uses a
named pipe there.

## Licence

MIT.
