"""
app.py — FastAPI server + loop de estrategia corregido (Underdog $1)
"""
import asyncio
import logging
import os
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
    seconds_remaining,
)
from simulator import Portfolio
import db as database

# ── Config ────────────────────────────────────────────────────────────────────
POLL_INTERVAL = 2.0
PORT          = int(os.environ.get("PORT", "8000"))
templates     = Jinja2Templates(directory="templates")

# ── Estado Compartido ─────────────────────────────────────────────────────────
connected: set[WebSocket] = set()
state: dict = {"market": {}, "status": "initializing", "portfolio": {}}

async def broadcast(data: dict):
    dead = set()
    for ws in list(connected):
        try:
            await ws.send_json(data)
        except:
            dead.add(ws)
    connected.difference_update(dead)

# ── Loop de Estrategia ────────────────────────────────────────────────────────
async def strategy_loop():
    saved = database.load_state()
    portfolio = Portfolio(db=database)
    portfolio.restore(saved)
    log.info(f"Sistema iniciado. Capital base: ${portfolio.capital}")

    market_info = None
    last_market_id = None

    while True:
        try:
            if market_info is None:
                state["status"] = "searching"
                market_info = await asyncio.to_thread(find_active_sol_market)
                if market_info is None:
                    await asyncio.sleep(10)
                    continue

            if market_info["condition_id"] != last_market_id:
                last_market_id = market_info["condition_id"]
                log.info(f"Monitoreando: {market_info['question']}")

            # Actualizar Precios
            up_ob, down_ob, err = await asyncio.to_thread(
                get_dual_book_metrics,
                market_info["up_token_id"],
                market_info["down_token_id"]
            )

            if not err:
                market_info["up_price"] = up_ob["vwap_mid"]
                market_info["down_price"] = round(1 - up_ob["vwap_mid"], 4)

            # CALCULAR TIEMPO (Crucial para el Dashboard)
            secs_left = seconds_remaining(market_info)
            market_info["seconds_remaining"] = secs_left

            # Lógica de Entrada (Underdog)
            if secs_left is not None and not portfolio.active_trade:
                # bet_size fijo a 1.0 para evitar errores de cálculo
                entered = portfolio.consider_entry(
                    {}, 
                    market_info["question"],
                    market_info["up_price"],
                    market_info["down_price"],
                    secs_left=secs_left
                )
                if entered:
                    t = portfolio.active_trade
                    log.info(f"!!! COMPRA EJECUTADA !!! {t.direction} a {t.entry_price}")

            # Lógica de Cierre (Expiración)
            if secs_left is not None and secs_left < 5 and portfolio.active_trade:
                closed = portfolio.close_trade(market_info["up_price"], market_info["down_price"])
                if closed:
                    log.info(f"RESULTADO: {closed.status} | P&L: {closed.pnl}")

            # Preparar datos para el Dashboard
            state["status"] = "active"
            state["market"] = market_info
            state["portfolio"] = {
                "capital": portfolio.capital,
                "active_trade": portfolio.active_trade.to_dict() if portfolio.active_trade else None
            }

            await broadcast(state)
            await asyncio.sleep(POLL_INTERVAL)

            if secs_left is not None and secs_left <= 0:
                market_info = None
                await asyncio.sleep(5)

        except Exception as e:
            log.error(f"Error en loop: {e}")
            await asyncio.sleep(5)

# ── Servidor FastAPI ──────────────────────────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    asyncio.create_task(strategy_loop())
    yield

app = FastAPI(lifespan=lifespan)

@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    return templates.TemplateResponse("index.html", {"request": request})

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    connected.add(websocket)
    try:
        await websocket.send_json(state)
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        connected.remove(websocket)

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=PORT)
