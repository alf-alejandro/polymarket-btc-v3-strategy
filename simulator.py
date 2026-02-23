"""
simulator.py — Estrategia: Underdog Hunter
Lógica: Compra fija de $1 en el último minuto si el precio está entre 0.05 y 0.15.
"""
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

INITIAL_CAPITAL = 100.0
TRADE_PCT = 0.01  # Atributo de compatibilidad

@dataclass
class Trade:
    id: int
    market: str
    direction: str           
    entry_price: float         
    shares: float         
    bet_size: float         
    entry_time: str
    secs_at_entry: float = 0.0
    status: str = "OPEN"
    pnl: float = 0.0
    exit_price: Optional[float] = None

    def close_binary(self, won: bool):
        self.exit_price = 1.0 if won else 0.0
        if won:
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
            "status": self.status,
            "entry_time": self.entry_time
        }

class Portfolio:
    def __init__(self, initial_capital: float = INITIAL_CAPITAL, db=None):
        self.initial_capital = initial_capital
        self.capital = initial_capital
        self.trade_pct = TRADE_PCT
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
        
        # Filtro: Último minuto y precio entre 0.05 y 0.15
        options = []
        if 0.05 < up_price <= 0.15: options.append(("UP", up_price))
        if 0.05 < down_price <= 0.15: options.append(("DOWN", down_price))
        
        if not options or secs_left > 60 or secs_left < 5:
            return False
            
        # Elegir la opción más barata (el Underdog real)
        direction, price = min(options, key=lambda x: x[1])
        bet_size = 1.0
        shares = round(bet_size / price, 4)
        
        self.capital = round(self.capital - bet_size, 4)
        self._trade_counter += 1
        self.active_trade = Trade(
            self._trade_counter, market_question, direction, 
            price, shares, bet_size, 
            datetime.now().strftime("%H:%M:%S"), secs_left
        )
        return True

    def close_trade(self, up_price, down_price):
        if not self.active_trade: return None
        trade = self.active_trade
        # Se gana si el precio final del token elegido es > 0.5
        won = (up_price > 0.5) if trade.direction == "UP" else (down_price > 0.5)
        trade.close_binary(won)
        if won: self.capital = round(self.capital + trade.shares, 4)
        self.closed_trades.append(trade)
        self.active_trade = None
        return trade
