import asyncio
import logging
import os
from contextlib import asynccontextmanager
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from starlette.requests import Request
import uvicorn

from strategy_core import find_active_sol_market, get_dual_book_metrics, seconds_remaining
from simulator import Portfolio
import db as database

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("strategy")

# ── Config ──
POLL_INTERVAL = 2.0
templates = Jinja2Templates(directory="templates")
connected: set[WebSocket] = set()
state: dict = {"market": {}, "status": "initializing", "portfolio": {}}

async def broadcast(data: dict):
    for ws in list(connected):
        try: await ws.send_json(data)
        except: connected.discard(ws)

async def strategy_loop():
    saved = database.load_state()
    portfolio = Portfolio(db=database)
    portfolio.restore(saved)
    
    market_info = None
    while True:
        try:
            if market_info is None:
                state["status"] = "searching"
                market_info = await asyncio.to_thread(find_active_sol_market)
                if not market_info:
                    await asyncio.sleep(10); continue

            # Actualizar precios y tiempo
            up_ob, _, err = await asyncio.to_thread(get_dual_book_metrics, market_info["up_token_id"], market_info["down_token_id"])
            if not err:
                market_info["up_price"] = up_ob["vwap_mid"]
                market_info["down_price"] = round(1 - up_ob["vwap_mid"], 4)

            secs_left = seconds_remaining(market_info)
            market_info["seconds_remaining"] = secs_left # Fix NaN

            # Lógica de trades
            portfolio.consider_entry({}, market_info["question"], market_info["up_price"], market_info["down_price"], secs_left=secs_left)
            
            if secs_left is not None and secs_left < 5 and portfolio.active_trade:
                portfolio.close_trade(market_info["up_price"], market_info["down_price"])

            # Enviar estado completo
            state["status"] = "active"
            state["market"] = market_info
            state["portfolio"] = {
                "capital": portfolio.capital,
                "active_trade": portfolio.active_trade.to_dict() if portfolio.active_trade else None,
                "history": [t.to_dict() for t in reversed(portfolio.closed_trades[-10:])]
            }
            await broadcast(state)
            await asyncio.sleep(POLL_INTERVAL)

            if secs_left is not None and secs_left <= 0:
                market_info = None
                await asyncio.sleep(5)

        except Exception as e:
            log.error(f"Error: {e}"); await asyncio.sleep(5)

@asynccontextmanager
async def lifespan(app: FastAPI):
    asyncio.create_task(strategy_loop()); yield

app = FastAPI(lifespan=lifespan)

@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    return templates.TemplateResponse("index.html", {"request": request})

@app.websocket("/ws")
async def ws_endpoint(websocket: WebSocket):
    await websocket.accept(); connected.add(websocket)
    try:
        await websocket.send_json(state)
        while True: await websocket.receive_text()
    except WebSocketDisconnect: connected.discard(websocket)

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))
