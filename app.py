"""
app.py — FastAPI server: WebSocket broadcast + strategy background loop
v4: Dual book OBI + EMA signal + precio real de Binance (momentum + divergencia)
"""

import asyncio
import logging
import os
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
    compute_combined_obi,
    compute_signal,
    seconds_remaining,
    fetch_clob_market,
    SLOT_STEP,
)
from price_feed import PriceFeed
from simulator import Portfolio
import db as database

# ── Depth walk helpers ────────────────────────────────────────────────────────

def _walk_asks_for_entry(top_asks: list, bet_usdc: float) -> dict:
    remaining = bet_usdc
    shares = 0.0
    cost   = 0.0
    for price, size in top_asks:
        if remaining <= 0:
            break
        fill_usdc = min(remaining, price * size)
        shares   += fill_usdc / price
        cost     += fill_usdc
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
    proceeds  = 0.0
    for price, size in top_bids:
        if remaining <= 0:
            break
        fill      = min(remaining, size)
        proceeds += fill * price
        remaining -= fill
    sold = shares_to_sell - remaining
    avg  = proceeds / sold if sold > 0 else 0.0
    return {
        "shares_sold": round(sold, 4),
        "avg_price":   round(avg, 4),
        "proceeds":    round(proceeds, 4),
        "filled":      remaining <= 0.001,
        "unfilled":    round(max(0.0, remaining), 4),
        "fill_pct":    round(min(100.0, sold / shares_to_sell * 100), 1) if shares_to_sell > 0 else 0.0,
    }


# ── Config ────────────────────────────────────────────────────────────────────
POLL_INTERVAL = float(os.environ.get("POLL_INTERVAL", "3"))
OBI_THRESHOLD = float(os.environ.get("OBI_THRESHOLD", "0.18"))
WINDOW_SIZE   = int(os.environ.get("WINDOW_SIZE", "6"))
PORT          = int(os.environ.get("PORT", "8000"))

# ── Shared state ──────────────────────────────────────────────────────────────
connected: set[WebSocket] = set()

state: dict = {
    "market":    {},
    "orderbook": {},
    "orderbook_down": {},
    "signal":    {},
    "portfolio": {},
    "price_feed": {},
    "config": {
        "threshold":       OBI_THRESHOLD,
        "window_size":     WINDOW_SIZE,
        "poll_interval":   POLL_INTERVAL,
        "initial_capital": 100.0,
        "trade_pct":       0.02,
    },
    "status": "initializing",
    "error":  None,
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
    portfolio = Portfolio(initial_capital=saved["initial_capital"], db=database)
    portfolio.restore(saved)
    log.info(f"Portafolio restaurado: capital=${portfolio.capital:.2f}, trades={len(portfolio.closed_trades)}")

    # PriceFeed: instancia única, mantiene historial entre snaps
    price_feed = PriceFeed()
    log.info(f"PriceFeed iniciado para {price_feed.binance_sym}")

    obi_window     = deque(maxlen=WINDOW_SIZE)
    market_info    = None
    snap           = 0
    last_market_id = None
    error_streak   = 0

    while True:
        try:
            # ── Buscar mercado ────────────────────────────────────────────────
            if market_info is None:
                state["status"] = "searching"
                await broadcast(state)
                log.info("Searching for active market...")
                market_info = await asyncio.to_thread(find_active_sol_market)
                if market_info is None:
                    state["error"] = "No active market found. Retrying in 15s..."
                    await asyncio.sleep(15)
                    continue
                state["error"] = None
                log.info(f"Market found: {market_info['question']}")

            # ── Nuevo ciclo de mercado ────────────────────────────────────────
            if market_info["condition_id"] != last_market_id:
                last_market_id = market_info["condition_id"]
                obi_window.clear()
                snap = 0
                log.info(f"New market cycle: {market_info['question']}")
                if portfolio.active_trade:
                    portfolio.close_trade(market_info["up_price"], market_info["down_price"])

            # ── Order books (UP + DOWN) ───────────────────────────────────────
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
                        portfolio.close_trade(market_info["up_price"], market_info["down_price"])
                    market_info = None
                    error_streak = 0
                    state["status"] = "searching"
                    state["error"]  = "Mercado expirado, buscando siguiente..."
                    await broadcast(state)
                    await asyncio.sleep(6)
                else:
                    error_streak += 1
                    wait  = min(POLL_INTERVAL * (2 ** (error_streak - 1)), 60)
                    label = "Rate limit" if "429" in err_str else "Error"
                    log.warning(f"{label} streak={error_streak}, backoff {wait:.0f}s")
                    state["status"] = "error"
                    state["error"]  = f"{label} — esperando {wait:.0f}s"
                    await broadcast(state)
                    await asyncio.sleep(wait)
                continue

            error_streak = 0

            # ── Precios reales ────────────────────────────────────────────────
            up_ask     = up_ob["best_ask"]
            up_bid     = up_ob["best_bid"]
            book_valid = up_ask > 0.005 and up_bid > 0.005 and up_ask > up_bid

            if down_ob:
                down_ask = down_ob["best_ask"]
                down_bid = down_ob["best_bid"]
                market_info["up_price"]   = round((up_bid + up_ask) / 2, 4)
                market_info["down_price"] = round((down_bid + down_ask) / 2, 4)
            else:
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
                top_asks_down = [(round(1 - p, 4), s) for p, s in top_bids_up] if book_valid else []
                top_bids_down = [(round(1 - p, 4), s) for p, s in top_asks_up] if book_valid else []

            # ── OBI combinado ─────────────────────────────────────────────────
            combined_obi = compute_combined_obi(up_ob, down_ob)
            obi_window.append(combined_obi)

            # ── Precio real de Binance ────────────────────────────────────────
            # Lo corremos en thread para no bloquear el loop (request HTTP)
            price_snap = await asyncio.to_thread(
                price_feed.snapshot, market_info["up_price"]
            )
            state["price_feed"] = price_snap

            if price_snap.get("available"):
                log.debug(
                    f"Price {price_feed.binance_sym}: {price_snap['price']:.4f} | "
                    f"mom60={price_snap['mom_60s']:+.4%} | "
                    f"div={price_snap['divergence'].get('direction','?')} "
                    f"str={price_snap['divergence'].get('strength',0):.2f}"
                )

            # ── Señal combinada (OBI + precio) ────────────────────────────────
            signal = compute_signal(
                combined_obi,
                list(obi_window),
                OBI_THRESHOLD,
                up_ob=up_ob,
                down_ob=down_ob,
                price_snap=price_snap,
            )

            # ── Filtro duro de spread ─────────────────────────────────────────
            sig_label = signal.get("label", "NEUTRAL")
            if book_valid and sig_label not in ("NEUTRAL",):
                if "UP" in sig_label:
                    sp = (up_ask - up_bid) / up_ask if up_ask > 0 else 1.0
                    if sp > 0.10:
                        log.info(f"Ignorando {sig_label}: spread UP {sp*100:.1f}%")
                        signal["label"] = "NEUTRAL"
                elif "DOWN" in sig_label and down_ob:
                    sp = (down_ask - down_bid) / down_ask if down_ask and down_ask > 0 else 1.0
                    if sp > 0.10:
                        log.info(f"Ignorando {sig_label}: spread DOWN {sp*100:.1f}%")
                        signal["label"] = "NEUTRAL"

            # ── Tiempo restante ───────────────────────────────────────────────
            secs_left = seconds_remaining(market_info)

            # ── Exits ─────────────────────────────────────────────────────────
            if portfolio.active_trade and secs_left is not None and secs_left > 0:
                reason = portfolio.check_exits(
                    signal,
                    market_info["up_price"],
                    market_info["down_price"],
                    secs_left,
                )
                if reason == "PARTIAL_TP":
                    # Salida parcial: vender 50% al mejor bid disponible
                    at             = portfolio.active_trade
                    exit_bid_price = None
                    if book_valid:
                        bids_to_walk = top_bids_up if at.direction == "UP" else top_bids_down
                        if bids_to_walk:
                            d = _walk_bids_for_exit(bids_to_walk, at.shares_remaining * 0.5)
                            if d["shares_sold"] > 0:
                                exit_bid_price = d["avg_price"]
                    partial_pnl = portfolio.apply_partial_exit(
                        market_info["up_price"],
                        market_info["down_price"],
                        exit_bid_price=exit_bid_price,
                    )
                    log.info(
                        f"PARTIAL_TP #{at.id}: precio={exit_bid_price or 'mid':.4f} "
                        f"pnl_parcial={partial_pnl:+.4f} | "
                        f"shares restantes={at.shares_remaining:.4f} → esperando resolución"
                    )
                elif reason:
                    # Salida total (SL o LATE)
                    at             = portfolio.active_trade
                    exit_bid_price = None
                    if book_valid:
                        bids_to_walk = top_bids_up if at.direction == "UP" else top_bids_down
                        if bids_to_walk:
                            shares_to_walk = at.shares_remaining if at.partial_exit_done else at.shares
                            d = _walk_bids_for_exit(bids_to_walk, shares_to_walk)
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
                            f"Exit [{reason}] #{exited.id}: "
                            f"entry={exited.entry_price:.4f} exit={exited.exit_price:.4f} "
                            f"pnl={exited.pnl:+.4f}"
                        )

            # ── Entry: mean reversion — buscar token barato en zona 0.15–0.35 ──
            # El filtro de tiempo y precio está dentro de consider_entry
            if secs_left is not None and not portfolio.active_trade:
                bet_size         = round(portfolio.capital * 0.04, 2)
                entry_depth_up   = _walk_asks_for_entry(top_asks_up, bet_size) \
                                   if book_valid and top_asks_up and bet_size > 0 else None
                entry_depth_down = _walk_asks_for_entry(top_asks_down, bet_size) \
                                   if book_valid and top_asks_down and bet_size > 0 else None

                entered = portfolio.consider_entry(
                    signal,
                    market_info["question"],
                    market_info["up_price"],
                    market_info["down_price"],
                    secs_left=secs_left,
                    entry_depth_up=entry_depth_up,
                    entry_depth_down=entry_depth_down,
                    up_bid=up_bid if book_valid else None,
                    down_bid=down_bid if book_valid and down_ob else None,
                )
                if entered:
                    t = portfolio.active_trade
                    log.info(
                        f"ENTRADA Mean Reversion #{t.id}: {t.direction} "
                        f"precio={t.entry_price:.4f} shares={t.shares:.2f} "
                        f"secs_left={secs_left:.0f} target={0.47}"
                    )

            # Emergency close
            if secs_left is not None and secs_left < 5 and portfolio.active_trade:
                portfolio.close_trade(market_info["up_price"], market_info["down_price"])

            if secs_left is not None and secs_left <= 0:
                market_info = None
                await asyncio.sleep(5)
                continue

            # Refresh accepting_orders cada 5 snaps
            if snap % 5 == 0:
                fresh = await asyncio.to_thread(fetch_clob_market, market_info["condition_id"])
                if fresh:
                    market_info["accepting_orders"] = bool(fresh.get("accepting_orders"))

            # ── Portfolio stats ───────────────────────────────────────────────
            portfolio_stats = portfolio.stats(market_info["up_price"], market_info["down_price"])

            if portfolio.active_trade and portfolio_stats.get("active_trade"):
                at = portfolio.active_trade
                exit_depth = None
                if book_valid:
                    bids_to_walk = top_bids_up if at.direction == "UP" else top_bids_down
                    if bids_to_walk:
                        exit_depth = _walk_bids_for_exit(bids_to_walk, at.shares)

                if exit_depth and exit_depth["shares_sold"] > 0:
                    sim_upnl  = portfolio_stats["active_trade"].get("unrealized_pnl", 0)
                    real_upnl = round(exit_depth["proceeds"] - at.bet_size, 4)
                    portfolio_stats["active_trade"].update({
                        "real_current_price":  exit_depth["avg_price"],
                        "real_unrealized_pnl": real_upnl,
                        "spread_impact":       round(sim_upnl - real_upnl, 4),
                        "exit_depth":          exit_depth,
                    })
                else:
                    portfolio_stats["active_trade"].update({
                        "real_current_price":  None,
                        "real_unrealized_pnl": None,
                        "spread_impact":       None,
                        "exit_depth":          None,
                    })

                portfolio_stats["active_trade"]["real_entry_cost"] = (
                    up_ask if at.direction == "UP" else (down_ask or None)
                )

            # ── Broadcast state ───────────────────────────────────────────────
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
            market_info     = None
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


app       = FastAPI(title="Polymarket Strategy v4 – Dual OBI + Price Feed", lifespan=lifespan)
templates = Jinja2Templates(directory="templates")


# ── Routes ────────────────────────────────────────────────────────────────────

@app.get("/health")
async def health():
    pf = state.get("price_feed", {})
    return {
        "status":          "ok",
        "db_path":         database.db_path(),
        "strategy":        state.get("status"),
        "market":          state.get("market", {}).get("question"),
        "snapshot":        state.get("snapshot"),
        "error":           state.get("error"),
        "price_feed":      {
            "available":  pf.get("available", False),
            "price":      pf.get("price"),
            "symbol":     pf.get("symbol"),
            "mom_60s":    pf.get("mom_60s"),
        },
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
