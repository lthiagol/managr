"""A deliberately small, forgiving TOML reader.

Herdr plugin commands are plain `python3` with no build step, and macOS still
ships 3.9, which has no `tomllib`. Rather than vendor a full parser or demand a
newer interpreter, we read the narrow slice of TOML that Herdr's `config.toml`
and this plugin's own `bar.toml` actually use: tables, arrays of tables, scalars
and flat arrays of scalars.

The parser is intentionally lenient. A user's `config.toml` is theirs to edit
and may hold anything; a malformed line degrades that one key rather than
raising, because a status line must never take the terminal down with it.
"""

import re

_TABLE_RE = re.compile(r"^\[([^\[\]]+)\]\s*$")
_ARRAY_TABLE_RE = re.compile(r"^\[\[([^\[\]]+)\]\]\s*$")
_ANY_TABLE_RE = re.compile(r"^\[+\[?[^\[\]]+\]?\]?\s*$")


def check_strict(text):
    """Return a list of structural problems; empty means it looks like TOML.

    `tomlite` is deliberately lenient, because it reads a user's config that may
    hold anything. But the config mutators use a parse as a SAFETY NET before
    rewriting a file the user cares about, and leniency turns that net into a
    no-op: on Python 3.9 there is no `tomllib`, so a malformed table header
    such as "[ui" parses fine and the file gets rewritten anyway.

    This is not a TOML parser. It only has to catch the realistic ways a config
    can be broken in a way that would make a rewrite unsafe.
    """
    problems = []
    in_array = False

    for number, raw in enumerate(text.splitlines(), 1):
        line = strip_comment(raw).strip()

        if in_array:
            # Inside a multi-line array; wait for it to close.
            if line.count("[") <= line.count("]"):
                in_array = False
            continue

        if not line:
            continue

        if line.startswith("["):
            if not _ANY_TABLE_RE.match(line):
                problems.append("line %d: malformed table header %r" % (number, line))
            continue

        if "=" not in line:
            problems.append("line %d: not a key/value pair: %r" % (number, line))
            continue

        key, _, value = line.partition("=")
        if not key.strip().strip("\"'"):
            problems.append("line %d: empty key" % number)
            continue

        stripped = value.strip()
        if stripped.startswith(('"', "'")):
            quote = stripped[0]
            if len(stripped) < 2 or not stripped.endswith(quote):
                problems.append("line %d: unterminated string" % number)
        elif stripped.startswith("[") and stripped.count("[") > stripped.count("]"):
            in_array = True

    return problems


class TomlError(ValueError):
    """Raised only for input that is not a string at all."""


def strip_comment(line):
    """Drop a trailing `#` comment without eating `#` inside a string."""
    out = []
    in_str = False
    quote = ""
    i = 0
    while i < len(line):
        ch = line[i]
        if in_str:
            if ch == "\\" and i + 1 < len(line):
                out.append(line[i : i + 2])
                i += 2
                continue
            if ch == quote:
                in_str = False
            out.append(ch)
        elif ch in "\"'":
            in_str = True
            quote = ch
            out.append(ch)
        elif ch == "#":
            break
        else:
            out.append(ch)
        i += 1
    return "".join(out).strip()


def _split_top(text):
    """Split on commas that sit outside strings and brackets."""
    parts, buf = [], []
    depth = 0
    in_str = False
    quote = ""
    i = 0
    while i < len(text):
        ch = text[i]
        if in_str:
            buf.append(ch)
            if ch == "\\" and i + 1 < len(text):
                buf.append(text[i + 1])
                i += 2
                continue
            if ch == quote:
                in_str = False
        elif ch in "\"'":
            in_str = True
            quote = ch
            buf.append(ch)
        elif ch in "[{":
            depth += 1
            buf.append(ch)
        elif ch in "]}":
            depth -= 1
            buf.append(ch)
        elif ch == "," and depth == 0:
            parts.append("".join(buf))
            buf = []
        else:
            buf.append(ch)
        i += 1
    tail = "".join(buf).strip()
    if tail:
        parts.append(tail)
    return [p.strip() for p in parts if p.strip()]


def _unescape(text):
    out, i = [], 0
    while i < len(text):
        ch = text[i]
        if ch == "\\" and i + 1 < len(text):
            nxt = text[i + 1]
            out.append({"n": "\n", "t": "\t", "r": "\r", '"': '"', "\\": "\\"}.get(nxt, "\\" + nxt))
            i += 2
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def _scalar(raw):
    text = raw.strip()
    if text.startswith("[") and text.endswith("]"):
        inner = text[1:-1].strip()
        return [_scalar(p) for p in _split_top(inner)] if inner else []
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "\"'":
        return _unescape(text[1:-1])
    low = text.lower()
    if low == "true":
        return True
    if low == "false":
        return False
    if low in ("null", "~", ""):
        return None
    try:
        return int(text)
    except ValueError:
        pass
    try:
        return float(text)
    except ValueError:
        pass
    # Herdr writes bare named punctuation, e.g. `prefix = "-"`. Keep it as a word.
    return text


def _descend(root, path):
    """Walk `a.b.c` creating tables as needed; returns (container, leaf_name)."""
    parts = [p.strip() for p in path.split(".")]
    node = root
    for part in parts[:-1]:
        nxt = node.get(part)
        if isinstance(nxt, list):
            nxt = nxt[-1] if nxt else None
        if not isinstance(nxt, dict):
            nxt = {}
            node[part] = nxt
        node = nxt
    return node, parts[-1]


def loads(text):
    """Parse the supported TOML subset into plain dicts and lists."""
    if not isinstance(text, str):
        raise TomlError("expected a string")

    root = {}
    current = root
    pending_key = None
    pending_raw = ""

    for raw_line in text.splitlines():
        line = strip_comment(raw_line)

        if pending_key is not None:
            # Continuation of a multi-line array value.
            pending_raw += " " + line
            if pending_raw.count("[") <= pending_raw.count("]"):
                current[pending_key] = _scalar(pending_raw)
                pending_key, pending_raw = None, ""
            continue

        if not line:
            continue

        arr_table = _ARRAY_TABLE_RE.match(line)
        if arr_table:
            parent, leaf = _descend(root, arr_table.group(1))
            bucket = parent.setdefault(leaf, [])
            if not isinstance(bucket, list):
                bucket = []
                parent[leaf] = bucket
            entry = {}
            bucket.append(entry)
            current = entry
            continue

        table = _TABLE_RE.match(line)
        if table:
            parent, leaf = _descend(root, table.group(1))
            node = parent.get(leaf)
            if not isinstance(node, dict):
                node = {}
                parent[leaf] = node
            current = node
            continue

        if "=" not in line:
            continue

        key, _, value = line.partition("=")
        key = key.strip().strip("\"'")
        value = value.strip()

        # An array that opens here and closes on a later line.
        if value.startswith("[") and value.count("[") > value.count("]"):
            pending_key, pending_raw = key, value
            continue

        if not key:
            continue
        current[key] = _scalar(value)

    return root
