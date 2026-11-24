"""macOS side of the MT5 bridge: looks like the MetaTrader5 module, runs it in Wine."""

from __future__ import annotations

import functools
from multiprocessing.connection import Client
from types import SimpleNamespace


class BridgeError(RuntimeError):
    pass


def _wrap(x):
    if isinstance(x, dict):
        if "__ns__" in x and len(x) == 1:
            return SimpleNamespace(**{k: _wrap(v) for k, v in x["__ns__"].items()})
        return {k: _wrap(v) for k, v in x.items()}
    if isinstance(x, list):
        return [_wrap(v) for v in x]
    return x


class RemoteMt5:
    def __init__(self, host: str = "127.0.0.1", port: int = 18812, key: str = ""):
        if not key:
            raise BridgeError("MT5_BRIDGE_KEY is not set in .env. Run scripts/mac/setup_wine.sh.")
        try:
            self._conn = Client((host, port), authkey=key.encode())
        except OSError as e:
            raise BridgeError(f"Can't reach the MT5 bridge on {host}:{port}. "
                              "Start it with scripts/mac/start_bridge.sh.") from e
        except Exception as e:  # multiprocessing.AuthenticationError
            raise BridgeError(f"MT5 bridge refused the key: {e}") from e
        self._cache: dict = {}

    def _ask(self, msg):
        try:
            self._conn.send(msg)
            reply = self._conn.recv()
        except (EOFError, OSError) as e:
            raise BridgeError("Lost the connection to the MT5 bridge.") from e
        if reply[0] == "err":
            raise BridgeError(reply[1])
        return reply

    def _call(self, name, *args, **kwargs):
        return _wrap(self._ask(("call", name, args, kwargs))[1])

    def __getattr__(self, name):
        if name.startswith("_"):
            raise AttributeError(name)
        if name not in self._cache:
            reply = self._ask(("attr", name))
            self._cache[name] = (functools.partial(self._call, name) if reply[0] == "func"
                                 else _wrap(reply[1]))
        return self._cache[name]

    def close(self):
        self._conn.close()
