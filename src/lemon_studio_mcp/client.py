"""A JSON-RPC 2.0 client for Lemon Studio's control plane.

Lemon Studio listens on a local socket when started with `--control <socket>` (beside its window) or
`--serve <socket>` (no window). One request per line, one answer per line. See Lemon Studio's
docs/control-plane.md for the methods and commands.

Where it connects, from the environment:
    LEMON_SOCKET   a Unix socket path (default /tmp/lemon.sock), or
    LEMON_PORT     a local TCP port instead, with
    LEMON_TOKEN    the token Lemon Studio printed for that port.
"""

from __future__ import annotations

import itertools
import json
import os
import socket
import threading
from typing import Any

from mcp.server.mcpserver.exceptions import ToolError

DEFAULT_SOCKET = "/tmp/lemon.sock"
TIMEOUT = 15.0


class LemonError(ToolError):
    """An error answer from Lemon Studio (`code` is JSON-RPC's), or no connection (`code` None). A ToolError, so
    the MCP client sees its message (other exceptions reach it only as "Error executing tool")."""

    def __init__(self, message: str, code: int | None = None):
        super().__init__(message)
        self.code = code


def socket_path() -> str:
    return os.environ.get("LEMON_SOCKET", DEFAULT_SOCKET)


class LemonClient:
    def __init__(self, name: str = "Claude", path: str | None = None, port: int | None = None, token: str | None = None):
        self.name = name
        self.path = path or socket_path()
        env_port = os.environ.get("LEMON_PORT")
        self.port = port if port is not None else (int(env_port) if env_port else None)
        self.token = token if token is not None else os.environ.get("LEMON_TOKEN")
        self._ids = itertools.count(1)
        self._lock = threading.Lock()
        self._sock: socket.socket | None = None
        self._file = None
        self.hello: dict[str, Any] | None = None

    @property
    def where(self) -> str:
        return f"127.0.0.1:{self.port}" if self.port else self.path

    # ---- connection -------------------------------------------------------------------------------------------
    def _connect(self) -> None:
        try:
            if self.port:
                s = socket.create_connection(("127.0.0.1", self.port), timeout=TIMEOUT)
            else:
                s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                s.settimeout(TIMEOUT)
                s.connect(self.path)
        except OSError as e:
            raise LemonError(
                f"Lemon Studio is not listening at {self.where} ({e.strerror or e}). Start it with "
                f"`lemonstudio --control {self.path}` or use the lemon_launch tool."
            ) from e
        self._sock, self._file = s, s.makefile("rw", encoding="utf-8", newline="\n")
        params: dict[str, Any] = {"name": self.name}
        if self.token:
            params["token"] = self.token
        self.hello = self._rpc("hello", params)

    def close(self) -> None:
        for x in (self._file, self._sock):
            try:
                if x is not None:
                    x.close()
            except OSError:
                pass
        self._sock = self._file = None
        self.hello = None

    def connected(self) -> bool:
        try:
            self.call("transport")
            return True
        except LemonError as e:
            return e.code is not None

    # ---- requests ---------------------------------------------------------------------------------------------
    def _rpc(self, method: str, params: dict[str, Any]) -> Any:
        assert self._file is not None
        req = {"jsonrpc": "2.0", "id": next(self._ids), "method": method, "params": params}
        self._file.write(json.dumps(req) + "\n")
        self._file.flush()
        line = self._file.readline()
        if not line:
            raise ConnectionResetError("Lemon Studio closed the connection")
        resp = json.loads(line)
        if "error" in resp:
            err = resp["error"]
            raise LemonError(err.get("message", "error"), err.get("code"))
        return resp.get("result")

    def call(self, method: str, **params: Any) -> Any:
        """Calls `method`; reconnects once if the app was restarted since the last call."""
        params = {k: v for k, v in params.items() if v is not None}
        with self._lock:
            for attempt in (0, 1):
                try:
                    if self._file is None:
                        self._connect()
                    return self._rpc(method, params)
                except (OSError, ValueError) as e:
                    self.close()
                    if attempt == 1:
                        raise LemonError(f"lost the connection to Lemon Studio at {self.where}: {e}") from e
        raise AssertionError("unreachable")

    def apply(self, commands: list[dict[str, Any]], name: str | None = None) -> list[Any]:
        """Applies `commands` as one undo step (all or nothing); returns each command's result."""
        return self.call("apply", commands=commands, name=name)["results"]
