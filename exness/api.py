"""
Exness Trader API client (REST), written against the official docs at
https://www.exness-api.com (OpenAPI: /openapi/trading.public.yaml, v1.0.2).

Every request is signed with Ed25519 over a compact-JSON EXN-DATA payload.
Mutating calls are refused locally unless the client was built with
allow_trading=True (only live mode does that); a Read-only API key makes the
server refuse them too.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from risk.guard import SymbolSpec
from signals.smc import Bars

log = logging.getLogger(__name__)

EMPTY_BODY_HASH = "47DEQpj8HBSa-_TImW-5JCeuQeRkm5NMpJWZG3hSuFU"
_TF_MINUTES = {"M1": 1, "M5": 5, "M15": 15, "M30": 30, "H1": 60}


class ExnessApiError(RuntimeError):
    def __init__(self, message: str, status: int | None = None, code: int | None = None):
        super().__init__(message)
        self.status, self.code = status, code


@dataclass(frozen=True)
class OrderResult:
    ok: bool
    ticket: int | None  # position_id
    price: float
    volume: float
    message: str = ""


@dataclass(frozen=True)
class ClosedTrade:
    exit_time: datetime
    exit_price: float
    profit: float  # net of commission, account currency


# --- signing -------------------------------------------------------------------------

def b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def load_signing_key(text: str):
    """128 hex chars (seed + public key, as Key Management issues it) or PKCS#8 PEM."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    text = text.strip()
    if text.startswith("-----BEGIN PRIVATE KEY-----"):
        key = serialization.load_pem_private_key(text.encode(), password=None)
        if not isinstance(key, Ed25519PrivateKey):
            raise ExnessApiError("EXN_PRIVATE_KEY is not an Ed25519 key")
        return key
    if not re.fullmatch(r"[0-9a-fA-F]{128}", text):
        raise ExnessApiError("EXN_PRIVATE_KEY must be 128 hex characters or an unencrypted PEM")
    raw = bytes.fromhex(text)
    key = Ed25519PrivateKey.from_private_bytes(raw[:32])
    public = key.public_key().public_bytes(serialization.Encoding.Raw,
                                           serialization.PublicFormat.Raw)
    if public != raw[32:]:
        raise ExnessApiError("EXN_PRIVATE_KEY's public half does not match its seed")
    return key


def signed_headers(api_key: str, key, method: str, path_with_query: str, body: bytes = b"",
                   idempotency_key: str = "", timestamp_ms: int | None = None) -> dict:
    ts = timestamp_ms if timestamp_ms is not None else int(time.time() * 1000)
    payload = json.dumps({
        "api_key": api_key,
        "idempotency_key": idempotency_key,
        "timestamp": ts,
        "sign_version": 1,
        "method": method.upper(),
        "path": path_with_query,
        "body_hash": b64url(hashlib.sha256(body).digest()),
    }, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return {
        "EXN-API-KEY": api_key,
        "EXN-IDEMPOTENCY-KEY": idempotency_key,
        "EXN-TIMESTAMP": str(ts),
        "EXN-SIGN-VERSION": "1",
        "EXN-DATA": b64url(payload),
        "EXN-SIGN": b64url(key.sign(payload)),
    }


# --- transport + throttling --------------------------------------------------------------

class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None  # never forward signed headers elsewhere


def urllib_transport(method: str, url: str, headers: dict, body: bytes | None,
                     timeout: float = 15) -> tuple[int, str, bytes]:
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    opener = urllib.request.build_opener(_NoRedirect)
    try:
        with opener.open(req, timeout=timeout) as r:
            return r.status, r.headers.get("Content-Type", ""), r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.headers.get("Content-Type", "") if e.headers else "", e.read()


class RateLimiter:
    """Client-side sliding windows from GET /limits (the API reports no remaining quota)."""

    def __init__(self, clock=time.monotonic, sleep=time.sleep):
        self.rules: dict[str, tuple[int, float]] = {}
        self._hits: dict[str, deque] = {}
        self.clock, self.sleep = clock, sleep

    def load(self, limits: dict):
        rest = (limits.get("limits") or {}).get("rest") or {}
        g = rest.get("global_account_rate")
        if g:
            self.rules["*"] = (int(g["limit"]), float(g["window_seconds"]))
        for m in rest.get("methods") or []:
            r = m.get("rate_limit")
            if r:
                self.rules[m["api_operation_id"]] = (int(r["limit"]), float(r["window_seconds"]))

    def acquire(self, op: str):
        for k in ("*", op):
            if k not in self.rules:
                continue
            limit, window = self.rules[k]
            q = self._hits.setdefault(k, deque())
            now = self.clock()
            while q and now - q[0] >= window:
                q.popleft()
            if len(q) >= limit:
                self.sleep(max(0.0, window - (now - q[0])) + 0.01)
                now = self.clock()
                while q and now - q[0] >= window:
                    q.popleft()
            q.append(now)


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_time(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(timezone.utc)


def _https_origin(value: str) -> str:
    u = urllib.parse.urlsplit(value.strip())
    if u.scheme != "https" or not u.hostname or u.username or u.password or \
            u.path not in ("", "/") or u.query or u.fragment:
        raise ExnessApiError(f"Expected an https:// host with no path, got {value!r}")
    return f"https://{u.netloc}"


# --- client ------------------------------------------------------------------------------

class ExnessApi:
    def __init__(self, api_key: str, private_key: str, account_id: str, gateway: str,
                 allow_trading: bool = False, user_agent: str = "Claudebot/2.0",
                 order_timeout: float = 20.0, deviation_points: int = 30,
                 transport=urllib_transport, sleep=time.sleep, now=None):
        missing = [n for n, v in (("EXN_API_KEY", api_key), ("EXN_PRIVATE_KEY", private_key),
                                  ("EXN_ACCOUNT_ID", account_id),
                                  ("EXN_DISCOVERY_BASE_URL", gateway)) if not v]
        if missing:
            raise ExnessApiError(f"Missing in .env: {', '.join(missing)}")
        if not re.fullmatch(r"[1-9][0-9]*", str(account_id)):
            raise ExnessApiError("EXN_ACCOUNT_ID must be the numeric trading account ID")
        self.api_key, self.account_id = api_key, str(account_id)
        self.key = load_signing_key(private_key)
        self.gateway = _https_origin(gateway)
        self.allow_trading = allow_trading
        self.user_agent, self.order_timeout = user_agent, order_timeout
        self.deviation = deviation_points
        self._transport, self._sleep = transport, sleep
        self._now = now or (lambda: datetime.now(timezone.utc))
        self.limiter = RateLimiter(sleep=sleep)
        self.access_point: str | None = None
        self.currency = ""
        self.max_candles = 1000
        self._conditions: dict[str, dict] = {}
        self._quotes: dict[str, tuple[float, float, datetime]] = {}

    # --- plumbing ----------------------------------------------------------------------
    def _request(self, method: str, path: str, op: str, query: list | None = None,
                 body: dict | None = None, base: str | None = None, mutating: bool = False):
        if mutating and not self.allow_trading:
            raise ExnessApiError("Trading calls are disabled (dry-run).")
        qs = urllib.parse.urlencode([(k, v) for k, v in (query or []) if v is not None])
        path_q = f"{path}?{qs}" if qs else path
        raw = json.dumps(body, separators=(",", ":")).encode() if body is not None else b""
        idem = f"cb-{uuid.uuid4().hex}" if mutating else ""
        headers = signed_headers(self.api_key, self.key, method, path_q, raw, idem)
        headers["User-Agent"] = self.user_agent
        headers["Accept"] = "application/json"
        if body is not None:
            headers["Content-Type"] = "application/json"
        self.limiter.acquire(op)
        status, ctype, data = self._transport(method, (base or self.access_point) + path_q,
                                              headers, raw if body is not None else None)
        if "json" not in ctype.lower():
            raise ExnessApiError(f"{method} {path}: HTTP {status}, non-JSON response "
                                 "(edge/Gateway layer; check host, User-Agent, IP allowlist)",
                                 status)
        parsed = json.loads(data or b"{}")
        if status >= 400:
            raise ExnessApiError(f"{method} {path}: HTTP {status} {parsed.get('code')} "
                                 f"{parsed.get('error_message')}", status, parsed.get("code"))
        return parsed

    def _acct(self, area: str, rest: str = "") -> str:
        return f"/v1/{area}/accounts/{self.account_id}{rest}"

    # --- session -----------------------------------------------------------------------
    def connect(self):
        """Discover the account's Access Point, load limits and account details."""
        d = self._request("GET", "/v1/trading/access-point", "getAccessPoint",
                          [("account_id", self.account_id)], base=self.gateway)
        if not d.get("access_point"):
            raise ExnessApiError("Discovery response has no access_point")
        self.access_point = _https_origin(d["access_point"])
        limits = self._request("GET", self._acct("configuration", "/limits"), "getLimits")
        self.limiter.load(limits)
        for m in (limits.get("limits") or {}).get("rest", {}).get("methods") or []:
            if m.get("api_operation_id") == "getCandleHistory":
                self.max_candles = int((m.get("limits") or {}).get("max_candles_per_request")
                                       or self.max_candles)
        details = self._request("GET", self._acct("configuration", "/account"), "getAccountDetails")
        s = details.get("settings") or {}
        self.currency = s.get("currency", "")
        if s.get("trade_mode") == "trading_disabled" or s.get("account_status") == "close_only":
            log.warning(f"Account trading restricted: {s.get('trade_mode')} / {s.get('account_status')}")
        return details

    def shutdown(self):
        pass

    def account(self):
        st = self._snapshot().get("account_state") or {}
        return SimpleNamespace(login=self.account_id, server=self.access_point,
                               currency=self.currency, balance=float(st.get("balance", 0)),
                               equity=float(st.get("equity", 0)))

    # --- instruments -------------------------------------------------------------------
    def resolve_symbol(self, base: str) -> str:
        names = self._request("GET", self._acct("configuration", "/instruments"),
                              "getAvailableInstrumentList").get("instruments") or []
        if base in names:
            return base
        cands = sorted((n for n in names if n.upper().startswith(base.upper())), key=len)
        if not cands:
            raise ExnessApiError(f"{base} is not available to this account/API key")
        return cands[0]

    def conditions(self, symbol: str) -> dict:
        if symbol not in self._conditions:
            self._conditions[symbol] = self._request(
                "GET", self._acct("configuration", f"/instruments/{urllib.parse.quote(symbol)}/conditions"),
                "getInstrumentCondition")
        return self._conditions[symbol]

    def spec(self, symbol: str) -> SymbolSpec:
        c = self.conditions(symbol)
        if c.get("quote_currency") and self.currency and c["quote_currency"] != self.currency:
            raise ExnessApiError(f"{symbol} is quoted in {c['quote_currency']} but the account is in "
                                 f"{self.currency}; lot sizing would need a conversion rate")
        point = 10.0 ** -int(c["point_digits"])
        contract = float(c["contract_size"])
        # buy pnl = volume * (bid - open) * contract_size * ProfitRate (rate 1: same currency)
        return SymbolSpec(float(c["volume_min"]), float(c["volume_max"]), float(c["volume_step"]),
                          point, point * contract, point, 0)

    # --- market data -------------------------------------------------------------------
    def _candles(self, symbol: str, tf: str, start: datetime, end: datetime, price_type="bid"):
        out = self._request("GET", self._acct("market-data", "/candles"), "getCandleHistory", [
            ("instrument", symbol), ("price_type", price_type), ("timeframe", tf),
            ("from", _iso(start)), ("to", _iso(end))])
        return out.get("candles") or []

    @staticmethod
    def _bars(rows, side=None) -> Bars:
        t, o, h, l, c = [], [], [], [], []
        for r in rows:
            p = r[side] if side else r
            t.append(_parse_time(r["time"]))
            o.append(float(p["open"])); h.append(float(p["high"]))
            l.append(float(p["low"])); c.append(float(p["close"]))
        return Bars(t, o, h, l, c)

    def rates(self, symbol: str, timeframe: str, count: int) -> Bars:
        """Last `count` CLOSED bid candles. Also caches the latest bid/ask."""
        mins = _TF_MINUTES[timeframe]
        now = self._now()
        count = min(count, self.max_candles - 1)
        rows = self._candles(symbol, timeframe, now - timedelta(minutes=mins * (count + 1)), now,
                             "both")
        if rows:
            last = rows[-1]
            self._quotes[symbol] = (float(last["bid"]["close"]), float(last["ask"]["close"]), now)
        closed = [r for r in rows if _parse_time(r["time"]) + timedelta(minutes=mins) <= now]
        return self._bars(closed[-count:], "bid")

    def tick(self, symbol: str) -> tuple[float, float, datetime]:
        q = self._quotes.get(symbol)
        if q is None or (self._now() - q[2]).total_seconds() > 10:
            self.rates(symbol, "M1", 2)
            q = self._quotes.get(symbol)
        if q is None:
            raise ExnessApiError(f"No quotes for {symbol}")
        return q

    def history(self, symbol: str, timeframe: str, start: datetime, end: datetime) -> Bars:
        """Bid candles over a range, paged by the per-request candle limit."""
        step = timedelta(minutes=_TF_MINUTES[timeframe] * self.max_candles)
        rows, cur = [], start
        while cur < end:
            nxt = min(cur + step, end)
            rows += self._candles(symbol, timeframe, cur, nxt)
            cur = nxt
        return self._bars(rows)

    # --- trading state -----------------------------------------------------------------
    def _snapshot(self) -> dict:
        return self._request("GET", self._acct("trading", "/snapshot"), "getTradingStateSnapshot")

    def positions(self, symbol: str | None = None) -> list:
        out = []
        for p in self._snapshot().get("positions") or []:
            if p.get("state") != "open" or (symbol and p.get("instrument") != symbol):
                continue
            out.append(SimpleNamespace(ticket=int(p["position_id"]), symbol=p["instrument"],
                                       type=p["direction"], volume=float(p["volume"]),
                                       open_price=float(p.get("open_price") or 0),
                                       create_time=_parse_time(p["create_time"])))
        return out

    def _wait(self, ack: dict) -> dict:
        op_id = ack.get("operation_id")
        if not op_id:
            raise ExnessApiError(f"No operation_id in ACK: {ack}")
        deadline, delay = time.monotonic() + self.order_timeout, 0.3
        while True:
            st = self._request("GET", self._acct("trading", f"/operations/{op_id}"),
                               "getOperationStatus")
            if st.get("status") in ("confirmed", "rejected", "failed"):
                return st
            if time.monotonic() > deadline:
                return st
            self._sleep(delay)
            delay = min(delay * 2, 2.0)

    def _fmt_price(self, symbol: str, price: float) -> str:
        return f"{price:.{int(self.conditions(symbol)['point_digits'])}f}"

    def _fmt_volume(self, symbol: str, volume: float) -> str:
        step = self.conditions(symbol)["volume_step"]
        decimals = len(step.split(".")[1].rstrip("0")) if "." in step else 0
        return f"{volume:.{decimals}f}"

    def market_order(self, symbol: str, side: str, volume: float, sl: float, tp: float,
                     comment: str = "") -> OrderResult:
        before = {p.ticket for p in self.positions(symbol)}
        body = {"instrument": symbol, "side": side, "volume": self._fmt_volume(symbol, volume),
                "stop_loss_price": self._fmt_price(symbol, sl),
                "take_profit_price": self._fmt_price(symbol, tp), "comment": comment[:31]}
        if self.conditions(symbol).get("exec_mode") == "instant":
            bid, ask, _ = self.tick(symbol)
            body["price"] = self._fmt_price(symbol, ask if side == "buy" else bid)
            body["deviation"] = str(self.deviation)
        ack = self._request("POST", self._acct("trading", "/positions"), "openPosition",
                            body=body, mutating=True)
        st = self._wait(ack)
        if st.get("status") != "confirmed":
            return OrderResult(False, None, 0.0, 0.0,
                               f"{st.get('status')} {st.get('error_code', '')} "
                               f"{st.get('error_message', '')}".strip())
        for _ in range(6):  # the ACK has no position id: find the new position
            new = [p for p in self.positions(symbol) if p.ticket not in before and p.type == side]
            if new:
                p = max(new, key=lambda x: x.create_time)
                return OrderResult(True, p.ticket, p.open_price, p.volume, "confirmed")
            self._sleep(0.5)
        log.warning(f"{symbol}: order confirmed but the new position wasn't in the snapshot")
        return OrderResult(True, None, 0.0, volume, "confirmed, position not located")

    def close_position(self, pos, comment: str = "time stop") -> OrderResult:
        ack = self._request("DELETE", self._acct("trading", f"/positions/{pos.ticket}"),
                            "closePosition", mutating=True)
        st = self._wait(ack)
        ok = st.get("status") == "confirmed"
        return OrderResult(ok, pos.ticket, 0.0, pos.volume,
                           f"{st.get('status')} {st.get('error_message', '')}".strip())

    def closed_result(self, ticket: int, since: datetime) -> ClosedTrade | None:
        query = [("from", _iso(since - timedelta(minutes=1))), ("limit", 200)]
        deals = []
        for _ in range(10):
            page = self._request("GET", self._acct("history", "/deals"), "getDealsHistory", query)
            deals += [i["deal"] for i in page.get("items") or []]
            if not page.get("has_more") or not page.get("next_cursor"):
                break
            query = [("cursor", page["next_cursor"]), ("limit", 200)]
        mine = [d for d in deals if str(d.get("position_id")) == str(ticket)]
        closes = [d for d in mine if d.get("type") == "close"]
        if not closes:
            return None
        last = max(closes, key=lambda d: d["create_time"])
        profit = sum(float((d.get("profit") or {}).get("amount") or 0) for d in closes)
        profit += sum(float(d.get("commission") or 0) for d in mine)
        return ClosedTrade(_parse_time(last["create_time"]), float(last.get("price") or 0), profit)
