import base64
import json
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from exness.api import (EMPTY_BODY_HASH, ExnessApi, ExnessApiError, RateLimiter, b64url,
                        load_signing_key, signed_headers)

# From https://www.exness-api.com/trading/guides/code-examples (synthetic example)
DOC_DATA = ("eyJhcGlfa2V5IjoiZXhuc2stc3ludGhldGljLWV4YW1wbGUiLCJpZGVtcG90ZW5jeV9rZXkiOiIiLCJ0aW1lc3Rh"
            "bXAiOjE3NzM2NTYwNzAxMjMsInNpZ25fdmVyc2lvbiI6MSwibWV0aG9kIjoiR0VUIiwicGF0aCI6Ii92MS90cm"
            "FkaW5nL2FjY2Vzcy1wb2ludD9hY2NvdW50X2lkPTEyMzQ1NiIsImJvZHlfaGFzaCI6IjQ3REVRcGo4SEJTYS1f"
            "VEltVy01SkNldVFlUmttNU5NcEpXWkczaFN1RlUifQ")

SEED = Ed25519PrivateKey.generate()
RAW = SEED.private_bytes(serialization.Encoding.Raw, serialization.PrivateFormat.Raw,
                         serialization.NoEncryption())
PUB = SEED.public_key()
HEX_KEY = (RAW + PUB.public_bytes(serialization.Encoding.Raw,
                                  serialization.PublicFormat.Raw)).hex()
NOW = datetime(2026, 11, 24, 10, 0, 30, tzinfo=timezone.utc)


def _unb64(s):
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def test_exn_data_matches_docs_vector():
    h = signed_headers("exnsk-synthetic-example", SEED, "GET",
                       "/v1/trading/access-point?account_id=123456", timestamp_ms=1773656070123)
    assert h["EXN-DATA"] == DOC_DATA
    assert h["EXN-IDEMPOTENCY-KEY"] == "" and h["EXN-SIGN-VERSION"] == "1"
    PUB.verify(_unb64(h["EXN-SIGN"]), _unb64(h["EXN-DATA"]))  # raises if bad
    assert b64url(__import__("hashlib").sha256(b"").digest()) == EMPTY_BODY_HASH


def test_post_signs_body_hash():
    body = b'{"instrument":"XAUUSD"}'
    h = signed_headers("k", SEED, "post", "/x", body, "idem-12345", 1)
    data = json.loads(_unb64(h["EXN-DATA"]))
    assert data["method"] == "POST" and data["idempotency_key"] == "idem-12345"
    assert data["body_hash"] == b64url(__import__("hashlib").sha256(body).digest())


def test_key_formats():
    assert load_signing_key(HEX_KEY)
    pem = SEED.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                             serialization.NoEncryption()).decode()
    assert load_signing_key(pem)
    with pytest.raises(ExnessApiError):
        load_signing_key("abc")
    bad = HEX_KEY[:64] + "00" * 32
    with pytest.raises(ExnessApiError, match="public half"):
        load_signing_key(bad)


class FakeExness:
    """Checks every request's signature and answers like the documented API."""

    def __init__(self):
        self.calls = []
        self.positions = []
        self.op_polls = 0

    def __call__(self, method, url, headers, body):
        u = urlsplit(url)
        path_q = u.path + (f"?{u.query}" if u.query else "")
        data = json.loads(_unb64(headers["EXN-DATA"]))
        PUB.verify(_unb64(headers["EXN-SIGN"]), _unb64(headers["EXN-DATA"]))
        assert data["path"] == path_q and data["method"] == method
        assert headers["User-Agent"]
        if method == "GET":
            assert headers["EXN-IDEMPOTENCY-KEY"] == ""
        else:
            assert len(headers["EXN-IDEMPOTENCY-KEY"]) >= 5
        self.calls.append((method, u.netloc, u.path, u.query, json.loads(body) if body else None))
        return self.route(method, u.netloc, u.path, u.query, body)

    def ok(self, obj, status=200):
        return status, "application/json", json.dumps(obj).encode()

    def route(self, method, host, path, query, body):
        a = "/v1/{}/accounts/42"
        if path == "/v1/trading/access-point":
            assert host == "gw.exness.example" and query == "account_id=42"
            return self.ok({"access_point": "https://ap-1.exness.example", "account_id": "42"})
        assert host == "ap-1.exness.example"
        if path == a.format("configuration") + "/limits":
            return self.ok({"limits": {"rest": {
                "global_account_rate": {"limit": 100, "window_seconds": 1},
                "methods": [{"api_operation_id": "getCandleHistory", "http_method": "GET",
                             "path": "x", "rate_limit": {"limit": 50, "window_seconds": 1},
                             "limits": {"max_candles_per_request": 500}}]}}})
        if path == a.format("configuration") + "/account":
            return self.ok({"account_id": "42", "create_date": "2026-01-01T00:00:00Z",
                            "settings": {"currency": "USD", "leverage": "1:200",
                                         "account_status": "active", "trade_mode": "enabled",
                                         "execution_mode": "market"}})
        if path == a.format("configuration") + "/instruments":
            return self.ok({"instruments": ["BTCUSDm", "XAUUSDm", "XAUUSDm.pro"]})
        if path.endswith("/instruments/XAUUSDm/conditions"):
            return self.ok({"instrument": "XAUUSDm", "quote_currency": "USD", "point_digits": 3,
                            "contract_size": "100", "volume_min": "0.01", "volume_max": "200",
                            "volume_step": "0.01", "exec_mode": "market"})
        if path == a.format("trading") + "/snapshot":
            return self.ok({"account_state": {"balance": "1000.00", "equity": "1012.50",
                                              "used_margin": "0"},
                            "positions": self.positions, "orders": []})
        if path == a.format("market-data") + "/candles":
            rows = []
            for k in range(5, -1, -1):  # newest is still forming
                t = NOW.replace(second=0) - timedelta(minutes=k)
                p = 2000.0 + k
                rows.append({"time": t.strftime("%Y-%m-%dT%H:%M:%SZ"), "volume": 1,
                             "bid": {"open": p, "high": p + 1, "low": p - 1, "close": p},
                             "ask": {"open": p + .2, "high": p + 1.2, "low": p - .8,
                                     "close": p + .2}})
            return self.ok({"candles": rows})
        if method == "POST" and path == a.format("trading") + "/positions":
            self.positions.append({"position_id": "777", "direction": "buy",
                                   "instrument": "XAUUSDm", "state": "open", "volume": "0.05",
                                   "open_price": "2000.2", "create_time": "2026-11-24T10:00:31Z"})
            return self.ok({"status": "accepted", "operation_id": "9"}, 202)
        if path == a.format("trading") + "/operations/9":
            self.op_polls += 1
            return self.ok({"operation_id": "9", "status": "pending" if self.op_polls < 2 else
                            "confirmed", "created_at": "x", "updated_at": "x"})
        if method == "DELETE" and path == a.format("trading") + "/positions/777":
            return self.ok({"status": "accepted", "operation_id": "9"}, 202)
        if path == a.format("history") + "/deals":
            return self.ok({"items": [
                {"event_time": "t", "deal": {"deal_id": "1", "type": "open", "position_id": "777",
                                             "create_time": "2026-11-24T10:00:31Z",
                                             "commission": "-0.40", "price": "2000.2"}},
                {"event_time": "t", "deal": {"deal_id": "2", "type": "close", "position_id": "777",
                                             "create_time": "2026-11-24T10:09:00Z",
                                             "commission": "-0.40", "price": "2004.0",
                                             "profit": {"amount": "19.00", "currency": "USD"}}},
                {"event_time": "t", "deal": {"deal_id": "3", "type": "close", "position_id": "5",
                                             "create_time": "2026-11-24T10:09:00Z",
                                             "profit": {"amount": "-50", "currency": "USD"}}}],
                "has_more": False})
        if path.endswith("/boom"):
            return self.ok({"code": 3001, "error_message": "REQUEST_INVALID_PARAMETERS"}, 400)
        return 403, "text/html", b"<html>edge</html>"


def _api(fake, trading=False):
    api = ExnessApi("exnsk-test", HEX_KEY, "42", "https://gw.exness.example",
                    allow_trading=trading, transport=fake, sleep=lambda s: None, now=lambda: NOW)
    api.connect()
    return api


def test_connect_discovers_access_point_and_limits():
    api = _api(FakeExness())
    assert api.access_point == "https://ap-1.exness.example"
    assert api.currency == "USD" and api.max_candles == 500
    assert api.limiter.rules["getCandleHistory"] == (50, 1.0)
    acct = api.account()
    assert acct.equity == 1012.5 and acct.login == "42"


def test_resolve_symbol_and_spec():
    api = _api(FakeExness())
    assert api.resolve_symbol("XAUUSD") == "XAUUSDm"
    spec = api.spec("XAUUSDm")
    # 1 lot = 100 oz: a $1.00 move = $100, so one 0.001 point = $0.10
    assert spec.tick_size == pytest.approx(0.001) and spec.tick_value == pytest.approx(0.1)
    with pytest.raises(ExnessApiError):
        api.resolve_symbol("EURUSD")


def test_rates_drop_forming_bar_and_cache_quote():
    fake = FakeExness()
    api = _api(fake)
    bars = api.rates("XAUUSDm", "M1", 10)
    assert len(bars) == 5 and bars.close[-1] == 2001.0  # 10:00 bar still forming
    bid, ask, _ = api.tick("XAUUSDm")
    assert (bid, ask) == (2000.0, 2000.2)
    q = fake.calls[-1][3]
    assert "price_type=both" in q and "timeframe=M1" in q and "from=" in q and "to=" in q


def test_dry_run_client_refuses_to_trade():
    fake = FakeExness()
    api = _api(fake)
    with pytest.raises(ExnessApiError, match="disabled"):
        api.market_order("XAUUSDm", "buy", 0.05, 1995, 2010)
    assert not any(c[0] in ("POST", "DELETE") for c in fake.calls)


def test_market_order_waits_for_confirmation_and_finds_position():
    fake = FakeExness()
    api = _api(fake, trading=True)
    res = api.market_order("XAUUSDm", "buy", 0.05, 1995.12345, 2010.0, "smc#1")
    assert res.ok and res.ticket == 777 and res.price == 2000.2
    post = next(c for c in fake.calls if c[0] == "POST")
    assert post[4] == {"instrument": "XAUUSDm", "side": "buy", "volume": "0.05",
                       "stop_loss_price": "1995.123", "take_profit_price": "2010.000",
                       "comment": "smc#1"}
    assert fake.op_polls == 2


def test_close_and_closed_result():
    fake = FakeExness()
    api = _api(fake, trading=True)
    api.market_order("XAUUSDm", "buy", 0.05, 1995, 2010)
    pos = api.positions("XAUUSDm")[0]
    assert api.close_position(pos).ok
    r = api.closed_result(777, NOW)
    assert r.exit_price == 2004.0 and r.profit == pytest.approx(18.2)


def test_errors_are_explicit():
    api = _api(FakeExness())
    with pytest.raises(ExnessApiError, match="REQUEST_INVALID_PARAMETERS") as e:
        api._request("GET", "/boom", "x")
    assert e.value.code == 3001
    with pytest.raises(ExnessApiError, match="non-JSON"):
        api._request("GET", "/v1/unknown", "x")


def test_missing_credentials():
    with pytest.raises(ExnessApiError, match="EXN_API_KEY"):
        ExnessApi("", HEX_KEY, "42", "https://gw.example")
    with pytest.raises(ExnessApiError, match="https"):
        ExnessApi("k", HEX_KEY, "42", "http://gw.example")


def test_rate_limiter_waits():
    t = [0.0]
    slept = []
    rl = RateLimiter(clock=lambda: t[0], sleep=lambda s: (slept.append(s), t.__setitem__(0, t[0] + s)))
    rl.rules["op"] = (2, 1.0)
    for _ in range(3):
        rl.acquire("op")
    assert slept and slept[0] == pytest.approx(1.01)
