#!/usr/bin/env python3
"""Check the action map against the LIVE Herdr, and report.

Three questions, three answers:

1. Does every action's method exist in the running server's schema, and do
   the parameters it would send satisfy that schema?  Every action is
   checked: the method exists, the parameters are ones the schema defines,
   the types are right and the required fields are there.
2. Do the safe actions -- the ones that only move the focus or read the
   state -- actually work?  They are executed against the live server and
   the response has to come back without an error.
3. Are the hint rows really unreachable?  The schema is searched for a
   method for each of them, and what it finds is reported.

**The probe does not change the user's session.**  It does not close a pane,
a tab or a space; it does not create one, split a pane or start an agent.
Everything destructive is checked against the schema and left alone.  The
focus moves the safe actions make are put back before the probe ends.

    python3 scripts/api_check.py

Exits non-zero when anything is wrong.
"""

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

from managr import actions as actions_mod  # noqa: E402
from managr import apicheck  # noqa: E402
from managr.client import HerdrClient, HerdrError, HerdrUnavailable  # noqa: E402


def socket_path():
    """Where the running server listens."""
    return os.environ.get("HERDR_SOCKET_PATH") or os.path.expanduser(
        "~/.config/herdr/herdr.sock"
    )


class RecordingClient:
    """The real client, with every call written down.

    The record says which methods the action actually reached the server
    with: an action that answered "no agents" never called `agent.focus`,
    and that method is then untested by this run.
    """

    def __init__(self, client):
        self.client = client
        self.calls = []

    def call(self, method, params=None):
        self.calls.append((method, params or {}))
        return self.client.call(method, params)


def current_pane(client):
    """The focused pane, as the server sees it."""
    return (client.call("pane.current").get("pane") or {})


def run_safe(recorder, context):
    """Run every safe action against the live server.

    Returns {action name: (status, detail)}. `status` is "ok", "skip" or
    "fail".
    """
    results = {}
    for name, action in actions_mod.ACTIONS.items():
        if not action.safe:
            continue
        recorder.calls = []
        # The actions that ask for a value are given the first one: the
        # probe is checking the call, not the user's choice.
        value = "1" if action.needs_input else None
        try:
            outcome = actions_mod.EXECUTORS[name](context, value)
        except HerdrError as exc:
            results[name] = ("fail", "herdr refused it: %s" % exc.message)
            continue
        except HerdrUnavailable as exc:
            results[name] = ("fail", "herdr unreachable: %s" % exc)
            continue
        except Exception as exc:  # noqa: BLE001 - a probe reports, it does not crash
            results[name] = ("fail", "it raised %s: %s" % (exc.__class__.__name__, exc))
            continue
        called = [method for method, _ in recorder.calls]
        missing = [call.method for call in action.calls if call.method not in called]
        if missing:
            # The action answered before it called: there are no agents, or
            # no tabs. The method was not exercised, which is a fact about
            # the session, not a bug.
            results[name] = ("skip", "not exercised (%s)" % outcome.message)
            continue
        results[name] = ("ok", outcome.message)
    return results


def restore_focus(client, before):
    """Put the focus back where the probe found it."""
    try:
        current = current_pane(client)
    except (HerdrError, HerdrUnavailable) as exc:
        return "the focus could not be read back (%s)" % exc
    if current.get("pane_id") == before.get("pane_id"):
        return "the focus never moved"
    for method, key in (
        ("workspace.focus", "workspace_id"),
        ("tab.focus", "tab_id"),
        ("pane.focus", "pane_id"),
    ):
        if not before.get(key):
            continue
        try:
            client.call(method, {key: before[key]})
        except (HerdrError, HerdrUnavailable):
            pass
    try:
        after = current_pane(client)
    except (HerdrError, HerdrUnavailable):
        return "the focus was moved back, but it could not be confirmed"
    if after.get("pane_id") == before.get("pane_id"):
        return "the focus was put back"
    return "the focus did NOT go back (it is on %s now)" % after.get("pane_id")


def main():
    print("managr: checking the action map against the live herdr")

    try:
        schema = apicheck.Schema(apicheck.live_schema())
    except RuntimeError as exc:
        print("FAIL: %s" % exc)
        print(
            "  (the recorded snapshot in src/managr/data/api-schema.json is "
            "what the tests check against)"
        )
        return 1
    print("schema: %d methods" % len(schema.methods))

    try:
        recorded = apicheck.Schema(apicheck.recorded_schema())
    except (OSError, ValueError) as exc:
        print("  the recorded snapshot could not be read (%s)" % exc)
        recorded = None
    if recorded is not None:
        added = sorted(set(schema.methods) - set(recorded.methods))
        gone = sorted(set(recorded.methods) - set(schema.methods))
        if added:
            print(
                "  the live schema has %d methods the recorded snapshot does not: %s"
                % (len(added), ", ".join(added))
            )
        if gone:
            print(
                "  the recorded snapshot has %d methods the live schema does not: %s"
                % (len(gone), ", ".join(gone))
            )
        if not added and not gone:
            print("  the live schema matches the recorded snapshot")

    # -- the live server ----------------------------------------------------

    recorder = RecordingClient(HerdrClient(socket_path()))
    context = actions_mod.Context(recorder)
    before = None
    try:
        context.refresh()
        before = current_pane(recorder.client)
    except (HerdrError, HerdrUnavailable) as exc:
        print("SKIP: the live server is not reachable (%s)" % exc)

    safe_results = {}
    if before is not None:
        safe_results = run_safe(recorder, context)

    # -- every action against the schema ------------------------------------

    print("")
    print("  %-22s %-24s %-24s %s" % ("action", "method", "how", "result"))
    problems = []
    executed = 0
    schema_only = 0
    public_count = 0
    for name in sorted(actions_mod.ACTIONS):
        action = actions_mod.ACTIONS[name]
        if name in actions_mod.INTERNAL_ACTIONS:
            # Not a Herdr action: a local process, or a plugin invocation.
            # It has no socket call to check, so it is listed and left out
            # of the count.
            issues = []
            status, detail = "ok", "not a Herdr action; it runs a local process"
            how = "(local)"
            print("  %-22s %-24s %-24s %s %s" % (name, action.method, how, status, detail))
            continue
        public_count += 1
        issues = apicheck.check_action(action, schema)
        if action.safe:
            how = "executed"
            if before is not None:
                status, detail = safe_results.get(name, ("fail", "it did not run"))
                executed += 1
            else:
                status, detail = "skip", "not run: no live server"
        elif action.destructive:
            how = "schema-only (destructive)"
            status, detail = "ok", "not run: it changes the session"
            schema_only += 1
        else:
            how = "schema-only"
            status, detail = "ok", "not run: it is not safe on a live session"
            schema_only += 1
        if issues:
            status, detail = "fail", "; ".join(issues)
        if status == "fail":
            problems.append("%s: %s" % (name, detail))
        print("  %-22s %-24s %-24s %s %s" % (name, action.method, how, status, detail))

    # -- the calls that are not actions -------------------------------------
    #
    # The custom layer fetches its rows with these: the actions of every
    # installed plugin, and the plugins themselves. A method Herdr does not
    # have is a method Herdr does not have, whatever it is called from.

    print("")
    print("  %-22s %-24s %-24s %s" % ("list call", "method", "how", "result"))
    for name in sorted(actions_mod.EXTRA_CALLS):
        call = actions_mod.EXTRA_CALLS[name]
        issues = apicheck.check_call(call, schema)
        status = "fail" if issues else "ok"
        detail = "; ".join(issues) if issues else "the schema has it"
        if issues:
            problems.append("%s: %s" % (name, detail))
        print("  %-22s %-24s %-24s %s %s" % (name, call.method, "schema-only", status, detail))

    # -- the hint rows ------------------------------------------------------

    print("")
    print("  %-24s %-8s %s" % ("unreachable action", "claim", "what the schema has"))
    for name in sorted(actions_mod.UNREACHABLE):
        entry = actions_mod.UNREACHABLE[name]
        issues, matches = apicheck.check_unreachable(schema, {name: entry})[name]
        if issues:
            problems.extend("%s: %s" % (name, issue) for issue in issues)
        if matches:
            found = ", ".join(
                "%s (%s)" % (method, reason) if reason else method
                for method, reason in sorted(matches.items())
            )
        else:
            found = "nothing"
        print("  %-24s %-8s %s" % (name, "WRONG" if issues else "ok", found))

    # -- put the focus back -------------------------------------------------

    print("")
    if before is not None:
        print("  the focus: %s" % restore_focus(recorder.client, before))

    print("")
    # The internal runners are not Herdr actions: they run a plugin's
    # action, a shell command and a window command. They are in the map,
    # but they are not actions of Herdr's, and counting them as one says
    # the overlay can execute forty Herdr actions when it can execute
    # thirty-seven.
    print(
        "%d actions: %d executed against the live server, %d schema-checked only"
        % (public_count, executed, schema_only)
    )
    print(
        "%d internal runners (not Herdr actions)"
        % len(actions_mod.INTERNAL_ACTIONS)
    )
    print(
        "%d hint rows checked against the schema"
        % len(actions_mod.UNREACHABLE)
    )
    print(
        "%d list calls checked against the schema"
        % len(actions_mod.EXTRA_CALLS)
    )

    if problems:
        print("")
        for problem in problems:
            print("FAIL: %s" % problem)
        print("FAIL")
        return 1
    print("PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
