"""
simulator.py — Estrategia: Underdog (Caza-reversiones en el último minuto)
"""
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

INITIAL_CAPITAL   = 100.0
# Mantenemos trade_pct para evitar errores de compatibilidad con app.py
TRADE_PCT         = 0.01      

# ── Configuración de la Estrategia ─────────────────────────────────────────────
MIN_ENTRY_PRICE   = 0.05      # Más de 0.05
MAX_ENTRY_PRICE   = 0.15      # Igual o menos de 0.15
MAX_SECS_TO_ENTER = 60        # Último minuto
MIN_SECS_TO_ENTER = 5         # Margen de seguridad antes del cierre

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
    status:         str = "OPEN"
    pnl:            Optional[float] = None
    entry_fee:      float = 0.0
    exit_price:     Optional[float] = None
    exit_reason:    Optional[str] = None

    def close_binary(self, won: bool):
        """Cierre basado en resolución 0 o 1 (sin stop loss)"""
        self.exit_price = 1.0 if won else 0.0
        self.exit_reason = "EXPIRE"
        if won:
            # PnL = Valor final de acciones - costo inicial
            self.pnl = round(self.shares - self.bet_size, 4)
            self.status = "WIN"
        else:
            self.pnl = round(-self.bet_size, 4)
            self.status = "LOSS"
        return self.pnl

    def to_dict(self) -> dict:
        return {
            "id": self.id, 
            "direction": self.direction, 
            "entry_price": self.entry_price, 
            "bet_size": self.bet_size,
            "pnl": self.pnl, 
            "status": self.status
        }

class Portfolio:
    def __init__(self, initial_capital: float = INITIAL_CAPITAL, db=None):
        self.initial_capital = initial_capital
        self.capital = initial_capital
        self.trade_pct = TRADE_PCT  # Evita el AttributeError
        self.active_trade: Optional[Trade] = None
        self.closed_trades = []
        self._trade_counter = 0
        self._db = db

    def restore(self, saved: dict) -> None:
        self.capital = saved.get("capital", self.initial_capital)
        self._trade_counter = saved.get("trade_counter", 0)

    def consider_entry(self, signal, market_question, up_price, down_price, secs_left=None, **kwargs):
        if self.active_trade or self.capital < 1.0 or secs_left is None:
            return False
        
        # Filtro de tiempo: Solo en el último minuto
        if secs_left > MAX_SECS_TO_ENTER or secs_left < MIN_SECS_TO_ENTER:
            return False

        # Identificar si hay algún token en el rango "Underdog" (0.05 - 0.15)
        options = []
        if MIN_ENTRY_PRICE < up_price <= MAX_ENTRY_PRICE:
            options.append(("UP", up_price))
        if MIN_ENTRY_PRICE < down_price <= MAX_ENTRY_PRICE:
            options.append(("DOWN", down_price))
        
        if not options:
            return False
            
        # Seleccionamos la opción con menos probabilidades (la más barata)
        direction, price = min(options, key=lambda x: x[1])
        
        # Apuesta fija de $1
        bet_size = 1.0
        shares = round(bet_size / price, 4)
        
        self.capital = round(self.capital - bet_size, 4)
        self._trade_counter += 1
        
        self.active_trade = Trade(
            id=self._trade_counter,
            market=market_question,
            direction=direction,
            entry_price=price,
            shares=shares,
            bet_size=bet_size,
            entry_time=datetime.utcnow().strftime("%H:%M:%S"),
            secs_at_entry=secs_left
        )
        
        if self._db: self._db.save_trade(self.active_trade)
        return True

    def check_exits(self, *args, **kwargs):
        return None # No usamos salidas por precio (TP/SL)

    def close_trade(self, up_price, down_price):
        if not self.active_trade: return None
        trade = self.active_trade
        
        # Ganamos si el token comprado termina por encima de 0.5 (indicador de victoria)
        won = (up_price > 0.5) if trade.direction == "UP" else (down_price > 0.5)
        
        pnl = trade.close_binary(won)
        if won:
            self.capital = round(self.capital + trade.shares, 4)
        
        self.closed_trades.append(trade)
        self.active_trade = None
        return trade
