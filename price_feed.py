"""
price_feed.py — Feed de precio real de BTC/SOL desde Binance

Sin API key. Usa el endpoint público REST de Binance.
Calcula momentum y detecta divergencia con el token de Polymarket.

Lógica central:
  - Cada snap obtenemos el precio spot de BTC/SOL
  - Calculamos el retorno % en los últimos N segundos (momentum)
  - Lo comparamos con el precio implícito del token UP en Polymarket
  - Si el precio real ya subió pero el token UP todavía no se ajustó → señal UP
  - Si el precio real ya bajó pero el token DOWN todavía no se ajustó → señal DOWN
  - Ese lag (arbitraje de información) es la ventaja real

Configurable:
  SYMBOL env var = SOL | BTC  (default: SOL)
"""

import os
import time
import logging
import requests
from collections import deque

log = logging.getLogger("price_feed")

SYMBOL = os.environ.get("SYMBOL", "SOL").upper()

# Binance symbol: SOLUSDT o BTCUSDT
BINANCE_SYMBOL = f"{SYMBOL}USDT"
BINANCE_TICKER = f"https://api.binance.com/api/v3/ticker/price?symbol={BINANCE_SYMBOL}"
BINANCE_KLINES = f"https://api.binance.com/api/v3/klines"

# Cuántas lecturas guardamos para calcular momentum
PRICE_HISTORY_LEN = 30   # ~90 segundos con poll de 3s

# Umbrales de momentum para generar señal
# SOL es más volátil que BTC, por eso umbrales distintos
MOMENTUM_THRESHOLD = {
    "SOL": 0.0015,   # 0.15% en el periodo medido → señal
    "BTC": 0.0008,   # 0.08% → señal (BTC menos volátil)
}.get(SYMBOL, 0.0012)

# Umbral de divergencia: cuánto puede estar desajustado el token vs la probabilidad
# implícita que sugiere el precio real
DIVERGENCE_THRESHOLD = 0.06  # 6 puntos de diferencia en probabilidad implícita


class PriceFeed:
    """
    Mantiene historial de precios del activo subyacente y calcula señales
    de momentum y divergencia respecto al token de Polymarket.
    """

    def __init__(self, symbol: str = SYMBOL, history_len: int = PRICE_HISTORY_LEN):
        self.symbol       = symbol
        self.binance_sym  = f"{symbol}USDT"
        self._history     = deque(maxlen=history_len)  # (timestamp, price)
        self._last_fetch  = 0.0
        self._last_price  = None
        self._error_count = 0

    # ── Fetch ─────────────────────────────────────────────────────────────────

    def fetch_price(self) -> float | None:
        """Obtiene el precio spot actual desde Binance. Retorna None si falla."""
        try:
            url = f"https://api.binance.com/api/v3/ticker/price?symbol={self.binance_sym}"
            r   = requests.get(url, timeout=4)
            r.raise_for_status()
            price = float(r.json()["price"])
            self._last_price  = price
            self._error_count = 0
            return price
        except Exception as e:
            self._error_count += 1
            if self._error_count <= 3:
                log.warning(f"PriceFeed fetch error ({self.binance_sym}): {e}")
            return None

    def update(self) -> float | None:
        """Llama fetch y guarda en historial. Retorna precio actual o None."""
        price = self.fetch_price()
        if price is not None:
            self._history.append((time.time(), price))
        return price

    # ── Momentum ──────────────────────────────────────────────────────────────

    def momentum(self, lookback_secs: float = 60.0) -> float:
        """
        Retorno % del precio en los últimos `lookback_secs` segundos.
        Positivo = precio subió → favorece UP.
        Negativo = precio bajó  → favorece DOWN.
        Retorna 0.0 si no hay historial suficiente.
        """
        if len(self._history) < 2:
            return 0.0

        now = time.time()
        cutoff = now - lookback_secs

        # Buscar el precio más antiguo dentro de la ventana
        oldest_price = None
        for ts, px in self._history:
            if ts >= cutoff:
                oldest_price = px
                break

        if oldest_price is None or oldest_price == 0:
            return 0.0

        current_price = self._history[-1][1]
        return round((current_price - oldest_price) / oldest_price, 6)

    def momentum_30s(self) -> float:
        return self.momentum(30.0)

    def momentum_60s(self) -> float:
        return self.momentum(60.0)

    def momentum_90s(self) -> float:
        return self.momentum(90.0)

    # ── Divergencia vs token Polymarket ───────────────────────────────────────

    def implied_probability_from_momentum(self, mom_60s: float) -> float:
        """
        Convierte el momentum de 60s en una probabilidad implícita de UP.

        Lógica:
          - Si el precio subió X% en 60s, el mercado debería estar valorando
            el token UP más alto de lo que estaba.
          - Usamos una función logística calibrada para SOL/BTC en ventanas de 5min.
          - Base 50% (sin momentum) ± ajuste por momentum.

        Este no es un modelo perfecto, es una heurística calibrada empíricamente.
        """
        # Escalar el momentum al rango de probabilidad
        # Para SOL: un movimiento de ±1% en 60s → probabilidad ~70/30
        # Para BTC: un movimiento de ±0.5% en 60s → probabilidad ~65/35
        scale = {
            "SOL": 25.0,   # multiplicador
            "BTC": 45.0,
        }.get(self.symbol, 30.0)

        adj = mom_60s * scale   # ej: +0.01 * 25 = +0.25
        prob = 0.5 + adj
        return round(max(0.05, min(0.95, prob)), 4)

    def divergence_signal(self, token_up_price: float, mom_60s: float) -> dict:
        """
        Compara la probabilidad implícita del precio real con el precio del token UP.

        Si implied_prob >> token_up_price → el token está BARATO → señal de COMPRA UP
        Si implied_prob << token_up_price → el token está CARO   → señal de COMPRA DOWN

        Retorna un dict con:
          - direction: "UP" | "DOWN" | "NEUTRAL"
          - divergence: float (diferencia signed, positivo = favorece UP)
          - implied_prob: float
          - strength: float 0-1
        """
        implied = self.implied_probability_from_momentum(mom_60s)
        div     = round(implied - token_up_price, 4)  # positivo = token barato (favorece UP)
        abs_div = abs(div)

        if abs_div < DIVERGENCE_THRESHOLD:
            return {
                "direction":    "NEUTRAL",
                "divergence":   div,
                "implied_prob": implied,
                "strength":     0.0,
            }

        direction = "UP" if div > 0 else "DOWN"
        # Fuerza: 0 en el umbral, 1 cuando la divergencia es 3x el umbral
        strength = round(min(1.0, (abs_div - DIVERGENCE_THRESHOLD) / (DIVERGENCE_THRESHOLD * 2)), 4)

        return {
            "direction":    direction,
            "divergence":   div,
            "implied_prob": implied,
            "strength":     strength,
        }

    # ── Resumen para el loop ──────────────────────────────────────────────────

    def snapshot(self, token_up_price: float) -> dict:
        """
        Todo lo que necesita el loop de estrategia en un solo dict.
        Llama update() internamente.
        """
        price = self.update()

        if price is None:
            return {
                "price":        self._last_price,
                "available":    False,
                "mom_30s":      0.0,
                "mom_60s":      0.0,
                "mom_90s":      0.0,
                "divergence":   {},
                "error_count":  self._error_count,
            }

        mom_30 = self.momentum_30s()
        mom_60 = self.momentum_60s()
        mom_90 = self.momentum_90s()
        div    = self.divergence_signal(token_up_price, mom_60)

        return {
            "price":        price,
            "available":    True,
            "mom_30s":      mom_30,
            "mom_60s":      mom_60,
            "mom_90s":      mom_90,
            "divergence":   div,
            "error_count":  0,
            "symbol":       self.symbol,
            "binance_sym":  self.binance_sym,
        }

    @property
    def last_price(self) -> float | None:
        return self._last_price

    @property
    def history_len(self) -> int:
        return len(self._history)
