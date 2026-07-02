import logging
import re
import threading
from pathlib import Path

import uvicorn
from fastapi import Body, FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import text

from data.db import get_session
from config.settings import get_config, has_binance_keys

_CONFIG_PATH = Path(__file__).parent.parent / "config" / "config.yaml"

logger = logging.getLogger(__name__)

_TEMPLATE_DIR = Path(__file__).parent / "templates"
app = FastAPI(title="Claudebot", docs_url=None, redoc_url=None)
templates = Jinja2Templates(directory=str(_TEMPLATE_DIR))

# The dashboard runs as a thread inside the bot process; the orchestrator
# registers itself here so control endpoints can drive the live wallet.
_BOT = {"orchestrator": None}


def register_bot(orchestrator):
    _BOT["orchestrator"] = orchestrator


def _runtime_mode(cfg) -> str:
    orch = _BOT.get("orchestrator")
    if orch is not None:
        return "PAPER" if orch.paper_mode else "LIVE"
    return "PAPER" if cfg["bot"].get("paper_mode", True) else "LIVE"


def _paper_guard():
    """Control endpoints are paper-mode only — never touch a live account.
    Checks the RUNNING bot's mode, not just the config file."""
    orch = _BOT.get("orchestrator")
    if orch is not None and not orch.paper_mode:
        return "Refused: bot is running in LIVE mode"
    cfg = get_config()
    if orch is None and not cfg["bot"].get("paper_mode", True):
        return "Refused: bot is configured LIVE"
    return None


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    return templates.TemplateResponse("index.html", {"request": request})


@app.get("/api/summary")
async def summary():
    cfg = get_config()
    paper_balance = cfg["bot"].get("paper_balance_usdt", 10_000)
    session = get_session()
    try:
        row = session.execute(text("""
            SELECT
                COALESCE(SUM(CASE WHEN status='closed' THEN pnl ELSE 0 END), 0) AS total_pnl,
                COUNT(*) AS total_trades,
                SUM(CASE WHEN pnl > 0 AND status='closed' THEN 1 ELSE 0 END) AS wins,
                SUM(CASE WHEN status='open' THEN 1 ELSE 0 END) AS open_count
            FROM trades
        """)).fetchone()

        today_row = session.execute(text("""
            SELECT COALESCE(SUM(pnl), 0), COUNT(*)
            FROM trades
            WHERE DATE(exit_time) = CURRENT_DATE AND status = 'closed'
        """)).fetchone()

        # Unrealized PnL on open positions, marked to the latest cached price.
        # equity = start + realized + unrealized — same definition the bot's
        # in-memory wallet uses, so terminal and dashboard agree.
        unreal_row = session.execute(text("""
            SELECT COALESCE(SUM(
                (COALESCE(
                    (SELECT c.close_price FROM ohlcv_cache c
                     WHERE c.symbol = t.symbol
                     ORDER BY c.open_time DESC LIMIT 1),
                    t.entry_price) - t.entry_price) * t.quantity
                * (CASE WHEN t.side = 'buy' THEN 1 ELSE -1 END)), 0),
                COALESCE(SUM(t.entry_price * t.quantity), 0)
            FROM trades t WHERE t.status = 'open'
        """)).fetchone()

        ml_row = session.execute(text("""
            SELECT version, accuracy, f1_score, training_samples, trained_at
            FROM ml_models
            WHERE model_name = 'xgb_main' AND is_active = 1
            ORDER BY trained_at DESC LIMIT 1
        """)).fetchone()

        total_pnl   = float(row[0]) if row else 0.0
        total       = int(row[1])   if row else 0
        wins        = int(row[2] or 0) if row else 0
        open_count  = int(row[3] or 0) if row else 0
        today_pnl   = float(today_row[0]) if today_row else 0.0
        today_count = int(today_row[1])   if today_row else 0
        unreal_pnl  = float(unreal_row[0]) if unreal_row else 0.0
        deployed    = float(unreal_row[1]) if unreal_row else 0.0
        closed = total - open_count

        return {
            "equity":        round(paper_balance + total_pnl + unreal_pnl, 2),
            "free_cash":     round(paper_balance + total_pnl - deployed, 2),
            "deployed":      round(deployed + unreal_pnl, 2),
            "total_pnl":     round(total_pnl, 2),
            "unrealized_pnl": round(unreal_pnl, 2),
            "today_pnl":     round(today_pnl, 2),
            "today_trades":  today_count,
            "total_trades":  total,
            "closed_trades": closed,
            "open_positions": open_count,
            "win_rate":      round(wins / closed * 100, 1) if closed > 0 else 0.0,
            # Runtime mode = what the running bot is actually doing;
            # configured mode = what config.yaml says (differs after a toggle
            # until the bot restarts).
            "mode":          _runtime_mode(cfg),
            "configured_mode": "PAPER" if cfg["bot"].get("paper_mode", True) else "LIVE",
            "ml": {
                "version":   ml_row[0] if ml_row else None,
                "accuracy":  round(float(ml_row[1]) * 100, 1) if ml_row and ml_row[1] else None,
                "f1":        round(float(ml_row[2]), 3) if ml_row and ml_row[2] else None,
                "samples":   int(ml_row[3]) if ml_row and ml_row[3] else 0,
                "trained_at": ml_row[4].strftime("%Y-%m-%d %H:%M") if ml_row and ml_row[4] else None,
            },
        }
    finally:
        session.close()


@app.get("/api/trades/open")
async def open_trades():
    session = get_session()
    try:
        # Latest cached close per symbol = best available "current" price.
        # The fetcher writes every candle to ohlcv_cache each tick.
        rows = session.execute(text("""
            SELECT t.id, t.symbol, t.strategy, t.side, t.entry_price, t.quantity,
                   t.stop_loss, t.take_profit, t.ml_confidence, t.entry_time,
                   (SELECT c.close_price FROM ohlcv_cache c
                    WHERE c.symbol = t.symbol
                    ORDER BY c.open_time DESC LIMIT 1) AS current_price
            FROM trades t WHERE t.status = 'open'
            ORDER BY t.entry_time DESC
        """)).fetchall()
        out = []
        for r in rows:
            entry = float(r[4])
            qty = float(r[5])
            side = r[3]
            direction = 1 if side == "buy" else -1   # short PnL is inverted
            current = float(r[10]) if r[10] is not None else None
            mkt_price = current if current is not None else entry
            unreal = (mkt_price - entry) * qty * direction
            out.append({
                "id":            r[0],
                "symbol":        r[1],
                "strategy":      r[2],
                "side":          side,
                "entry_price":   entry,
                "current_price": current,
                "quantity":      qty,
                # value = reserved margin + unrealized (matches bot equity)
                "value_usdt":    round(entry * qty + unreal, 2),
                "unrealized_pnl":     round(unreal, 2),
                "unrealized_pnl_pct": round((mkt_price - entry) / entry * 100 * direction, 2) if entry else None,
                "stop_loss":     float(r[6]) if r[6] else None,
                "take_profit":   float(r[7]) if r[7] else None,
                "ml_confidence": round(float(r[8]) * 100, 1) if r[8] else None,
                "entry_time":    r[9].strftime("%Y-%m-%d %H:%M") if r[9] else None,
            })
        return out
    finally:
        session.close()


@app.get("/api/trades/closed")
async def closed_trades(limit: int = 50):
    session = get_session()
    try:
        rows = session.execute(text("""
            SELECT id, symbol, strategy, entry_price, exit_price, quantity,
                   pnl, pnl_pct, duration_minutes, entry_time, exit_time
            FROM trades WHERE status = 'closed'
            ORDER BY exit_time DESC
            LIMIT :limit
        """), {"limit": limit}).fetchall()
        return [
            {
                "id":               r[0],
                "symbol":           r[1],
                "strategy":         r[2],
                "entry_price":      float(r[3]),
                "exit_price":       float(r[4]) if r[4] else None,
                "quantity":         float(r[5]),
                "pnl":              round(float(r[6]), 4) if r[6] else None,
                "pnl_pct":          round(float(r[7]) * 100, 2) if r[7] else None,
                "duration_minutes": int(r[8]) if r[8] else None,
                "entry_time":       r[9].strftime("%Y-%m-%d %H:%M") if r[9] else None,
                "exit_time":        r[10].strftime("%Y-%m-%d %H:%M") if r[10] else None,
            }
            for r in rows
        ]
    finally:
        session.close()


@app.get("/api/pnl/chart")
async def pnl_chart():
    session = get_session()
    try:
        rows = session.execute(text("""
            SELECT DATE(exit_time) AS day, SUM(pnl) AS daily_pnl
            FROM trades
            WHERE status = 'closed' AND exit_time IS NOT NULL
            GROUP BY DATE(exit_time)
            ORDER BY day
        """)).fetchall()
        cumulative, labels, data = 0.0, [], []
        for r in rows:
            cumulative += float(r[1])
            labels.append(str(r[0]))
            data.append(round(cumulative, 2))
        return {"labels": labels, "data": data}
    finally:
        session.close()


@app.get("/api/strategies")
async def strategy_performance():
    session = get_session()
    try:
        rows = session.execute(text("""
            SELECT strategy,
                   COUNT(*) AS total,
                   SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) AS wins,
                   COALESCE(SUM(pnl), 0) AS total_pnl,
                   COALESCE(AVG(pnl_pct), 0) AS avg_pnl_pct
            FROM trades WHERE status = 'closed'
            GROUP BY strategy
            ORDER BY total_pnl DESC
        """)).fetchall()
        return [
            {
                "strategy":   r[0],
                "total":      int(r[1]),
                "wins":       int(r[2] or 0),
                "win_rate":   round(int(r[2] or 0) / int(r[1]) * 100, 1),
                "total_pnl":  round(float(r[3]), 2),
                "avg_pnl_pct": round(float(r[4]) * 100, 2),
            }
            for r in rows
        ]
    finally:
        session.close()


@app.get("/api/liquidations")
async def recent_liquidations(limit: int = 20):
    session = get_session()
    try:
        rows = session.execute(text("""
            SELECT symbol, side, quantity, price, usd_value, event_time, traded_on
            FROM liquidation_events
            ORDER BY event_time DESC
            LIMIT :limit
        """), {"limit": limit}).fetchall()
        return [
            {
                "symbol":     r[0],
                "side":       r[1],
                "quantity":   float(r[2]),
                "price":      float(r[3]),
                "usd_value":  float(r[4]),
                "event_time": r[5].strftime("%H:%M:%S") if r[5] else None,
                "traded_on":  bool(r[6]),
            }
            for r in rows
        ]
    finally:
        session.close()


@app.post("/api/paper/close_all")
async def close_all_paper():
    """Flatten every open position at the current price (PnL realized honestly,
    fees and slippage included). Paper mode only."""
    err = _paper_guard()
    if err:
        return {"ok": False, "error": err}
    orch = _BOT["orchestrator"]
    if orch is None:
        return {"ok": False, "error": "Bot not connected yet — try again in a few seconds"}

    closed, failed = 0, 0
    for t in orch.orders.get_open_trades():
        r = orch.orders.close_position(
            t.symbol, float(t.quantity), t.id, side=(t.side or "buy"), fraction=1.0
        )
        if r:
            closed += 1
        else:
            failed += 1
    logger.info(f"[DASHBOARD] Close-all: {closed} closed, {failed} failed")
    return {"ok": True, "closed": closed, "failed": failed}


@app.post("/api/paper/reset")
async def reset_paper():
    """Full paper reset: wipe trades + signals, deactivate old ML models,
    restore the wallet to its starting balance. Paper mode only."""
    err = _paper_guard()
    if err:
        return {"ok": False, "error": err}

    session = get_session()
    try:
        session.execute(text("DELETE FROM signals"))
        session.execute(text("DELETE FROM trades"))
        # Old models were trained on the wiped history — retire them so both
        # classes restart in bootstrap mode and learn from clean data.
        session.execute(text("UPDATE ml_models SET is_active=0"))
        session.commit()
    except Exception as e:
        session.rollback()
        logger.error(f"Paper reset failed: {e}")
        return {"ok": False, "error": str(e)}
    finally:
        session.close()

    orch = _BOT["orchestrator"]
    if orch is not None:
        orch.orders.reset_paper_wallet()
        orch.guards.reset_daily()
        orch.guards.set_starting_balance(orch.orders.equity())
        orch.predictor.unload()
    logger.info("[DASHBOARD] Paper reset complete — fresh wallet, clean history")
    return {"ok": True}


@app.post("/api/mode")
async def set_mode(payload: dict = Body(...)):
    """
    Toggle paper/live in config.yaml. Takes effect on RESTART — the wallet
    model and order routing are fixed at startup, so hot-flipping mid-run
    would strand open paper positions inside a live engine.
    Uses a targeted line edit so the config's comments are preserved.
    """
    mode = str(payload.get("mode", "")).lower()
    if mode not in ("paper", "live"):
        return {"ok": False, "error": "mode must be 'paper' or 'live'"}

    warning = None
    if mode == "live" and not has_binance_keys():
        return {"ok": False, "error": "No Binance API keys configured — "
                "add them to config/secrets.yaml or .env before going live"}

    try:
        text_cfg = _CONFIG_PATH.read_text(encoding="utf-8")
        new_val = "true" if mode == "paper" else "false"
        updated, n = re.subn(r"(paper_mode:\s*)(true|false)",
                             rf"\g<1>{new_val}", text_cfg, count=1)
        if n == 0:
            return {"ok": False, "error": "paper_mode key not found in config.yaml"}
        _CONFIG_PATH.write_text(updated, encoding="utf-8")
    except Exception as e:
        return {"ok": False, "error": f"Could not update config: {e}"}

    logger.warning(f"[DASHBOARD] Trading mode set to {mode.upper()} — restart required")
    return {"ok": True, "configured_mode": mode.upper(),
            "restart_required": True, "warning": warning}


def start_dashboard(host: str = "0.0.0.0", port: int = 8080):
    """Start the dashboard web server in a background daemon thread."""
    cfg = uvicorn.Config(app, host=host, port=port, log_level="warning")
    server = uvicorn.Server(cfg)
    t = threading.Thread(target=server.run, daemon=True, name="dashboard")
    t.start()
    logger.info(f"Dashboard → http://localhost:{port}")
    return t
