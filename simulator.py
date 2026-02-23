"""simulator.py — Estrategia: Underdog (Caza-reversiones en el último minuto)

Lógica: Compra la opción con menos probabilidades (precio bajo) solo en el 
último minuto del mercado, esperando a la resolución binaria (0 o 1).
"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

INITIAL_CAPITAL   = 100.0
TRADE_PCT         = 0.01      # Se mantiene por compatibilidad con app.py

# ── Filtros de entrada ─────────────────────────────────────────────────────────
MIN_ENTRY_PRICE   = 0.05      # Precio mínimo (más de 0.05)
MAX_ENTRY_PRICE   = 0.15      # Precio máximo (igual o menos de 0.15)
MAX_ENTRY_SPREAD  = 0.15      # Spread más flexible para opciones baratas

# Ventana: Último minuto (60 segundos o menos)
MIN_SECS_TO_ENTER = 5         # Buffer de seguridad antes del cierre
MAX_SECS_TO_ENTER = 60        # Solo entrar si queda < 1 minuto

# ── Salida (Desactivadas para esta estrategia) ────────────────────────────────
TARGET_PRICE      = 1.10      
PARTIAL_EXIT_PCT  = 0.0       
SL_PRICE_ABS      = -0.10     

# ── Fee model ──────────────────────────────────────────────────────────────────
FEE_RATE = 0.0625             

def estimate_fee(price: float, usdc_amount: float) -> float:
    rate = max(0.0, price) * max(0.0, 1 - price) * FEE_RATE
    return round(rate * usdc_amount, 6)

@dataclass
class Trade:
    id:             int
    market:         str
    direction:      str           
    entry_price:    float         
    shares:         float         
    bet_size:       float         
    entry_time:     str
    secs_at_entry:  float = 0.0

    entry_fee:   float          = 0.0
    exit_price:  Optional[float] = None
    exit_fee:    float          = 0.0
    pnl:         Optional[float] = None
    status:      str            = "OPEN"
    exit_reason: Optional[str]  = None

    def __post_init__(self):
        self.entry_fee = estimate_fee(self.entry_price, self.bet_size)

    def close_binary(self, won: bool, exit_price: float,
                     exit_reason: str = "EXPIRE") -> float:
        self.exit_price  = exit_price
        self.exit_reason = exit_reason
        shares           = self.shares
        cost             = self.bet_size
        entry_fee        = self.entry_fee

        if won:
            proceeds      = shares * 1.0
            self.exit_fee = estimate_fee(1.0, proceeds)
            final_pnl     = round(proceeds - cost - entry_fee - self.exit_fee, 4)
        else:
            self.exit_fee = 0.0
            final_pnl     = round(-cost - entry_fee, 4)

        self.pnl    = final_pnl
        self.status = "WIN" if self.pnl > 0 else "LOSS"
        return self.pnl

    def to_dict(self) -> dict:
        return {
            "id":            self.id,
            "direction":     self.direction,
            "entry_price":   self.entry_price,
            "bet_size":      self.bet_size,
            "pnl":           self.pnl,
            "status":        self.status,
            "exit_reason":   self.exit_reason,
        }

class Portfolio:
    def __init__(self, initial_capital: float = INITIAL_CAPITAL, db=None):
        self.initial_capital  = initial_capital
        self.capital          = initial_capital
        self.trade_pct        = TRADE_PCT
        self.active_trade: Optional[Trade] = None
        self.closed_trades: list[Trade]    = []
        self._trade_counter = 0
        self._db            = db

    def restore(self, saved: dict) -> None:
        self.capital         = saved.get("capital", self.initial_capital)
        self.initial_capital = saved.get("initial_capital", self.initial_capital)
        self._trade_counter  = saved.get("trade_counter", 0)

    def consider_entry(self, signal: dict, market_question: str,
                       up_price: float, down_price: float,
                       secs_left: float = None, **kwargs) -> bool:

        if self.active_trade or self.capital < 1.0 or secs_left is None:
            return False

        # Filtro de tiempo: < 60 segundos
        if secs_left > MAX_SECS_TO_ENTER or secs_left < MIN_SECS_TO_ENTER:
            return False

        # Seleccionar la opción Underdog (0.05 < precio <= 0.15)
        prices = [("UP", up_price), ("DOWN", down_price)]
        eligible = [p for p in prices if MIN_ENTRY_PRICE < p[1] <= MAX_ENTRY_PRICE]
        
        if not eligible:
            return False
            
        # Elegir la de menor precio (la menos probable)
        direction, entry_price = min(eligible, key=lambda x: x[1])

        bet_size = 1.0 # Apuesta fija de $1
        shares   = round(bet_size / entry_price, 4)

        self.capital = round(self.capital - bet_size, 4)
        self._trade_counter += 1
        self.active_trade = Trade(
            id            = self._trade_counter,
            market        = market_question,
            direction     = direction,
            entry_price   = entry_price,
            shares        = shares,
            bet_size      = bet_size,
            entry_time    = datetime.utcnow().strftime("%H:%M:%S"),
            secs_at_entry = secs_left,
        )
        if self._db:
            self._db.save_trade(self.active_trade)
        return True

    def check_exits(self, *args, **kwargs) -> Optional[str]:
        return None # No hay salidas automáticas (TP/SL)

    def close_trade(self, up_price: float, down_price: float) -> Optional[Trade]:
        if not self.active_trade: return None
        trade = self.active_trade
        
        won = (up_price >= 0.5) if trade.direction == "UP" else (down_price >= 0.5)
        pnl = trade.close_binary(won, 1.0 if won else 0.0)
        
        if won:
            self.capital = round(self.capital + trade.shares, 4)
        
        self.closed_trades.append(trade)
        self.active_trade = None
        return trade
