"""
strategy_core.py — Market discovery + order book metrics + signal engine

Mejoras v3:
  - OBI combinado UP+DOWN (no solo UP)
  - Señal con momentum: peso mayor a los últimos snaps
  - Detección de presión real de liquidez (volume-weighted OBI)
  - Spread filter embebido en la señal

Configurable via env vars:
  SYMBOL = SOL | BTC   (default: SOL)
"""

import os
import time
import requests
from datetime import datetime, timezone
from collections import deque
from py_clob_client.client import ClobClient

CLOB_HOST   = "https://clob.polymarket.com"
GAMMA_API   = "https://gamma-api.polymarket.com"
SLOT_ORIGIN = 1771778100   # slot anchor compartido SOL y BTC (Feb 22 2026)
SLOT_STEP   = 300          # 5 minutos
TOP_LEVELS  = 15

SYMBOL      = os.environ.get("SYMBOL", "SOL").upper()
SLUG_PREFIX = "btc-updown-5m" if SYMBOL == "BTC" else "sol-updown-5m"
MARKET_NAME = "Bitcoin" if SYMBOL == "BTC" else "Solana"


# ── Market discovery ──────────────────────────────────────────────────────────

def get_current_slot_ts():
    now     = int(time.time())
    elapsed = (now - SLOT_ORIGIN) % SLOT_STEP
    return now - elapsed


def fetch_gamma_market(slug: str):
    try:
        r = requests.get(f"{GAMMA_API}/markets", params={"slug": slug}, timeout=8)
        r.raise_for_status()
        data = r.json()
        return data[0] if isinstance(data, list) and data else None
    except Exception:
        return None


def fetch_clob_market(condition_id: str):
    try:
        r = requests.get(f"{CLOB_HOST}/markets/{condition_id}", timeout=8)
        r.raise_for_status()
        return r.json()
    except Exception:
        return None


def build_market_info(gamma_m, clob_m) -> dict | None:
    tokens = clob_m.get("tokens", [])
    if len(tokens) < 2:
        return None

    up_t   = next((t for t in tokens if "up"   in (t.get("outcome") or "").lower()), tokens[0])
    down_t = next((t for t in tokens if "down" in (t.get("outcome") or "").lower()), tokens[1])

    return {
        "condition_id":     clob_m.get("condition_id"),
        "question":         clob_m.get("question", "SOL Up/Down 5min"),
        "end_date":         gamma_m.get("endDate") or clob_m.get("end_date_iso", ""),
        "market_slug":      clob_m.get("market_slug", ""),
        "accepting_orders": bool(clob_m.get("accepting_orders")),
        "up_token_id":      up_t["token_id"],
        "up_outcome":       up_t.get("outcome", "Up"),
        "up_price":         float(up_t.get("price") or 0.5),
        "down_token_id":    down_t["token_id"],
        "down_outcome":     down_t.get("outcome", "Down"),
        "down_price":       float(down_t.get("price") or 0.5),
    }


def _order_book_live(token_id: str) -> bool:
    try:
        r = requests.get(
            f"{CLOB_HOST}/book",
            params={"token_id": token_id},
            timeout=5,
        )
        return r.status_code == 200
    except Exception:
        return False


def find_active_sol_market() -> dict | None:
    base = get_current_slot_ts()
    for offset in [0, 1, 2, -1]:
        ts   = base + offset * SLOT_STEP
        slug = f"{SLUG_PREFIX}-{ts}"
        gm   = fetch_gamma_market(slug)
        if not gm:
            continue
        cid = gm.get("conditionId")
        if not cid:
            continue
        cm = fetch_clob_market(cid)
        if not cm:
            continue
        info = build_market_info(gm, cm)
        if not info:
            continue
        if _order_book_live(info["up_token_id"]):
            return info
    return None


def seconds_remaining(market_info: dict) -> float | None:
    end_raw = market_info.get("end_date", "")
    if not end_raw:
        return None
    try:
        end_dt = datetime.fromisoformat(end_raw.replace("Z", "+00:00"))
        diff   = (end_dt - datetime.now(timezone.utc)).total_seconds()
        return max(0.0, diff)
    except Exception:
        return None


# ── Order book ────────────────────────────────────────────────────────────────

_clob_client = None

def get_clob_client() -> ClobClient:
    global _clob_client
    if _clob_client is None:
        _clob_client = ClobClient(CLOB_HOST)
    return _clob_client


def get_order_book_metrics(token_id: str, top_n: int = TOP_LEVELS) -> tuple[dict | None, str | None]:
    try:
        ob = get_clob_client().get_order_book(token_id)
    except Exception as e:
        return None, str(e)

    bids = sorted(ob.bids or [], key=lambda x: float(x.price), reverse=True)[:top_n]
    asks = sorted(ob.asks or [], key=lambda x: float(x.price))[:top_n]

    bid_vol = sum(float(b.size) for b in bids)
    ask_vol = sum(float(a.size) for a in asks)
    total   = bid_vol + ask_vol
    obi     = (bid_vol - ask_vol) / total if total > 0 else 0.0

    best_bid = float(bids[0].price) if bids else 0.0
    best_ask = float(asks[0].price) if asks else 0.0
    spread   = round(best_ask - best_bid, 4)

    if total > 0:
        bvwap = sum(float(b.price) * float(b.size) for b in bids) / bid_vol if bid_vol > 0 else 0
        avwap = sum(float(a.price) * float(a.size) for a in asks) / ask_vol if ask_vol > 0 else 0
        vwap_mid = (bvwap * bid_vol + avwap * ask_vol) / total
    else:
        vwap_mid = (best_bid + best_ask) / 2

    # ── Presión de profundidad: cuánto peso tienen los top 3 niveles vs el resto
    top3_bid = sum(float(b.size) for b in bids[:3])
    top3_ask = sum(float(a.size) for a in asks[:3])
    depth_pressure = (top3_bid - top3_ask) / (top3_bid + top3_ask) if (top3_bid + top3_ask) > 0 else 0.0

    return {
        "bid_volume":     round(bid_vol, 2),
        "ask_volume":     round(ask_vol, 2),
        "total_volume":   round(total, 2),
        "obi":            round(obi, 4),
        "depth_pressure": round(depth_pressure, 4),   # NEW: presión en top 3 niveles
        "best_bid":       round(best_bid, 4),
        "best_ask":       round(best_ask, 4),
        "spread":         spread,
        "spread_pct":     round(spread / best_ask, 4) if best_ask > 0 else 1.0,
        "vwap_mid":       round(vwap_mid, 4),
        "num_bids":       len(ob.bids or []),
        "num_asks":       len(ob.asks or []),
        "top_bids":       [(round(float(b.price), 4), round(float(b.size), 2)) for b in bids[:8]],
        "top_asks":       [(round(float(a.price), 4), round(float(a.size), 2)) for a in asks[:8]],
    }, None


def get_dual_book_metrics(up_token_id: str, down_token_id: str, top_n: int = TOP_LEVELS) -> tuple[dict | None, dict | None, str | None]:
    """
    Lee ambos libros (UP y DOWN) y retorna métricas individuales.
    El OBI combinado es la media ponderada de ambos tokens.
    DOWN token OBI positivo = presión DOWN en el mercado.
    """
    up_ob, err = get_order_book_metrics(up_token_id, top_n)
    if err:
        return None, None, err

    down_ob, err = get_order_book_metrics(down_token_id, top_n)
    # Si el book DOWN falla, usamos solo UP (modo degradado)
    if err or down_ob is None:
        return up_ob, None, None

    return up_ob, down_ob, None


# ── Signal engine ─────────────────────────────────────────────────────────────

def compute_combined_obi(up_ob: dict, down_ob: dict | None) -> float:
    """
    OBI combinado real:
      - up_ob.obi  > 0  → más compradores de UP  → presión alcista
      - down_ob.obi > 0 → más compradores de DOWN → presión bajista
    
    Resultado final: positivo = presión UP, negativo = presión DOWN.
    
    Ponderamos por volumen total de cada libro para dar más peso
    al libro más líquido.
    """
    up_obi = up_ob["obi"]
    up_vol  = up_ob["total_volume"]

    if down_ob is None:
        # Modo degradado: solo UP book
        return up_obi

    down_obi = down_ob["obi"]
    down_vol  = down_ob["total_volume"]

    total_vol = up_vol + down_vol
    if total_vol == 0:
        return 0.0

    # Presión neta: UP compradores vs DOWN compradores
    # up_obi positivo = favorece UP
    # down_obi positivo = favorece DOWN (lo restamos)
    weighted = (up_obi * up_vol - down_obi * down_vol) / total_vol
    return round(weighted, 4)


def compute_signal(
    combined_obi: float,
    obi_window: list[float],
    threshold: float,
    up_ob: dict = None,
    down_ob: dict = None,
) -> dict:
    """
    Señal mejorada:
    1. OBI combinado (UP - DOWN) ponderado por volumen
    2. Media exponencial del historial (más peso a snaps recientes)
    3. Boost por depth_pressure (liquidez concentrada en top 3 niveles)
    4. Penalización por spread alto
    """
    n = len(obi_window)

    if n == 0:
        ema_obi = combined_obi
    else:
        # EMA simple: alpha = 2/(n+1), pero con ventana pequeña usamos alpha fijo
        alpha = 0.35  # 35% peso al nuevo valor, 65% al histórico → reacciona en ~3-4 snaps
        ema_obi = obi_window[-1]
        for val in reversed(obi_window[:-1]):
            ema_obi = alpha * val + (1 - alpha) * ema_obi

    # Señal base: 55% snapshot actual + 45% EMA
    base_signal = round(0.55 * combined_obi + 0.45 * ema_obi, 4)

    # Boost por depth_pressure si ambos libros disponibles
    dp_boost = 0.0
    if up_ob and down_ob:
        up_dp   = up_ob.get("depth_pressure", 0)
        down_dp = down_ob.get("depth_pressure", 0)
        # depth_pressure neta: si los top 3 del UP tienen más bid que ask, y
        # los top 3 del DOWN tienen más ask que bid → señal UP reforzada
        net_dp  = up_dp - down_dp
        dp_boost = net_dp * 0.10   # máx ±10% del signal
    elif up_ob:
        dp_boost = up_ob.get("depth_pressure", 0) * 0.10

    combined = round(base_signal + dp_boost, 4)

    # Penalización por spread: si spread > umbral, reducir señal
    spread_penalty = 1.0
    if up_ob:
        sp = up_ob.get("spread_pct", 0)
        if sp > 0.08:   # spread > 8%: empezamos a penalizar
            spread_penalty = max(0.3, 1.0 - (sp - 0.08) * 5)
    combined = round(combined * spread_penalty, 4)

    abs_c = abs(combined)

    if combined > threshold:
        conf  = min(int(50 + (abs_c / threshold) * 35), 95)
        label = "STRONG UP" if combined > threshold * 1.8 else "UP"
        color = "green"
    elif combined < -threshold:
        conf  = min(int(50 + (abs_c / threshold) * 35), 95)
        label = "STRONG DOWN" if combined < -threshold * 1.8 else "DOWN"
        color = "red"
    else:
        label = "NEUTRAL"
        color = "yellow"
        conf  = 50

    return {
        "label":          label,
        "color":          color,
        "confidence":     conf,
        "obi_combined":   combined_obi,
        "obi_ema":        round(ema_obi, 4),
        "dp_boost":       round(dp_boost, 4),
        "spread_penalty": round(spread_penalty, 4),
        "combined":       combined,
        "history":        list(obi_window)[-20:],
        "threshold":      threshold,
        # Retrocompatibilidad con código que usa obi_now / obi_avg
        "obi_now":        combined_obi,
        "obi_avg":        round(ema_obi, 4),
    }
