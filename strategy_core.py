"""
strategy_core.py — Market discovery + order book metrics + signal engine

v4: Señal combinada OBI + momentum precio real (BTC/SOL de Binance)

La señal final combina 3 fuentes:
  1. OBI combinado UP+DOWN (presión en el libro de órdenes)
  2. Momentum del precio real (Binance spot, últimos 30/60/90s)
  3. Divergencia: diferencia entre precio implícito del momentum y precio del token

La divergencia es la señal más poderosa: si BTC subió 0.2% en 60s pero el
token UP todavía está en 0.47, hay un lag de información que podemos capturar.

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
SLOT_ORIGIN = 1771778100
SLOT_STEP   = 300
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
        r = requests.get(f"{CLOB_HOST}/book", params={"token_id": token_id}, timeout=5)
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
        bvwap    = sum(float(b.price) * float(b.size) for b in bids) / bid_vol if bid_vol > 0 else 0
        avwap    = sum(float(a.price) * float(a.size) for a in asks) / ask_vol if ask_vol > 0 else 0
        vwap_mid = (bvwap * bid_vol + avwap * ask_vol) / total
    else:
        vwap_mid = (best_bid + best_ask) / 2

    top3_bid       = sum(float(b.size) for b in bids[:3])
    top3_ask       = sum(float(a.size) for a in asks[:3])
    depth_pressure = (top3_bid - top3_ask) / (top3_bid + top3_ask) if (top3_bid + top3_ask) > 0 else 0.0

    return {
        "bid_volume":     round(bid_vol, 2),
        "ask_volume":     round(ask_vol, 2),
        "total_volume":   round(total, 2),
        "obi":            round(obi, 4),
        "depth_pressure": round(depth_pressure, 4),
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
    """Lee ambos libros (UP y DOWN). Si DOWN falla, modo degradado con solo UP."""
    up_ob, err = get_order_book_metrics(up_token_id, top_n)
    if err:
        return None, None, err
    down_ob, err = get_order_book_metrics(down_token_id, top_n)
    if err or down_ob is None:
        return up_ob, None, None
    return up_ob, down_ob, None


# ── OBI combinado ─────────────────────────────────────────────────────────────

def compute_combined_obi(up_ob: dict, down_ob: dict | None) -> float:
    """
    OBI neto ponderado por volumen de ambos libros.
    Positivo = presión UP. Negativo = presión DOWN.
    """
    up_obi  = up_ob["obi"]
    up_vol   = up_ob["total_volume"]

    if down_ob is None:
        return up_obi

    down_obi  = down_ob["obi"]
    down_vol   = down_ob["total_volume"]
    total_vol  = up_vol + down_vol

    if total_vol == 0:
        return 0.0

    return round((up_obi * up_vol - down_obi * down_vol) / total_vol, 4)


# ── Signal engine v4 ──────────────────────────────────────────────────────────

# Pesos de cada fuente. El precio real tiene mayor peso porque es
# información más "dura" que el order book (que se puede manipular).
_W_OBI = 0.35
_W_MOM = 0.30
_W_DIV = 0.35


def compute_signal(
    combined_obi: float,
    obi_window: list[float],
    threshold: float,
    up_ob: dict = None,
    down_ob: dict = None,
    price_snap: dict = None,   # snapshot del PriceFeed
) -> dict:
    """
    Señal v4: OBI + momentum de precio real + divergencia token vs precio.

    Si Binance no está disponible cae a modo solo-OBI automáticamente.
    """

    # ── 1. Componente OBI (EMA + depth pressure) ──────────────────────────────
    n = len(obi_window)
    if n == 0:
        ema_obi = combined_obi
    else:
        alpha   = 0.35
        ema_obi = obi_window[-1]
        for val in reversed(obi_window[:-1]):
            ema_obi = alpha * val + (1 - alpha) * ema_obi

    obi_component = 0.55 * combined_obi + 0.45 * ema_obi

    dp_boost = 0.0
    if up_ob and down_ob:
        net_dp   = up_ob.get("depth_pressure", 0) - down_ob.get("depth_pressure", 0)
        dp_boost = net_dp * 0.10
    elif up_ob:
        dp_boost = up_ob.get("depth_pressure", 0) * 0.10

    obi_component = round(obi_component + dp_boost, 4)

    # ── 2. Componentes de precio real ─────────────────────────────────────────
    mom_component   = 0.0
    div_component   = 0.0
    price_available = False
    price_data      = {}
    confirmed       = False

    if price_snap and price_snap.get("available"):
        price_available = True
        mom_30 = price_snap.get("mom_30s", 0.0)
        mom_60 = price_snap.get("mom_60s", 0.0)
        div    = price_snap.get("divergence", {})

        # Escalar momentum a rango ±1 (similar al OBI)
        scale       = {"SOL": 67.0, "BTC": 125.0}.get(SYMBOL, 80.0)
        raw_mom     = 0.4 * mom_30 + 0.6 * mom_60
        mom_component = round(max(-1.0, min(1.0, raw_mom * scale)), 4)

        # Divergencia: normalizada a ±1 por strength y dirección
        div_strength  = div.get("strength", 0.0)
        div_direction = div.get("direction", "NEUTRAL")
        if div_direction == "UP":
            div_component =  div_strength
        elif div_direction == "DOWN":
            div_component = -div_strength

        # Confirmación: OBI y momentum apuntan en la misma dirección
        obi_sign = 1 if obi_component > 0 else (-1 if obi_component < 0 else 0)
        mom_sign = 1 if mom_component > 0 else (-1 if mom_component < 0 else 0)
        confirmed = (obi_sign == mom_sign and obi_sign != 0)

        price_data = {
            "price":         price_snap.get("price"),
            "mom_30s":       mom_30,
            "mom_60s":       mom_60,
            "mom_component": mom_component,
            "div_component": div_component,
            "implied_prob":  div.get("implied_prob"),
            "divergence":    div.get("divergence"),
            "div_direction": div_direction,
            "div_strength":  div_strength,
        }

    # ── 3. Señal final combinada ───────────────────────────────────────────────
    if price_available:
        combined = round(
            _W_OBI * obi_component +
            _W_MOM * mom_component +
            _W_DIV * div_component,
            4,
        )
    else:
        # Modo degradado: solo OBI, threshold más exigente
        combined = round(obi_component, 4)

    # Penalización por spread alto
    spread_penalty = 1.0
    if up_ob:
        sp = up_ob.get("spread_pct", 0)
        if sp > 0.08:
            spread_penalty = max(0.3, 1.0 - (sp - 0.08) * 5)
    combined = round(combined * spread_penalty, 4)

    abs_c = abs(combined)

    # Threshold más bajo cuando precio real confirma la señal
    effective_threshold = threshold * (0.75 if confirmed else 1.0)

    if combined > effective_threshold:
        base_conf = int(55 + (abs_c / threshold) * 30)
        conf      = min(base_conf + (8 if confirmed else 0), 97)
        label     = "STRONG UP" if combined > effective_threshold * 1.8 else "UP"
        color     = "green"
    elif combined < -effective_threshold:
        base_conf = int(55 + (abs_c / threshold) * 30)
        conf      = min(base_conf + (8 if confirmed else 0), 97)
        label     = "STRONG DOWN" if combined < -effective_threshold * 1.8 else "DOWN"
        color     = "red"
    else:
        label = "NEUTRAL"
        color = "yellow"
        conf  = 50

    return {
        # Principal
        "label":               label,
        "color":               color,
        "confidence":          conf,
        "combined":            combined,
        "confirmed":           confirmed,
        "effective_threshold": round(effective_threshold, 4),
        "spread_penalty":      round(spread_penalty, 4),

        # Componentes (debug / display)
        "obi_component":       round(obi_component, 4),
        "mom_component":       round(mom_component, 4),
        "div_component":       round(div_component, 4),
        "price_available":     price_available,
        "price":               price_data,

        # Historial OBI
        "obi_combined":        combined_obi,
        "obi_ema":             round(ema_obi, 4),
        "history":             list(obi_window)[-20:],
        "threshold":           threshold,

        # Retrocompatibilidad
        "obi_now":             combined_obi,
        "obi_avg":             round(ema_obi, 4),
    }
