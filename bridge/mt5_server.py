"""
MT5 bridge server. Runs INSIDE Wine, under the Windows Python that has the
MetaTrader5 package (scripts/mac/start_bridge.sh starts it). The bot runs in
normal macOS Python and reaches MetaTrader5 through this, over 127.0.0.1.

Standard library only (plus MetaTrader5), so the Wine Python needs nothing else.
Results are converted to plain lists/dicts here, so the Mac side never has to
unpickle MetaTrader5's own types.
"""

import logging
import os
import sys
import threading
from multiprocessing.connection import Listener

log = logging.getLogger("mt5-bridge")
_HERE = os.path.dirname(os.path.abspath(__file__))
_lock = threading.Lock()  # the MetaTrader5 library is not meant for concurrent calls


def read_key() -> bytes:
    key = os.environ.get("MT5_BRIDGE_KEY", "").strip()
    env_path = os.path.join(_HERE, "..", ".env")
    if not key and os.path.exists(env_path):
        with open(env_path, encoding="utf-8") as f:
            for line in f:
                if line.startswith("MT5_BRIDGE_KEY="):
                    key = line.split("=", 1)[1].strip()
    if not key:
        sys.exit("MT5_BRIDGE_KEY is not set. Run scripts/mac/setup_wine.sh first.")
    return key.encode()


def plain(x):
    """MetaTrader5 results -> builtins. Named tuples become {"__ns__": {...}}."""
    if x is None or isinstance(x, (bool, int, float, str)):
        return x
    if hasattr(x, "_asdict"):
        return {"__ns__": {k: plain(v) for k, v in x._asdict().items()}}
    dtype = getattr(x, "dtype", None)
    if dtype is not None and getattr(dtype, "names", None):  # numpy structured array (rates)
        names = dtype.names
        return [dict(zip(names, plain(list(row)))) for row in x.tolist()]
    if hasattr(x, "tolist"):  # numpy scalar / plain array
        return plain(x.tolist())
    if isinstance(x, (list, tuple)):
        return [plain(v) for v in x]
    if isinstance(x, dict):
        return {k: plain(v) for k, v in x.items()}
    return x  # datetimes etc. pickle fine


def handle(module, msg):
    op, name = msg[0], msg[1]
    if name.startswith("_"):
        return ("err", f"refused private name {name}")
    try:
        if op == "attr":
            value = getattr(module, name)
            return ("func",) if callable(value) else ("const", plain(value))
        if op == "call":
            with _lock:
                return ("ok", plain(getattr(module, name)(*msg[2], **msg[3])))
        return ("err", f"unknown op {op}")
    except Exception as e:  # report back, keep serving
        return ("err", f"{type(e).__name__}: {e}")


def _serve_conn(conn, module):
    with conn:
        while True:
            try:
                msg = conn.recv()
            except (EOFError, OSError):
                return
            conn.send(handle(module, msg))


def serve_forever(listener, module):
    while True:
        try:
            conn = listener.accept()
        except Exception as e:  # wrong key, dropped handshake
            log.warning(f"rejected connection: {e}")
            continue
        threading.Thread(target=_serve_conn, args=(conn, module), daemon=True).start()


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s: %(message)s")
    import MetaTrader5  # noqa: N813

    host = os.environ.get("MT5_BRIDGE_HOST", "127.0.0.1")
    port = int(os.environ.get("MT5_BRIDGE_PORT", "18812"))
    with Listener((host, port), authkey=read_key()) as listener:
        log.info(f"MT5 bridge listening on {host}:{port} (MetaTrader5 {MetaTrader5.__version__})")
        serve_forever(listener, MetaTrader5)


if __name__ == "__main__":
    main()
