"""Check the action map against Herdr's API schema.

The action map was written by reading the documentation. Nothing ever checked
it against a running server, and Herdr's request objects do not forbid extra
fields -- so a call with the wrong parameter name is *accepted and ignored*.
`pane.close` with `pane` instead of `pane_id` is not refused: it closes
nothing, and the overlay says "pane closed" all the same.

This module is the reusable half. It takes a schema and the action map and
says what does not line up:

* every method the map uses has to exist in the schema;
* every parameter it sends has to be one the schema defines, of the right
  type, and every required parameter has to be there;
* every action the plugin claims is unreachable has to stay unreachable.

`scripts/api_check.py` runs it against the live server and executes the safe
actions. `tests/test_api_check.py` runs it against the recorded 0.9.5 schema,
so the check works with no server and no network.

The recorded schema is a snapshot of `herdr api schema --json` from herdr
0.9.5. The live schema is the truth, but a check that needs a server only
runs when somebody remembers to run it.
"""

import json
import os
import subprocess

from . import actions as actions_mod

# The recorded schema: `herdr api schema --json` from herdr 0.9.5.
SCHEMA_FILE = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "data", "api-schema.json"
)


def recorded_schema(path=None):
    """The recorded 0.9.5 schema."""
    with open(path or SCHEMA_FILE, "r", encoding="utf-8") as handle:
        return json.load(handle)


def live_schema(herdr="herdr"):
    """The schema of the installed herdr, as `herdr api schema --json` prints it."""
    try:
        proc = subprocess.run(
            [herdr, "api", "schema", "--json"], capture_output=True, text=True
        )
    except OSError as exc:
        raise RuntimeError("`%s` could not be run (%s)" % (herdr, exc))
    if proc.returncode != 0:
        raise RuntimeError(
            "`%s api schema --json` failed: %s" % (herdr, (proc.stderr or "").strip())
        )
    try:
        return json.loads(proc.stdout)
    except ValueError as exc:
        raise RuntimeError("the schema is not JSON: %s" % exc)


class Schema:
    """Herdr's request schema, as a table of methods and their parameters."""

    def __init__(self, raw):
        self.raw = raw
        request = (raw.get("schemas") or {}).get("request") or {}
        self.defs = request.get("$defs") or {}
        self.methods = {}
        for entry in request.get("oneOf") or []:
            properties = entry.get("properties") or {}
            name = (properties.get("method") or {}).get("const")
            if not name:
                continue
            self.methods[name] = properties.get("params") or {}

    def __contains__(self, method):
        return method in self.methods

    def method_names(self):
        return sorted(self.methods)

    def params(self, method):
        """(properties, required) of a method's parameters."""
        fragment = self._resolve(self.methods.get(method) or {})
        return fragment.get("properties") or {}, list(fragment.get("required") or [])

    def _resolve(self, fragment):
        """Follow `$ref`s into the schema's own definitions."""
        seen = 0
        while isinstance(fragment, dict) and "$ref" in fragment:
            name = str(fragment["$ref"]).rsplit("/", 1)[-1]
            fragment = self.defs.get(name) or {}
            seen += 1
            if seen > 20:
                break
        return fragment

    def types(self, fragment):
        """(types, values) a parameter schema accepts.

        `types` are JSON type names. `values` is the set of allowed values
        when the schema is an enum -- the direction of a focus move, say.
        """
        fragment = self._resolve(fragment)
        types = set()
        values = set()
        kind = fragment.get("type")
        if isinstance(kind, list):
            types.update(kind)
        elif isinstance(kind, str):
            types.add(kind)
        for item in fragment.get("enum") or []:
            values.add(item)
        if "const" in fragment:
            values.add(fragment["const"])
        for branch in fragment.get("anyOf") or []:
            branch_types, branch_values = self.types(branch)
            types.update(branch_types)
            values.update(branch_values)
        if not types and "properties" in fragment:
            types.add("object")
        return types, values


def value_type(value):
    """The JSON type of a declared parameter value."""
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, str):
        return "string"
    if value is None:
        return "null"
    if isinstance(value, list):
        return "array"
    return "object"


def declared_type(spec):
    """The JSON type a declared parameter will have when it is sent.

    A literal has its own type; a reference to the context has the type the
    reference produces.
    """
    if isinstance(spec, str) and spec.startswith("$"):
        known = actions_mod.CONTEXT_VALUES.get(spec)
        return known[0] if known else "unknown"
    return value_type(spec)


def check_call(call, schema):
    """What is wrong with one call, as a list of sentences."""
    problems = []
    if call.method not in schema:
        problems.append("there is no method %r in the schema" % call.method)
        return problems

    properties, required = schema.params(call.method)
    for name in required:
        if name not in call.params:
            problems.append(
                "%s needs %r, and the call does not send it" % (call.method, name)
            )
    for name, spec in call.params.items():
        if name not in properties:
            # Herdr does not forbid extra fields, so this is accepted and
            # ignored: the action would do nothing and say it worked.
            problems.append("%s has no parameter %r" % (call.method, name))
            continue
        types, values = schema.types(properties[name])
        kind = declared_type(spec)
        if kind == "unknown":
            problems.append("%r is not a known context value" % spec)
            continue
        if types and kind not in types and not (kind == "integer" and "number" in types):
            problems.append(
                "%s wants %r to be %s, the call sends %s"
                % (call.method, name, "/".join(sorted(types)), kind)
            )
            continue
        if values and isinstance(spec, str) and spec not in values:
            problems.append(
                "%s wants %r to be one of %s"
                % (call.method, name, ", ".join(sorted(values)))
            )
    return problems


def check_action(action, schema):
    """What is wrong with one action, as a list of sentences."""
    problems = []
    for call in action.calls:
        problems.extend(check_call(call, schema))
    return problems


def check_actions(schema, actions=None):
    """{action name: problems} for the whole map."""
    actions = actions if actions is not None else actions_mod.ACTIONS
    return {name: check_action(action, schema) for name, action in actions.items()}


def check_unreachable(schema, unreachable=None):
    """{action name: (problems, matches)} for the hint rows.

    `matches` is every method whose name holds one of the action's search
    fragments -- the noun of the action. A match that is not in the action's
    `known` list means Herdr has a method for it now, so the hint row is
    wrong and has to become an executor.
    """
    unreachable = (
        unreachable if unreachable is not None else actions_mod.UNREACHABLE
    )
    out = {}
    for name, entry in unreachable.items():
        matches = {}
        for method in schema.method_names():
            for fragment in entry.search:
                if fragment in method:
                    matches[method] = entry.known.get(method, "")
                    break
        problems = []
        for method, reason in sorted(matches.items()):
            if not reason:
                problems.append(
                    "the schema has %r, so this action is reachable" % method
                )
        out[name] = (problems, matches)
    return out
