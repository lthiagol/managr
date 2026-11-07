#!/usr/bin/env python3
"""Drive the real overlay through a pty and check that it closes itself
when the action moves the workspace.

This is the bug the user reported: switching the workspace from the overlay
left the popup open on the old workspace, so the plugin could not be opened
again on the new one.

    python3 scripts/live_workspace_check.py
"""

import os
import pty
import re
import select
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")

DRIVER = r'''
import os, sys
sys.path.insert(0, "__SRCPATH__")
from managr import client as client_mod, keymap as keymap_mod, overlay as overlay_mod, tree as tree_mod
from managr.actions import Context

client = client_mod.HerdrClient()
ctx = Context(client)
ctx.refresh()
sys.stderr.write("START=" + str(ctx.workspace_id) + "/" + str(ctx.tab_id) + "\n")
sys.stderr.flush()
km = keymap_mod.build(labels={})
tree = tree_mod.build(keymap=km, labels={})
surface = overlay_mod.Overlay(tree, ctx)
code = surface.run()
sys.stderr.write("EXIT=" + str(code) + "\n")
sys.stderr.flush()
'''.replace("__SRCPATH__", os.path.join(ROOT, "src"))


def socket_call(method, params=None):
    import json
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


def current():
    result = socket_call("pane.current").get("result") or {}
    pane = result.get("pane") or {}
    return pane.get("workspace_id"), pane.get("tab_id")


def drain(fd):
    while True:
        try:
            if not select.select([fd], [], [], 0.2)[0]:
                return
            if not os.read(fd, 65536):
                return
        except OSError:
            return


def run(keys, wait=6.0):
    """Run the overlay in a pty, send the keys, and report what happened."""
    proc, master = pty.fork()
    if proc == 0:
        os.chdir(ROOT)
        os.environ["PYTHONPATH"] = os.path.join(ROOT, "src")
        os.execv(sys.executable, [sys.executable, "-c", DRIVER])
    out = b""

    def pump(seconds):
        """Read whatever the overlay wrote, for `seconds`.

        The pty's buffer is small: if nobody reads it, the overlay
        blocks on its next write and stops answering keys. So it is
        drained the whole time, not only at the end.
        """
        nonlocal out
        deadline = time.time() + seconds
        while time.time() < deadline:
            try:
                ready, _, _ = select.select([master], [], [], 0.1)
                if ready:
                    chunk = os.read(master, 65536)
                    if not chunk:
                        return
                    out += chunk
            except OSError:
                return

    pump(1.5)
    for key in keys:
        os.write(master, key.encode())
        pump(0.8)

    deadline = time.time() + wait
    status = None
    while time.time() < deadline:
        pump(0.3)
        try:
            done, code = os.waitpid(proc, os.WNOHANG)
        except ChildProcessError:
            done, code = proc, 0
        if done:
            status = code
            break
    if status is None:
        # Still running: it did not close itself. Stop it.
        os.write(master, b"\x03")  # ctrl-c
        time.sleep(0.5)
        try:
            os.kill(proc, 9)
        except OSError:
            pass
        try:
            os.waitpid(proc, 0)
        except OSError:
            pass
        os.close(master)
        return None, out.decode("utf-8", "replace")
    os.close(master)
    return status, out.decode("utf-8", "replace")


def main():
    before = current()
    print("before:", before)
    code, output = run(["n", "W"])  # nav layer, then next workspace
    after = current()
    print("after: ", after)
    print("the overlay exited:", code is not None, "code", code)
    tail = [line for line in output.splitlines() if "EXIT=" in line or "START=" in line]
    for line in tail:
        print("  ", ANSI.sub("", line))

    # The check moves the user to another workspace, so it puts them back.
    if after[0] and after[0] != before[0]:
        socket_call("workspace.focus", {"workspace_id": before[0]})
        print("restored:", current())

    ok = code is not None and after[0] != before[0]
    if not ok:
        print("FAIL: the overlay did not close itself after the workspace moved")
        return 1
    print("PASS: the overlay closed itself when the workspace moved")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
