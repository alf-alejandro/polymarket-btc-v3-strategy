"""
app.py — FastAPI server + strategy loop corregido para Underdog $1
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
)
from price_feed import PriceFeed
from simulator import Portfolio
import db as database

# ── Config ────────────────────────────────────────────────────────────────────
POLL_INTERVAL = 3.0
PORT          = int(os.environ.get("PORT", "8000"))

# ── Shared state ──────────────────────────────────────────────────────────────
connected: set[WebSocket] = set()
state: dict = {"market": {}, "status": "initializing", "error": None}

async def broadcast(data: dict):
    dead: set[WebSocket] = set()
    for ws in list(connected):
        try:
            await ws.send_json(data)
        except Exception:
            dead.add(ws)
    connected.difference_update(dead)

# ── Strategy loop ─────────────────────────────────────────────────────────────

async def strategy_loop():
    saved     = database.load_state()
    portfolio = Portfolio(db=database)
    portfolio.restore(saved)
    log.info(f"Bot iniciado. Capital=${portfolio.capital:.2f}")

    price_feed = PriceFeed()
    obi_window = deque(maxlen=6)
    market_info = None
    last_market_id = None

    while True:
        try:
            if market_info is None:
                state["status"] = "searching"
                market_info = await asyncio.to_thread(find_active_sol_market)
                if market_info is None:
                    await asyncio.sleep(15)
                    continue

            if market_info["condition_id"] != last_market_id:
                last_market_id = market_info["condition_id"]
                log.info(f"Nuevo mercado: {market_info['question']}")
                if portfolio.active_trade:
                    portfolio.close_trade(market_info["up_price"], market_info["down_price"])

            up_ob, down_ob, err = await asyncio.to_thread(
                get_dual_book_metrics,
                market_info["up_token_id"],
                market_info["down_token_id"],
            )

            if err:
                market_info = None
                await asyncio.sleep(5)
                continue

            # Actualizar precios
            market_info["up_price"] = up_ob["vwap_mid"]
            market_info["down_price"] = round(1 - up_ob["vwap_mid"], 4)

            # ── Tiempo restante ──
            secs_left = seconds_remaining(market_info)

            # ── Entrada Underdog ──
            if secs_left is not None and not portfolio.active_trade:
                # Corregido: Apuesta fija de $1 para evitar error de trade_pct
                entered = portfolio.consider_entry(
                    {}, # Signal vacío
                    market_info["question"],
                    market_info["up_price"],
                    market_info["down_price"],
                    secs_left=secs_left
                )
                if entered:
                    t = portfolio.active_trade
                    log.info(f"ENTRADA Underdog #{t.id}: {t.direction} precio={t.entry_price:.4f} secs_left={secs_left:.0f}")

            # ── Cierre por expiración (simulado) ──
            if secs_left is not None and secs_left < 5 and portfolio.active_trade:
                closed = portfolio.close_trade(market_info["up_price"], market_info["down_price"])
                if closed:
                    log.info(f"FIN DE MERCADO #{closed.id}: {closed.status} P&L={closed.pnl:+.4f}")

            if secs_left is not None and secs_left <= 0:
                market_info = None
                await asyncio.sleep(5)
                continue

            state["market"] = market_info
            await broadcast(state)
            await asyncio.sleep(POLL_INTERVAL)

        except Exception as e:
            log.error(f"Error en loop: {e}")
            await asyncio.sleep(5)

# ── FastAPI setup ─────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    asyncio.create_task(strategy_loop())
    yield

app = FastAPI(lifespan=lifespan)

@app.get("/")
async def root():
    return {"status": "running", "strategy": "Underdog Last Minute"}

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    connected.add(websocket)
    try:
        while True:
            await websocket.receive_text()
    except:
        connected.remove(websocket)

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=PORT)
