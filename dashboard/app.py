import logging
import threading
from pathlib import Path

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import text

from data.db import get_session
from config.settings import get_config

logger = logging.getLogger(__name__)

_TEMPLATE_DIR = Path(__file__).parent / "templates"
app = FastAPI(title="Claudebot", docs_url=None, redoc_url=None)
templates = Jinja2Templates(directory=str(_TEMPLATE_DIR))


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
        closed = total - open_count

        return {
            "equity":        round(paper_balance + total_pnl, 2),
            "total_pnl":     round(total_pnl, 2),
            "today_pnl":     round(today_pnl, 2),
            "today_trades":  today_count,
            "total_trades":  total,
            "closed_trades": closed,
            "open_positions": open_count,
            "win_rate":      round(wins / closed * 100, 1) if closed > 0 else 0.0,
            "mode":          "PAPER" if cfg["bot"].get("paper_mode", True) else "LIVE",
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
            current = float(r[10]) if r[10] is not None else None
            mkt_price = current if current is not None else entry
            out.append({
                "id":            r[0],
                "symbol":        r[1],
                "strategy":      r[2],
                "side":          r[3],
                "entry_price":   entry,
                "current_price": current,
                "quantity":      qty,
                "value_usdt":    round(mkt_price * qty, 2),
                "unrealized_pnl":     round((mkt_price - entry) * qty, 2),
                "unrealized_pnl_pct": round((mkt_price - entry) / entry * 100, 2) if entry else None,
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


def start_dashboard(host: str = "0.0.0.0", port: int = 8080):
    """Start the dashboard web server in a background daemon thread."""
    cfg = uvicorn.Config(app, host=host, port=port, log_level="warning")
    server = uvicorn.Server(cfg)
    t = threading.Thread(target=server.run, daemon=True, name="dashboard")
    t.start()
    logger.info(f"Dashboard → http://localhost:{port}")
    return t
