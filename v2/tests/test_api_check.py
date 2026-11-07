"""The action map, checked against a recorded copy of Herdr's 0.9.5 schema.

The socket calls in `actions.py` were written by reading the documentation.
Nothing checked them against a running server, and Herdr does not forbid
extra fields in a request -- so a call with the wrong parameter name is
*accepted and ignored*: the action does nothing and says it worked.

These tests run the schema half of `scripts/api_check.py` against the
recorded snapshot in `src/managr/data/api-schema.json`, so they need
neither a server nor the network.  The live check is the script's job; this
is the one that runs every time.

Run: python3 tests/test_api_check.py
"""

import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

from managr import actions as actions_mod  # noqa: E402
from managr import apicheck  # noqa: E402

# The methods that only move the focus or read the state.  An action that
# uses one of these is safe to call on a live session; anything else is not.
SAFE_METHODS = {
    "pane.focus_direction",
    "tab.focus",
    "workspace.focus",
    "agent.focus",
    "tab.list",
    "workspace.list",
    "agent.list",
}

# What each action says and whether it keeps the overlay open.  The map is
# data now, and the data has to keep saying the same thing: the refactor
# that made it data was not allowed to change a single message.
EXPECTED_OUTCOMES = {
    "split_vertical": ("split right", False),
    "split_horizontal": ("split down", False),
    "close_pane": ("pane closed", False),
    "focus_pane_left": ("focus left", False),
    "focus_pane_down": ("focus down", False),
    "focus_pane_up": ("focus up", False),
    "focus_pane_right": ("focus right", False),
    "swap_pane_left": ("swap left", False),
    "swap_pane_down": ("swap down", False),
    "swap_pane_up": ("swap up", False),
    "swap_pane_right": ("swap right", False),
    "resize_pane_left": ("resize left", False),
    "resize_pane_down": ("resize down", False),
    "resize_pane_up": ("resize up", False),
    "resize_pane_right": ("resize right", False),
    "zoom": ("zoom toggled", False),
    "clear_pane": ("cleared", False),
    "edit_scrollback": ("scrollback in $EDITOR", False),
    "rename_pane": ("renamed to 'x'", False),
    "new_tab": ("tab created", False),
    "close_tab": ("tab closed", False),
    "rename_tab": ("renamed to 'x'", False),
    "next_tab": ("three", True),
    "previous_tab": ("one", True),
    "move_tab_previous": ("moved", True),
    "move_tab_next": ("moved", True),
    "new_workspace": ("workspace created", False),
    "close_workspace": ("workspace closed", False),
    "rename_workspace": ("renamed to 'x'", False),
    "new_worktree": ("worktree 'x'", False),
    "previous_workspace": ("space", True),
    "next_workspace": ("space", True),
    "reload_config": ("config reloaded", False),
    "notify": ("notified", False),
    "focus_agent": ("a1", True),
    "switch_tab": ("one", True),
    "switch_workspace": ("space", True),
}

EXPECTED_PROMPTS = {
    "rename_pane": "pane name",
    "rename_tab": "tab name",
    "rename_workspace": "space name",
    "new_worktree": "branch name",
    "notify": "message",
    "focus_agent": "agent number",
}


class FakeClient:
    """A client that answers the list calls and records everything."""

    def __init__(self):
        self.calls = []

    def call(self, method, params=None):
        self.calls.append((method, params or {}))
        if method == "tab.list":
            return {
                "tabs": [
                    {"tab_id": "w1:t1", "label": "one"},
                    {"tab_id": "w1:t2", "label": "two"},
                    {"tab_id": "w1:t3", "label": "three"},
                ]
            }
        if method == "workspace.list":
            return {"workspaces": [{"workspace_id": "w1", "label": "space"}]}
        if method == "agent.list":
            return {"agents": [{"agent": "a1", "pane_id": "w1:p1"}]}
        return {}


class TestSchemaCheck(unittest.TestCase):
    """The map, against the recorded 0.9.5 schema."""

    @classmethod
    def setUpClass(cls):
        cls.schema = apicheck.Schema(apicheck.recorded_schema())

    def test_the_recorded_schema_is_the_0_9_5_snapshot(self):
        self.assertEqual(len(self.schema.methods), 102)
        for method in (
            "pane.split",
            "pane.focus_direction",
            "pane.close",
            "tab.focus",
            "workspace.focus",
            "agent.focus",
            "notification.show",
            "pane.copy_motion",
        ):
            self.assertIn(method, self.schema, method)

    def test_every_action_method_is_in_the_schema(self):
        for name, action in actions_mod.ACTIONS.items():
            for call in action.calls:
                self.assertIn(call.method, self.schema, "%s: %s" % (name, call.method))

    def test_every_action_parameter_satisfies_the_schema(self):
        for name, action in actions_mod.ACTIONS.items():
            problems = apicheck.check_action(action, self.schema)
            self.assertEqual(problems, [], "%s: %s" % (name, "; ".join(problems)))

    def test_a_method_that_does_not_exist_is_caught(self):
        broken = actions_mod.Action(
            "broken", [actions_mod.Call("pane.splitt", {"direction": "right"})]
        )
        problems = apicheck.check_action(broken, self.schema)
        self.assertTrue(problems)
        self.assertIn("pane.splitt", problems[0])

    def test_a_parameter_the_schema_does_not_define_is_caught(self):
        broken = actions_mod.Action(
            "broken", [actions_mod.Call("pane.close", {"pane": "$pane_id"})]
        )
        problems = apicheck.check_action(broken, self.schema)
        self.assertTrue(problems)
        self.assertIn("pane", problems[0])

    def test_a_missing_required_parameter_is_caught(self):
        broken = actions_mod.Action("broken", [actions_mod.Call("pane.close", {})])
        problems = apicheck.check_action(broken, self.schema)
        self.assertTrue(problems)
        self.assertIn("pane_id", problems[0])

    def test_a_parameter_of_the_wrong_type_is_caught(self):
        broken = actions_mod.Action(
            "broken", [actions_mod.Call("pane.focus_direction", {"direction": 7})]
        )
        problems = apicheck.check_action(broken, self.schema)
        self.assertTrue(problems)

    def test_a_value_outside_the_enum_is_caught(self):
        broken = actions_mod.Action(
            "broken", [actions_mod.Call("pane.focus_direction", {"direction": "sideways"})]
        )
        problems = apicheck.check_action(broken, self.schema)
        self.assertTrue(problems)

    def test_the_check_catches_every_action_at_once(self):
        results = apicheck.check_actions(self.schema)
        self.assertEqual(len(results), len(actions_mod.ACTIONS))
        broken = {
            name: problems
            for name, problems in results.items()
            if problems
        }
        self.assertEqual(broken, {})


class TestUnreachable(unittest.TestCase):
    """The hint rows: the claim that Herdr has no method for them."""

    @classmethod
    def setUpClass(cls):
        cls.schema = apicheck.Schema(apicheck.recorded_schema())

    def test_every_unreachable_action_declares_what_to_search_for(self):
        for name, entry in actions_mod.UNREACHABLE.items():
            self.assertTrue(entry.search, "%s declares no search" % name)

    def test_the_unreachable_actions_are_still_unreachable(self):
        results = apicheck.check_unreachable(self.schema)
        for name, (problems, matches) in results.items():
            self.assertEqual(problems, [], "%s: %s" % (name, "; ".join(problems)))

    def test_every_known_method_is_in_the_schema(self):
        # A known method that is not in the schema would make the search
        # vacuous: the claim would look confirmed because the method that
        # matched it is gone.
        for name, entry in actions_mod.UNREACHABLE.items():
            for method in entry.known:
                self.assertIn(method, self.schema, "%s: %s" % (name, method))

    def test_a_new_method_for_an_unreachable_action_is_caught(self):
        raw = apicheck.recorded_schema()
        raw["schemas"]["request"]["oneOf"].append(
            {
                "properties": {
                    "method": {"const": "client.detach", "type": "string"},
                    "params": {"$ref": "#/schemas/request/$defs/EmptyParams"},
                },
                "required": ["method", "params"],
                "type": "object",
            }
        )
        schema = apicheck.Schema(raw)
        problems, matches = apicheck.check_unreachable(
            schema, {"detach": actions_mod.UNREACHABLE["detach"]}
        )["detach"]
        self.assertTrue(problems)
        self.assertIn("client.detach", matches)

    def test_the_copy_primitives_are_not_a_way_into_copy_mode(self):
        # 0.9.5 has the copy primitives. They move a cursor and read a
        # selection; none of them enters the mode, so the hint row stays.
        entry = actions_mod.UNREACHABLE["copy_mode"]
        self.assertIn("pane.copy_motion", entry.known)
        self.assertIn("pane.copy_search", entry.known)
        self.assertIn("pane.selection.read", entry.known)


class TestSafeAndDestructive(unittest.TestCase):
    """What the probe may run, and what it must not."""

    def test_a_destructive_action_is_not_safe(self):
        for name, action in actions_mod.ACTIONS.items():
            if action.destructive:
                self.assertFalse(action.safe, name)

    def test_a_safe_action_only_moves_the_focus_or_reads_the_state(self):
        for name, action in actions_mod.ACTIONS.items():
            if not action.safe:
                continue
            for call in action.calls:
                self.assertIn(call.method, SAFE_METHODS, "%s: %s" % (name, call.method))

    def test_the_actions_that_create_or_close_are_destructive(self):
        for name in (
            "split_vertical",
            "split_horizontal",
            "close_pane",
            "clear_pane",
            "edit_scrollback",
            "new_tab",
            "close_tab",
            "new_workspace",
            "close_workspace",
            "new_worktree",
        ):
            self.assertTrue(actions_mod.ACTIONS[name].destructive, name)

    def test_every_destructive_action_is_left_to_the_schema(self):
        for name, action in actions_mod.ACTIONS.items():
            if action.destructive:
                self.assertFalse(action.safe, name)


class TestTheMapIsTheSourceOfTruth(unittest.TestCase):
    """The executor and the declaration have to agree.

    The map is data so that a tool can ask what an action would do without
    doing it. That is only worth anything if the executor does what the
    declaration says -- so the executor's calls are compared with the
    declaration's, one action at a time.
    """

    # The actions that take a number: the switchers and the agent picker.
    NUMERIC = {"switch_tab", "switch_workspace", "focus_agent"}

    def _run(self, name):
        action = actions_mod.ACTIONS[name]
        fake = FakeClient()
        ctx = actions_mod.Context(
            fake, workspace_id="w1", tab_id="w1:t2", pane_id="w1:p1"
        )
        value = "1" if name in self.NUMERIC else "x"
        outcome = actions_mod.EXECUTORS[name](ctx, value)
        return action, fake.calls, outcome, value

    def test_the_executor_calls_what_the_map_declares(self):
        for name in actions_mod.ACTIONS:
            action, calls, outcome, value = self._run(name)
            self.assertEqual(
                [method for method, _ in calls],
                [call.method for call in action.calls],
                name,
            )
            for (method, params), call in zip(calls, action.calls):
                self.assertEqual(
                    sorted(params), sorted(call.params), "%s: %s" % (name, method)
                )
                for key, spec in call.params.items():
                    if spec == "$pane_id":
                        self.assertEqual(params[key], "w1:p1", name)
                    elif spec == "$tab_id":
                        self.assertEqual(params[key], "w1:t2", name)
                    elif spec == "$workspace_id":
                        self.assertEqual(params[key], "w1", name)
                    elif spec == "$value":
                        self.assertEqual(params[key], value, name)
                    elif spec == "$title":
                        self.assertEqual(params[key], value or "managr", name)
                    elif not (isinstance(spec, str) and spec.startswith("$")):
                        self.assertEqual(params[key], spec, "%s: %s.%s" % (name, method, key))

    def test_every_action_says_what_it_used_to_say(self):
        for name, (message, keep_open) in EXPECTED_OUTCOMES.items():
            action, calls, outcome, value = self._run(name)
            self.assertEqual(outcome.message, message, name)
            self.assertEqual(outcome.keep_open, keep_open, name)

    def test_the_prompts_are_the_same(self):
        self.assertEqual(actions_mod.NEEDS_INPUT, EXPECTED_PROMPTS)

    def test_every_action_that_needs_input_has_one(self):
        for name in actions_mod.NEEDS_INPUT:
            self.assertIn(name, actions_mod.ACTIONS, name)
            self.assertTrue(actions_mod.ACTIONS[name].needs_input, name)


class TestTheCustomLayer(unittest.TestCase):
    """The custom layer's calls, against the recorded 0.9.5 schema.

    The custom layer runs the plugins' actions and lists the user's own
    commands. The socket calls are in the map like every other one, so the
    check covers them too.
    """

    @classmethod
    def setUpClass(cls):
        cls.schema = apicheck.Schema(apicheck.recorded_schema())

    def test_the_plugin_action_call_is_in_the_schema(self):
        action = actions_mod.ACTIONS["invoke_plugin_action"]
        self.assertEqual(apicheck.check_action(action, self.schema), [])

    def test_the_plugin_action_call_sends_the_ids(self):
        call = actions_mod.ACTIONS["invoke_plugin_action"].calls[0]
        self.assertEqual(call.method, "plugin.action.invoke")
        self.assertEqual(sorted(call.params), ["action_id", "plugin_id"])

    def test_the_list_calls_are_in_the_schema(self):
        # The names are named, so the test cannot pass on an empty map:
        # a guard whose sample cannot fail is not a guard.
        for name in ("list_plugin_actions", "list_plugins"):
            self.assertIn(name, actions_mod.EXTRA_CALLS, name)
            call = actions_mod.EXTRA_CALLS[name]
            self.assertEqual(apicheck.check_call(call, self.schema), [], name)

    def test_the_rearranging_calls_are_in_the_schema(self):
        # The navigatr's moves are not actions -- the destination is picked
        # in the tree and not in a layer -- but they are socket calls, and a
        # method Herdr does not have is a method Herdr does not have.
        for name in ("move_pane", "move_tab"):
            self.assertIn(name, actions_mod.EXTRA_CALLS, name)
            call = actions_mod.EXTRA_CALLS[name]
            self.assertEqual(apicheck.check_call(call, self.schema), [], name)

    def test_the_pane_move_says_where_the_pane_goes(self):
        # `pane.move` needs to know the tab it lands in, and Herdr needs to
        # know which way the pane is split into it.
        call = actions_mod.EXTRA_CALLS["move_pane"]
        self.assertEqual(call.method, "pane.move")
        destination = call.params["destination"]
        self.assertEqual(destination["type"], "tab")
        self.assertIn("tab_id", destination)
        self.assertIn("split", destination)

    def test_the_tab_move_says_where_the_tab_goes(self):
        call = actions_mod.EXTRA_CALLS["move_tab"]
        self.assertEqual(call.method, "tab.move")
        self.assertIn("tab_id", call.params)
        self.assertIn("insert_index", call.params)

    def test_the_local_actions_make_no_socket_call(self):
        # A shell command is a local process, and a popup or a pane command
        # cannot be run from inside the overlay at all. Neither pretends to
        # have a method it does not have.
        for name in ("run_shell_command", "run_window_command"):
            self.assertEqual(actions_mod.ACTIONS[name].calls, [], name)

    def test_the_custom_actions_are_in_the_map(self):
        for name in ("invoke_plugin_action", "run_shell_command", "run_window_command"):
            self.assertIn(name, actions_mod.EXECUTORS, name)


if __name__ == "__main__":
    # Any other test file named on the command line is run too, so
    # `python3 tests/test_keymap.py tests/test_configure.py` runs both
    # rather than ignoring the second one.
    _loader = unittest.TestLoader()
    _suite = unittest.TestSuite()
    _suite.addTests(_loader.loadTestsFromModule(sys.modules["__main__"]))
    for _name in [
        os.path.splitext(os.path.basename(a))[0]
        for a in sys.argv[1:]
        if a.endswith(".py")
    ]:
        _suite.addTests(_loader.loadTestsFromNames([_name]))
    _result = unittest.TextTestRunner(verbosity=2).run(_suite)
    sys.exit(0 if _result.wasSuccessful() else 1)
