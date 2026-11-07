"""A minimal client for Herdr's newline-delimited JSON socket API.

Why a raw socket and not the CLI: the overlay executes an action on every
keystroke, and spawning a `herdr` process per keystroke is both slower and
harder to reason about. The protocol is one JSON object per line, and the
response echoes the request `id`, so a request/response client is small.

Transport is a Unix domain socket. On Windows Herdr uses a named pipe, which
this module does not speak; v2 is therefore linux/macos only, the same as v1.
A future `WindowsPipeTransport` would slot in beside `SocketTransport` without
touching any caller.

Only the two things the overlay needs are here: a request/response call, and
enough error handling that a failure shows up in the overlay instead of
crashing it.
"""

import json
import os
import socket


class HerdrError(Exception):
    """A request reached Herdr and was rejected."""

    def __init__(self, code, message):
        super().__init__("%s: %s" % (code, message))
        self.code = code
        self.message = message


class HerdrUnavailable(Exception):
    """Herdr could not be reached at all (no socket, no server)."""


class HerdrClient:
    """A lazily-connected, long-lived socket client."""

    def __init__(self, socket_path=None, timeout=6.0):
        self.socket_path = socket_path or os.environ.get("HERDR_SOCKET_PATH")
        self.timeout = timeout
        self._sock = None
        self._next_id = 0

    # -- connection ---------------------------------------------------------

    def _connect(self):
        if self._sock is not None:
            return self._sock
        if not self.socket_path:
            raise HerdrUnavailable("HERDR_SOCKET_PATH is not set")
        if not os.path.exists(self.socket_path):
            raise HerdrUnavailable("no Herdr socket at %s" % self.socket_path)
        try:
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            sock.settimeout(self.timeout)
            sock.connect(self.socket_path)
        except OSError as exc:
            raise HerdrUnavailable("cannot connect to %s (%s)" % (self.socket_path, exc))
        self._sock = sock
        return sock

    def close(self):
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None

    def _readline(self):
        chunks = []
        while True:
            try:
                data = self._sock.recv(65536)
            except socket.timeout:
                raise HerdrUnavailable("timed out waiting for Herdr")
            except OSError as exc:
                raise HerdrUnavailable("socket error (%s)" % exc)
            if not data:
                raise HerdrUnavailable("Herdr closed the connection")
            chunks.append(data)
            if b"\n" in data:
                break
        blob = b"".join(chunks)
        line, _, _ = blob.partition(b"\n")
        return line.decode("utf-8", "replace")

    # -- requests -----------------------------------------------------------

    def call(self, method, params=None):
        """Send one request and return its `result` payload.

        Raises HerdrError when Herdr rejects the request and HerdrUnavailable
        when it cannot be reached.

        A long-lived connection can be dropped by the server while the overlay
        sits idle in front of the user, and the next write then fails with
        EPIPE. That is a stale connection, not a dead server, so the call is
        retried once on a fresh connection.
        """
        last_error = None
        for attempt in (1, 2):
            try:
                return self._call_once(method, params)
            except HerdrUnavailable as exc:
                last_error = exc
                self.close()
                if attempt == 2:
                    raise
        raise last_error

    def _call_once(self, method, params=None):
        sock = self._connect()
        self._next_id += 1
        request_id = "v2-%d" % self._next_id
        payload = {"id": request_id, "method": method, "params": params or {}}
        try:
            sock.sendall((json.dumps(payload) + "\n").encode("utf-8"))
        except OSError as exc:
            self.close()
            raise HerdrUnavailable("cannot send to Herdr (%s)" % exc)

        # Skip any lines that are not our response. We do not subscribe to
        # events, so in practice this loop runs once.
        while True:
            line = self._readline()
            if not line.strip():
                continue
            try:
                message = json.loads(line)
            except ValueError:
                continue
            if message.get("id") != request_id:
                continue
            if "error" in message:
                error = message["error"] or {}
                raise HerdrError(error.get("code", "unknown"), error.get("message", ""))
            return message.get("result", {})
