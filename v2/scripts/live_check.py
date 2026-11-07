#!/usr/bin/env python3
"""Drive the real overlay against the real Herdr server, through a pty.

This is the end-to-end check the unit tests cannot make: the overlay renders,
reads keys, and executes a real socket call that really moves focus in the
running session.

    python3 scripts/live_check.py
"""

import json
import os
import pty
import re
import select
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# The escape sequences the overlay writes.
#
# The checks below are about what the user *sees*, and the overlay is now
# coloured: the accent comes from the user's own Herdr theme, and the
# breadcrumb's last crumb is highlighted. So the colour is stripped before
# anything is searched for, otherwise a check for a piece of text fails
# because of an escape sequence in the middle of it.
ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")


def visible(text):
    """`text` without the escape sequences: what the terminal shows."""
    return ANSI.sub("", text)

DRIVER = r'''
import os, sys
sys.path.insert(0, "__SRCPATH__")
from managr import client as client_mod, keymap as keymap_mod, overlay as overlay_mod, tree as tree_mod
from managr.actions import Context

class LoggingClient(client_mod.HerdrClient):
    """The real client, with every call written down."""

    def __init__(self, *args, **kwargs):
        client_mod.HerdrClient.__init__(self, *args, **kwargs)
        self.calls = []

    def call(self, method, params=None):
        self.calls.append((method, params))
        return client_mod.HerdrClient.call(self, method, params)

km = keymap_mod.build(labels={})
tree = tree_mod.build(keymap=km, labels={})
client = LoggingClient()
ctx = Context(client)
ctx.refresh()
sys.stderr.write("START_PANE=" + str(ctx.pane_id) + "\n")
surface = overlay_mod.Overlay(tree, ctx)
code = surface.run()
sys.stderr.write("EXIT=" + str(code) + "\n")
sys.stderr.write("CALLS=" + repr(client.calls) + "\n")
'''.replace("__SRCPATH__", os.path.join(ROOT, "src"))


def socket_call(method, params=None):
    import socket as socket_mod

    path = os.environ.get("HERDR_SOCKET_PATH") or os.path.expanduser(
        "~/.config/herdr/herdr.sock"
    )
    sock = socket_mod.socket(socket_mod.AF_UNIX, socket_mod.SOCK_STREAM)
    sock.settimeout(5)
    sock.connect(path)
    try:
        sock.sendall(
            (json.dumps({"id": "live", "method": method, "params": params or {}}) + "\n").encode()
        )
        buf = b""
        while b"\n" not in buf:
            chunk = sock.recv(65536)
            if not chunk:
                break
            buf += chunk
        return json.loads(buf.decode().split("\n")[0])
    finally:
        sock.close()


def focused_pane():
    result = socket_call("pane.current")
    return (result.get("result") or {}).get("pane", {}).get("pane_id")


def focused_workspace():
    result = socket_call("workspace.list")
    spaces = (result.get("result") or {}).get("workspaces", [])
    for space in spaces:
        if space.get("focused"):
            return space.get("workspace_id")
    return None


def focused_workspace_row():
    """How many cursor moves reach the focused workspace, or None.

    The tree is spaces -> tabs -> panes, so the rows of the spaces are not
    next to each other: the position of the focused space has to be counted
    with the plugin's own tree builder, or the jump would go to the wrong
    row.

    The count is of the rows the cursor can *stop on*, not of the lines:
    a tab row is a heading, the cursor skips it, and `enter` never goes to
    one. Counting the tab rows as moves lands the cursor on a pane further
    down the tree, and the jump becomes a `pane.focus` instead of the
    `workspace.focus` this check is looking for.
    """
    sys.path.insert(0, os.path.join(ROOT, "src"))
    from managr import navigatr as navigatr_mod

    def listing(method, key):
        result = socket_call(method)
        payload = result.get("result") or {}
        return payload.get(key) or []

    rows = navigatr_mod.build(
        listing("workspace.list", "workspaces"),
        listing("tab.list", "tabs"),
        listing("pane.list", "panes"),
        listing("agent.list", "agents"),
    )
    moves = 0
    for row in rows:
        if not row.selectable:
            continue
        if row.kind == "workspace" and row.current:
            return moves
        moves += 1
    return None


def run_overlay(keys, timeout=25.0):
    master, slave = pty.openpty()
    try:
        import fcntl
        import struct
        import termios

        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 30, 100, 0, 0))
    except Exception:
        pass

    proc = subprocess.Popen(
        [sys.executable, "-c", DRIVER],
        stdin=slave,
        stdout=slave,
        stderr=subprocess.PIPE,
        cwd=ROOT,
        close_fds=True,
    )
    os.close(slave)

    output = b""
    deadline = time.time() + timeout
    time.sleep(1.0)
    for key in keys:
        if time.time() > deadline:
            break
        try:
            os.write(master, key)
        except OSError:
            break
        time.sleep(0.7)
        while select.select([master], [], [], 0.05)[0]:
            try:
                chunk = os.read(master, 65536)
            except OSError:
                break
            if not chunk:
                break
            output += chunk

    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=5)
    os.close(master)
    stderr = proc.stderr.read().decode("utf-8", "replace")
    proc.stderr.close()
    return output.decode("utf-8", "replace"), stderr


def check_no_op_feedback(before, after, output):
    """Did the overlay report what happened?  Returns (ok, message).

    A direction with no neighbour must say so rather than fail silently:
    the user pressed a key and needs to know it registered. The check lives
    in a function so it can be exercised without a running Herdr -- a
    warning that never reaches the exit status is not a check.
    """
    reported = "nothing that way" in visible(output)
    if before != after:
        if reported:
            return False, (
                "focus moved %s -> %s but the overlay said 'nothing that way'"
                % (before, after)
            )
        return True, "focus moved %s -> %s" % (before, after)
    if reported:
        return True, "the no-op was reported ('nothing that way')"
    return False, "no feedback for a no-op action"


def main():
    socket_path = os.environ.get("HERDR_SOCKET_PATH") or os.path.expanduser(
        "~/.config/herdr/herdr.sock"
    )
    if not os.path.exists(socket_path):
        print("SKIP: no herdr socket at %s" % socket_path)
        return 0

    before = focused_pane()
    print("focused pane before: %s" % before)

    # n = navigation layer, h = focus pane left, esc = up a level, esc = close
    out, err = run_overlay([b"n", b"h", b"\x1b", b"\x1b"])
    after = focused_pane()

    print("focused pane after:  %s" % after)
    print("stderr: %s" % err.strip().replace("\n", " | "))

    ok = True
    if "START_PANE" not in err:
        print("FAIL: the overlay never started")
        ok = False
    if "EXIT=0" not in err:
        print("FAIL: the overlay did not exit cleanly")
        ok = False
    seen = visible(out)
    if "layers › navigation" not in seen:
        print("FAIL: the navigation layer did not render")
        ok = False
    if "focus pane left" not in seen:
        print("FAIL: the items did not render")
        ok = False
    if out.replace("\r\n", "").count("\n"):
        print("FAIL: a bare newline was written; the output would staircase")
        ok = False
    if "unreachable" in err or "Broken pipe" in err:
        print("FAIL: the socket call failed")
        ok = False

    # A direction with no neighbour must say so rather than fail silently.
    # A silent no-op is a real failure, not a warning: the user pressed a
    # key and got nothing back.
    feedback_ok, feedback = check_no_op_feedback(before, after, out)
    print("%s: %s" % ("OK" if feedback_ok else "FAIL", feedback))
    if not feedback_ok:
        ok = False

    # The navigatr: the tree of the live session, drawn in the overlay.
    ok = check_navigatr(ok)

    print("\n%s" % ("PASS" if ok else "FAIL"))
    return 0 if ok else 1


def check_navigatr(ok):
    """Drive the navigatr against the running session.

    The tree has to hold the real spaces, the cursor has to be on the
    screen, and Escape has to go back to the root layer without closing
    the overlay. The jump goes to the space that is already focused, so it
    changes nothing -- and if it did, the focus is put back.
    """
    index = focused_workspace_row()
    if index is None:
        print("SKIP: the navigatr check (no focused workspace)")
        return ok

    before_space = focused_workspace()
    out, err = run_overlay([b"j"] + [b"j"] * index + [b"\r"])
    seen = visible(out)

    if "layers › navigatr" not in seen:
        print("FAIL: the navigatr did not render")
        ok = False
    if ">" not in seen:
        print("FAIL: the navigatr has no visible cursor")
        ok = False
    if "esc back" not in seen:
        print("FAIL: the navigatr's footer does not say what escape does")
        ok = False

    # The jump has to be a real socket call, with the parameter name the
    # server wants.
    if "workspace.focus" not in err:
        print("FAIL: the jump did not call workspace.focus")
        ok = False
    if "'workspace_id'" not in err:
        print("FAIL: the jump did not send a workspace_id")
        ok = False

    # The jump went to the space that was already focused, so the focus
    # cannot have moved. If it did, put it back: the check must not leave
    # the user's session somewhere else.
    after_space = focused_workspace()
    if after_space != before_space:
        print("FAIL: the jump moved the focus")
        ok = False
        if before_space:
            socket_call("workspace.focus", {"workspace_id": before_space})

    # And Escape goes back to the root layer, with the overlay still open.
    out, err = run_overlay([b"j", b"\x1b", b"\x1b"])
    frames = out.split("\x1b[H\x1b[2J")
    last = visible(frames[-1]) if frames else ""
    # The breadcrumb, not the word: the root lists the navigatr as an
    # entry, so the word is on the screen even when the overlay is not in
    # it.
    if "layers › navigatr" in last:
        print("FAIL: escape did not leave the navigatr")
        ok = False
    if "layers" not in last:
        print("FAIL: escape did not go back to the root layer")
        ok = False

    print("%s: the navigatr draws the live session" % ("OK" if ok else "FAIL"))
    return ok


if __name__ == "__main__":
    raise SystemExit(main())
