"""
app.py — FastAPI server: WebSocket broadcast + strategy background loop
v3: Dual book OBI + EMA signal + parámetros calibrados
"""

import asyncio
import json
import logging
import os
import time
from collections import deque
from contextlib import asynccontextmanager
from datetime import datetime

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("strategy")

import uvicorn
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from starlette.requests import Request

from strategy_core import (
    find_active_sol_market,
    get_dual_book_metrics,
    get_order_book_metrics,
    compute_combined_obi,
    compute_signal,
    seconds_remaining,
    fetch_clob_market,
    build_market_info,
    fetch_gamma_market,
    get_current_slot_ts,
    SLOT_STEP,
)
from simulator import Portfolio
import db as database

# ── Order book depth helpers (display only) ───────────────────────────────────

def _walk_asks_for_entry(top_asks: list, bet_usdc: float) -> dict:
    remaining = bet_usdc
    shares = 0.0
    cost = 0.0
    for price, size in top_asks:
        if remaining <= 0:
            break
        fill_usdc = min(remaining, price * size)
        shares += fill_usdc / price
        cost += fill_usdc
        remaining -= fill_usdc
    avg = cost / shares if shares > 0 else 0.0
    return {
        "shares":        round(shares, 4),
        "avg_price":     round(avg, 4),
        "cost":          round(cost, 4),
        "filled":        remaining <= 0.001,
        "unfilled_usdc": round(max(0.0, remaining), 4),
        "fill_pct":      round(min(100.0, cost / bet_usdc * 100), 1) if bet_usdc > 0 else 0.0,
    }


def _walk_bids_for_exit(top_bids: list, shares_to_sell: float) -> dict:
    remaining = shares_to_sell
    proceeds = 0.0
    for price, size in top_bids:
        if remaining <= 0:
            break
        fill = min(remaining, size)
        proceeds += fill * price
        remaining -= fill
    sold = shares_to_sell - remaining
    avg = proceeds / sold if sold > 0 else 0.0
    return {
        "shares_sold": round(sold, 4),
        "avg_price":   round(avg, 4),
        "proceeds":    round(proceeds, 4),
        "filled":      remaining <= 0.001,
        "unfilled":    round(max(0.0, remaining), 4),
        "fill_pct":    round(min(100.0, sold / shares_to_sell * 100), 1) if shares_to_sell > 0 else 0.0,
    }


# ── Config ────────────────────────────────────────────────────────────────────
# OBI_THRESHOLD más bajo: 0.15 activa señales con desequilibrio moderado pero real
# En mercados 5min de Polymarket, OBI ±0.35 es raro → threshold 0.15-0.20 es más realista
POLL_INTERVAL = float(os.environ.get("POLL_INTERVAL", "3"))
OBI_THRESHOLD = float(os.environ.get("OBI_THRESHOLD", "0.18"))   # Bajado de 0.35 → 0.18
WINDOW_SIZE   = int(os.environ.get("WINDOW_SIZE", "6"))           # Ventana EMA reducida (18s)
PORT          = int(os.environ.get("PORT", "8000"))

# ── Shared state ──────────────────────────────────────────────────────────────
connected: set[WebSocket] = set()

state: dict = {
    "market":    {},
    "orderbook": {},
    "signal":    {},
    "portfolio": {},
    "config":    {
        "threshold":       OBI_THRESHOLD,
        "window_size":     WINDOW_SIZE,
        "poll_interval":   POLL_INTERVAL,
        "initial_capital": 100.0,
        "trade_pct":       0.02,
    },
    "status":    "initializing",
    "error":     None,
}


# ── WebSocket broadcast ───────────────────────────────────────────────────────

async def broadcast(data: dict):
    dead: set[WebSocket] = set()
    for ws in connected.copy():
        try:
            await ws.send_json(data)
        except Exception:
            dead.add(ws)
    connected.difference_update(dead)


# ── Strategy loop ─────────────────────────────────────────────────────────────

async def strategy_loop():
    saved     = database.load_state()
    portfolio = Portfolio(
        initial_capital = saved["initial_capital"],
        db              = database,
    )
    portfolio.restore(saved)
    log.info(
        f"Portafolio restaurado: capital=${portfolio.capital:.2f}, "
        f"trades históricos={len(portfolio.closed_trades)}"
    )

    obi_window     = deque(maxlen=WINDOW_SIZE)
    market_info    = None
    snap           = 0
    last_market_id = None
    error_streak   = 0

    while True:
        try:
            # ── Find / refresh market ─────────────────────────────────────────
            if market_info is None:
                state["status"] = "searching"
                await broadcast(state)
                log.info("Searching for active market...")
                market_info = await asyncio.to_thread(find_active_sol_market)
                if market_info is None:
                    state["error"] = "No active market found. Retrying in 15s..."
                    log.warning("No market found, retrying in 15s")
                    await asyncio.sleep(15)
                    continue
                state["error"] = None
                log.info(f"Found market: {market_info['question']}")

            # ── Detect new market cycle ───────────────────────────────────────
            if market_info["condition_id"] != last_market_id:
                last_market_id = market_info["condition_id"]
                obi_window.clear()
                snap = 0
                log.info(f"New market cycle: {market_info['question']}")
                if portfolio.active_trade:
                    portfolio.close_trade(
                        market_info["up_price"],
                        market_info["down_price"],
                    )

            # ── Get DUAL order book (UP + DOWN) ───────────────────────────────
            snap += 1
            up_ob, down_ob, err = await asyncio.to_thread(
                get_dual_book_metrics,
                market_info["up_token_id"],
                market_info["down_token_id"],
            )

            if err or up_ob is None:
                err_str = str(err) if err else ""
                if "404" in err_str or "No orderbook" in err_str:
                    if portfolio.active_trade:
                        portfolio.close_trade(
                            market_info["up_price"],
                            market_info["down_price"],
                        )
                    market_info = None
                    error_streak = 0
                    state["status"] = "searching"
                    state["error"]  = "Mercado expirado, buscando siguiente..."
                    await broadcast(state)
                    await asyncio.sleep(6)
                else:
                    error_streak += 1
                    wait = min(POLL_INTERVAL * (2 ** (error_streak - 1)), 60)
                    label = "Rate limit" if "429" in err_str else "Error"
                    log.warning(f"{label} (streak={error_streak}), backoff {wait:.0f}s: {err_str[:80]}")
                    state["status"] = "error"
                    state["error"]  = f"{label} — esperando {wait:.0f}s (intento {error_streak})"
                    await broadcast(state)
                    await asyncio.sleep(wait)
                continue

            error_streak = 0

            # ── Precios reales desde el libro ─────────────────────────────────
            up_ask = up_ob["best_ask"]
            up_bid = up_ob["best_bid"]
            book_valid = up_ask > 0.005 and up_bid > 0.005 and up_ask > up_bid

            # DOWN token tiene su propio libro; si lo tenemos, usamos sus precios reales
            if down_ob:
                down_ask = down_ob["best_ask"]
                down_bid = down_ob["best_bid"]
                # Actualizar precios del mercado con mid real de cada libro
                market_info["up_price"]   = round((up_bid + up_ask) / 2, 4)
                market_info["down_price"] = round((down_bid + down_ask) / 2, 4)
            else:
                # Fallback: complemento del UP
                down_ask = round(1 - up_bid, 4) if book_valid else None
                down_bid = round(1 - up_ask, 4) if book_valid else None
                market_info["up_price"]   = up_ob["vwap_mid"]
                market_info["down_price"] = round(1 - up_ob["vwap_mid"], 4)

            top_asks_up = up_ob.get("top_asks", [])
            top_bids_up = up_ob.get("top_bids", [])

            if down_ob:
                top_asks_down = down_ob.get("top_asks", [])
                top_bids_down = down_ob.get("top_bids", [])
            else:
                # Complemento del UP book (precio = 1 - precio_UP)
                top_asks_down = [(round(1 - p, 4), s) for p, s in top_bids_up] if book_valid else []
                top_bids_down = [(round(1 - p, 4), s) for p, s in top_asks_up] if book_valid else []

            # ── OBI combinado ─────────────────────────────────────────────────
            combined_obi = compute_combined_obi(up_ob, down_ob)
            obi_window.append(combined_obi)

            # ── Signal con EMA + depth_pressure ──────────────────────────────
            signal = compute_signal(
                combined_obi,
                list(obi_window),
                OBI_THRESHOLD,
                up_ob=up_ob,
                down_ob=down_ob,
            )

            # ── Filtro duro de spread (por dirección) ─────────────────────────
            if book_valid and signal.get("label") not in ("NEUTRAL",):
                sig_label = signal.get("label", "")
                if "UP" in sig_label:
                    sp = (up_ask - up_bid) / up_ask if up_ask > 0 else 1.0
                    if sp > 0.10:
                        log.info(f"Ignorando {sig_label}: spread UP {sp*100:.1f}% > 10%")
                        signal["label"] = "NEUTRAL"
                elif "DOWN" in sig_label and down_ob:
                    sp = (down_ask - down_bid) / down_ask if down_ask and down_ask > 0 else 1.0
                    if sp > 0.10:
                        log.info(f"Ignorando {sig_label}: spread DOWN {sp*100:.1f}% > 10%")
                        signal["label"] = "NEUTRAL"

            # ── Simulation ────────────────────────────────────────────────────
            secs_left = seconds_remaining(market_info)

            # Smart exits
            if portfolio.active_trade and secs_left is not None and secs_left > 0:
                reason = portfolio.check_exits(
                    signal,
                    market_info["up_price"],
                    market_info["down_price"],
                    secs_left,
                )
                if reason:
                    at = portfolio.active_trade
                    exit_bid_price = None
                    if book_valid:
                        if at.direction == "UP" and top_bids_up:
                            d = _walk_bids_for_exit(top_bids_up, at.shares)
                            if d["shares_sold"] > 0:
                                exit_bid_price = d["avg_price"]
                        elif at.direction == "DOWN" and top_bids_down:
                            d = _walk_bids_for_exit(top_bids_down, at.shares)
                            if d["shares_sold"] > 0:
                                exit_bid_price = d["avg_price"]
                    exited = portfolio.exit_at_market_price(
                        market_info["up_price"],
                        market_info["down_price"],
                        reason,
                        exit_bid_price=exit_bid_price,
                    )
                    if exited:
                        log.info(
                            f"Smart exit [{reason}] #{exited.id}: "
                            f"entry={exited.entry_price:.4f} "
                            f"exit={exited.exit_price:.4f} "
                            f"pnl={exited.pnl:+.4f}"
                        )

            # Try entering a trade (solo si quedan >60s)
            if secs_left is not None and secs_left > 60:
                bet_size = round(portfolio.capital * 0.02, 2)
                entry_depth_up   = _walk_asks_for_entry(top_asks_up, bet_size) \
                                   if book_valid and top_asks_up and bet_size > 0 else None
                entry_depth_down = _walk_asks_for_entry(top_asks_down, bet_size) \
                                   if book_valid and top_asks_down and bet_size > 0 else None

                up_bid_entry   = up_bid if book_valid else None
                down_bid_entry = down_bid if book_valid and down_ob else None

                portfolio.consider_entry(
                    signal,
                    market_info["question"],
                    market_info["up_price"],
                    market_info["down_price"],
                    entry_depth_up=entry_depth_up,
                    entry_depth_down=entry_depth_down,
                    up_bid=up_bid_entry,
                    down_bid=down_bid_entry,
                )

            # Emergency binary close
            if secs_left is not None and secs_left < 5 and portfolio.active_trade:
                portfolio.close_trade(
                    market_info["up_price"],
                    market_info["down_price"],
                )

            if secs_left is not None and secs_left <= 0:
                market_info = None
                await asyncio.sleep(5)
                continue

            # Refresh accepting_orders status
            if snap % 5 == 0:
                fresh = await asyncio.to_thread(
                    fetch_clob_market, market_info["condition_id"]
                )
                if fresh:
                    market_info["accepting_orders"] = bool(fresh.get("accepting_orders"))

            # ── Build state snapshot ──────────────────────────────────────────
            portfolio_stats = portfolio.stats(
                market_info["up_price"],
                market_info["down_price"],
            )

            # Realistic exit depth for active trade (display)
            if portfolio.active_trade and portfolio_stats.get("active_trade"):
                at = portfolio.active_trade
                exit_depth = None
                if book_valid:
                    if at.direction == "UP" and top_bids_up:
                        exit_depth = _walk_bids_for_exit(top_bids_up, at.shares)
                    elif at.direction == "DOWN" and top_bids_down:
                        exit_depth = _walk_bids_for_exit(top_bids_down, at.shares)

                if exit_depth and exit_depth["shares_sold"] > 0:
                    sim_upnl  = portfolio_stats["active_trade"].get("unrealized_pnl", 0)
                    real_upnl = round(exit_depth["proceeds"] - at.bet_size, 4)
                    portfolio_stats["active_trade"]["real_current_price"]  = exit_depth["avg_price"]
                    portfolio_stats["active_trade"]["real_unrealized_pnl"] = real_upnl
                    portfolio_stats["active_trade"]["spread_impact"]       = round(sim_upnl - real_upnl, 4)
                    portfolio_stats["active_trade"]["exit_depth"]          = exit_depth
                else:
                    portfolio_stats["active_trade"]["real_current_price"]  = None
                    portfolio_stats["active_trade"]["real_unrealized_pnl"] = None
                    portfolio_stats["active_trade"]["spread_impact"]       = None
                    portfolio_stats["active_trade"]["exit_depth"]          = None

                portfolio_stats["active_trade"]["real_entry_cost"] = (
                    up_ask if at.direction == "UP" else (down_ask or None)
                )

            state["status"]    = "running"
            state["error"]     = None
            state["snapshot"]  = snap
            state["timestamp"] = datetime.utcnow().isoformat() + "Z"
            state["market"]    = {
                **market_info,
                "seconds_remaining": round(secs_left, 1) if secs_left is not None else None,
                "up_ask":   up_ask,
                "up_bid":   up_bid,
                "down_ask": down_ask,
                "down_bid": down_bid,
            }
            # Exportamos ambos libros al frontend
            state["orderbook"]      = up_ob
            state["orderbook_down"] = down_ob or {}
            state["signal"]         = signal
            state["portfolio"]      = portfolio_stats

            await broadcast(state)

        except Exception as exc:
            error_streak += 1
            wait = min(POLL_INTERVAL * (2 ** (error_streak - 1)), 60)
            log.exception(f"Strategy loop error (streak={error_streak}): {exc}")
            state["status"] = "error"
            state["error"]  = str(exc)
            market_info = None
            await broadcast(state)
            await asyncio.sleep(wait)
            continue

        await asyncio.sleep(POLL_INTERVAL)


# ── App lifespan ──────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    database.init_db()
    log.info(f"DB path: {database.db_path()}")
    task = asyncio.create_task(strategy_loop())
    yield
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


app       = FastAPI(title="Polymarket Strategy v3 – Dual OBI", lifespan=lifespan)
templates = Jinja2Templates(directory="templates")


# ── Routes ────────────────────────────────────────────────────────────────────

@app.get("/health")
async def health():
    return {
        "status":    "ok",
        "db_path":   database.db_path(),
        "data_dir":  database.DATA_DIR,
        "strategy":  state.get("status"),
        "market":    state.get("market", {}).get("question"),
        "snapshot":  state.get("snapshot"),
        "error":     state.get("error"),
    }


@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    return templates.TemplateResponse("index.html", {"request": request})


@app.get("/api/state")
async def get_state():
    return state


@app.get("/api/trades")
async def get_all_trades():
    saved = await asyncio.to_thread(database.load_state)
    return {
        "capital":         saved["capital"],
        "initial_capital": saved["initial_capital"],
        "total_trades":    saved["trade_counter"],
        "trades":          [t.to_dict() for t in saved["closed_trades"]],
        "db_path":         database.db_path(),
    }


@app.websocket("/ws")
async def ws_endpoint(websocket: WebSocket):
    await websocket.accept()
    connected.add(websocket)
    try:
        await websocket.send_json(state)
        while True:
            await asyncio.wait_for(websocket.receive_text(), timeout=30)
    except (WebSocketDisconnect, asyncio.TimeoutError, Exception):
        connected.discard(websocket)


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    uvicorn.run("app:app", host="0.0.0.0", port=PORT, log_level="warning")
