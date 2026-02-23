"""
simulator.py — Portfolio simulation v4: TP/SL por movimiento de precio del token

CAMBIO CENTRAL v4:
  El TP y SL ya no se calculan como múltiplo del bet ($), sino como
  movimiento absoluto del precio del TOKEN desde la entrada.

  Antes (v3):
    TP = unrealized >= bet * 2.0   → necesitaba casi resolución binaria
    SL = unrealized <= -(bet * 0.45)

  Ahora (v4):
    TP = token sube  TP_PRICE_MOVE  desde entry_price  (ej: +0.08 = 8 centavos)
    SL = token baja  SL_PRICE_MOVE  desde entry_price  (ej: -0.04 = 4 centavos)

  Ratio riesgo/recompensa: 2:1 a favor.
  Ambos niveles son alcanzables en minutos sin esperar resolución binaria.

  Otros cambios v4:
    - SIGNAL_EXIT_N: 4 → 6  (no salir en cada fluctuación de OBI)
    - LATE_EXIT_SECS: 60 → 75 (más margen para dejar correr ganadores)
    - Trailing stop: si el trade va +6 centavos, el SL sube a breakeven
"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional


INITIAL_CAPITAL  = 100.0
TRADE_PCT        = 0.02      # 2% del capital por trade
MIN_CONFIDENCE   = 60
ENTRY_AFTER_N    = 3         # snaps consecutivos para confirmar entrada

# Entry filters
MIN_ENTRY_PRICE  = 0.20
MAX_ENTRY_SPREAD = 0.10

# ── TP/SL por movimiento de precio del token (v4) ─────────────────────────────
TP_PRICE_MOVE    = 0.08    # TP cuando el token sube 8¢ desde entrada
SL_PRICE_MOVE    = 0.04    # SL cuando el token baja 4¢ desde entrada
TRAIL_TRIGGER    = 0.06    # A partir de +6¢, activar trailing stop
TRAIL_SL_MOVE    = 0.00    # Trailing SL sube a breakeven (0¢ de pérdida)

# Signal exit
SIGNAL_EXIT_N    = 6       # Subido de 4 → 6: aguantar fluctuaciones de OBI
LATE_EXIT_SECS   = 75      # Subido de 60 → 75: más margen para ganadores


@dataclass
class Trade:
    id:            int
    market:        str
    direction:     str            # "UP" or "DOWN"
    entry_price:   float          # precio del token al entrar (0–1)
    shares:        float          # bet_size / entry_price
    bet_size:      float          # USDC comprometidos
    entry_time:    str
    exit_price:    Optional[float] = None
    pnl:           Optional[float] = None
    status:        str = "OPEN"   # OPEN | WIN | LOSS | CANCELLED
    exit_reason:   Optional[str]  = None  # TP | SL | TRAIL | SIGNAL | LATE | EXPIRE | FORCED

    # Niveles dinámicos (se actualizan con trailing stop)
    _tp_price:     float = field(default=0.0, repr=False)
    _sl_price:     float = field(default=0.0, repr=False)
    _trailing_active: bool = field(default=False, repr=False)

    def __post_init__(self):
        """Calcula TP/SL iniciales basados en el precio de entrada."""
        if self.direction == "UP":
            self._tp_price = round(self.entry_price + TP_PRICE_MOVE, 4)
            self._sl_price = round(self.entry_price - SL_PRICE_MOVE, 4)
        else:  # DOWN
            # Para DOWN: ganamos cuando el token baja
            self._tp_price = round(self.entry_price - TP_PRICE_MOVE, 4)
            self._sl_price = round(self.entry_price + SL_PRICE_MOVE, 4)

    def update_trailing(self, current_price: float):
        """
        Activa y actualiza el trailing stop.
        Cuando el trade va +TRAIL_TRIGGER en nuestro favor,
        movemos el SL a breakeven (entry_price) para proteger capital.
        """
        if self._trailing_active:
            return  # ya activado, no mover más por ahora

        if self.direction == "UP":
            profit_move = current_price - self.entry_price
            if profit_move >= TRAIL_TRIGGER:
                # Mover SL a breakeven
                self._sl_price = round(self.entry_price + TRAIL_SL_MOVE, 4)
                self._trailing_active = True
        else:  # DOWN
            profit_move = self.entry_price - current_price
            if profit_move >= TRAIL_TRIGGER:
                self._sl_price = round(self.entry_price - TRAIL_SL_MOVE, 4)
                self._trailing_active = True

    def check_tp_sl(self, current_price: float) -> Optional[str]:
        """
        Evalúa si el precio actual toca TP o SL.
        Retorna "TP", "SL", "TRAIL" o None.
        """
        self.update_trailing(current_price)

        if self.direction == "UP":
            if current_price >= self._tp_price:
                return "TP"
            if current_price <= self._sl_price:
                return "TRAIL" if self._trailing_active else "SL"
        else:  # DOWN
            if current_price <= self._tp_price:
                return "TP"
            if current_price >= self._sl_price:
                return "TRAIL" if self._trailing_active else "SL"

        return None

    def mark_to_market(self, current_price: float) -> float:
        return round(self.shares * current_price, 4)

    def unrealized_pnl(self, current_price: float) -> float:
        return round(self.mark_to_market(current_price) - self.bet_size, 4)

    def close_binary(self, won: bool, exit_price: float, exit_reason: str = "EXPIRE") -> float:
        self.exit_price  = exit_price
        self.exit_reason = exit_reason
        if won:
            proceeds    = self.shares * 1.0
            self.pnl    = round(proceeds - self.bet_size, 4)
            self.status = "WIN"
        else:
            self.pnl    = round(-self.bet_size, 4)
            self.status = "LOSS"
        return self.pnl

    def close_market(self, exit_price: float, exit_reason: str) -> float:
        self.exit_price  = round(exit_price, 4)
        self.exit_reason = exit_reason
        proceeds = self.shares * exit_price
        self.pnl = round(proceeds - self.bet_size, 4)
        self.status = "WIN" if self.pnl > 0 else "LOSS"
        return self.pnl

    def to_dict(self) -> dict:
        return {
            "id":               self.id,
            "market":           self.market,
            "direction":        self.direction,
            "entry_price":      self.entry_price,
            "shares":           round(self.shares, 4),
            "bet_size":         self.bet_size,
            "entry_time":       self.entry_time,
            "exit_price":       self.exit_price,
            "pnl":              self.pnl,
            "status":           self.status,
            "exit_reason":      self.exit_reason,
            "tp_price":         round(self._tp_price, 4),
            "sl_price":         round(self._sl_price, 4),
            "trailing_active":  self._trailing_active,
        }


class Portfolio:
    def __init__(self, initial_capital: float = INITIAL_CAPITAL,
                 trade_pct: float = TRADE_PCT, db=None):
        self.initial_capital  = initial_capital
        self.capital          = initial_capital
        self.trade_pct        = trade_pct
        self.active_trade: Optional[Trade]  = None
        self.closed_trades: list[Trade]     = []
        self.pnl_history:   list[float]     = [0.0]
        self._trade_counter  = 0
        self._db             = db
        self._signal_streak: dict = {"label": None, "count": 0}
        self._opposing_streak = 0

    def restore(self, saved: dict) -> None:
        self.capital         = saved["capital"]
        self.initial_capital = saved["initial_capital"]
        self.pnl_history     = saved["pnl_history"]
        self._trade_counter  = saved["trade_counter"]
        self.closed_trades   = saved["closed_trades"]

    # ── Entry ─────────────────────────────────────────────────────────────────

    def consider_entry(self, signal: dict, market_question: str,
                       up_price: float, down_price: float,
                       entry_depth_up: dict = None,
                       entry_depth_down: dict = None,
                       up_bid: float = None,
                       down_bid: float = None) -> bool:
        if self.active_trade is not None:
            return False
        if self.capital < 1.0:
            return False

        label = signal["label"]
        conf  = signal["confidence"]

        if label not in ("UP", "DOWN", "STRONG UP", "STRONG DOWN"):
            self._signal_streak = {"label": None, "count": 0}
            return False

        direction = "UP" if "UP" in label else "DOWN"

        if self._signal_streak["label"] == direction:
            self._signal_streak["count"] += 1
        else:
            self._signal_streak = {"label": direction, "count": 1}

        if self._signal_streak["count"] < ENTRY_AFTER_N:
            return False
        if conf < MIN_CONFIDENCE:
            return False

        bet_size = round(self.capital * self.trade_pct, 2)

        depth = entry_depth_up if direction == "UP" else entry_depth_down
        if depth and depth.get("shares", 0) > 0:
            entry_price = depth["avg_price"]
            shares      = depth["shares"]
        else:
            entry_price = up_price if direction == "UP" else down_price
            shares      = round(bet_size / entry_price, 4)

        if entry_price <= 0.01:
            return False
        if entry_price < MIN_ENTRY_PRICE:
            return False

        bid_for_dir = up_bid if direction == "UP" else down_bid
        if bid_for_dir is not None and bid_for_dir > 0:
            spread_pct = (entry_price - bid_for_dir) / entry_price
            if spread_pct > MAX_ENTRY_SPREAD:
                return False

        self.capital = round(self.capital - bet_size, 4)
        self._trade_counter  += 1
        self._opposing_streak = 0
        self.active_trade = Trade(
            id          = self._trade_counter,
            market      = market_question,
            direction   = direction,
            entry_price = entry_price,
            shares      = shares,
            bet_size    = bet_size,
            entry_time  = datetime.utcnow().strftime("%H:%M:%S"),
        )
        self._signal_streak = {"label": None, "count": 0}
        if self._db:
            self._db.save_trade(self.active_trade)
        return True

    # ── v4: Smart exit checks ─────────────────────────────────────────────────

    def check_exits(self, signal: dict, up_price: float, down_price: float,
                    secs_left) -> Optional[str]:
        """
        Prioridad: TP > SL/TRAIL > SIGNAL > LATE

        TP y SL ahora se evalúan contra el precio actual del token,
        no contra el P&L en dólares. Más realista y alcanzable.
        """
        if not self.active_trade:
            return None

        trade = self.active_trade
        current_price = self.current_price_for_trade(up_price, down_price)

        # 1. TP / SL / TRAIL (basado en precio del token)
        price_exit = trade.check_tp_sl(current_price)
        if price_exit:
            return price_exit

        # 2. Signal reversal streak
        label = signal.get("label", "NEUTRAL")
        if label in ("UP", "DOWN", "STRONG UP", "STRONG DOWN"):
            direction = "UP" if "UP" in label else "DOWN"
            if direction != trade.direction:
                self._opposing_streak += 1
            else:
                self._opposing_streak = 0
        # NEUTRAL: no modifica el streak

        if self._opposing_streak >= SIGNAL_EXIT_N:
            return "SIGNAL"

        # 3. Late exit: cualquier ganancia en los últimos LATE_EXIT_SECS
        upnl = trade.unrealized_pnl(current_price)
        if secs_left is not None and secs_left <= LATE_EXIT_SECS and upnl > 0:
            return "LATE"

        return None

    def exit_at_market_price(self, up_price: float, down_price: float,
                             exit_reason: str,
                             exit_bid_price: float = None) -> Optional[Trade]:
        if not self.active_trade:
            return None

        trade = self.active_trade
        cp    = exit_bid_price if exit_bid_price is not None \
                else self.current_price_for_trade(up_price, down_price)
        pnl   = trade.close_market(cp, exit_reason)

        self.capital = round(self.capital + trade.bet_size + pnl, 4)
        self.closed_trades.append(trade)
        self.active_trade      = None
        self._signal_streak    = {"label": None, "count": 0}
        self._opposing_streak  = 0

        total_pnl = round(self.capital - self.initial_capital, 4)
        self.pnl_history.append(total_pnl)

        if self._db:
            self._db.save_trade(trade)
            self._db.save_portfolio_state(
                self.capital, self.initial_capital,
                self.pnl_history, self._trade_counter,
            )
        return trade

    # ── Mark-to-market ────────────────────────────────────────────────────────

    def current_price_for_trade(self, up_price: float, down_price: float) -> float:
        if not self.active_trade:
            return 0.0
        return up_price if self.active_trade.direction == "UP" else down_price

    def get_unrealized(self, up_price: float, down_price: float) -> float:
        if not self.active_trade:
            return 0.0
        cp = self.current_price_for_trade(up_price, down_price)
        return self.active_trade.unrealized_pnl(cp)

    # ── Binary close (market expiry fallback) ─────────────────────────────────

    def close_trade(self, up_price: float, down_price: float,
                    force_winner: Optional[bool] = None) -> Optional[Trade]:
        if not self.active_trade:
            return None

        trade = self.active_trade

        if force_winner is not None:
            won = force_winner
        else:
            won = (up_price >= 0.5) if trade.direction == "UP" else (down_price >= 0.5)

        exit_price = 1.0 if won else 0.0
        pnl        = trade.close_binary(won, exit_price, "EXPIRE")
        self.capital = round(self.capital + trade.bet_size + pnl, 4)
        self.closed_trades.append(trade)
        self.active_trade      = None
        self._signal_streak    = {"label": None, "count": 0}
        self._opposing_streak  = 0

        total_pnl = round(self.capital - self.initial_capital, 4)
        self.pnl_history.append(total_pnl)

        if self._db:
            self._db.save_trade(trade)
            self._db.save_portfolio_state(
                self.capital, self.initial_capital,
                self.pnl_history, self._trade_counter,
            )
        return trade

    def cancel_active_trade(self):
        if not self.active_trade:
            return
        self.active_trade.status      = "CANCELLED"
        self.active_trade.exit_reason = "FORCED"
        self.closed_trades.append(self.active_trade)
        if self._db:
            self._db.save_trade(self.active_trade)
            self._db.save_portfolio_state(
                self.capital, self.initial_capital,
                self.pnl_history, self._trade_counter,
            )
        self.active_trade     = None
        self._opposing_streak = 0

    # ── Stats ─────────────────────────────────────────────────────────────────

    def stats(self, up_price: float = 0.5, down_price: float = 0.5) -> dict:
        closed   = self.closed_trades
        wins     = [t for t in closed if t.status == "WIN"]
        losses   = [t for t in closed if t.status == "LOSS"]
        n_closed = len(wins) + len(losses)
        win_rate = round(len(wins) / n_closed * 100, 1) if n_closed else 0.0

        realized_pnl   = sum(t.pnl for t in closed if t.pnl is not None)
        unrealized_pnl = self.get_unrealized(up_price, down_price)
        total_pnl      = round(realized_pnl + unrealized_pnl, 4)
        equity         = round(
            self.capital
            + (self.active_trade.bet_size if self.active_trade else 0)
            + unrealized_pnl, 4,
        )

        active = None
        if self.active_trade:
            t  = self.active_trade
            cp = self.current_price_for_trade(up_price, down_price)

            # Distancia al TP y SL en precio del token
            if t.direction == "UP":
                dist_to_tp = round(t._tp_price - cp, 4)
                dist_to_sl = round(cp - t._sl_price, 4)
            else:
                dist_to_tp = round(cp - t._tp_price, 4)
                dist_to_sl = round(t._sl_price - cp, 4)

            # Progreso: 0% = en SL, 100% = en TP
            total_range = TP_PRICE_MOVE + SL_PRICE_MOVE
            if t.direction == "UP":
                progress = round(
                    max(0.0, min(1.0, (cp - t._sl_price) / total_range)) * 100, 1
                )
            else:
                progress = round(
                    max(0.0, min(1.0, (t._sl_price - cp) / total_range)) * 100, 1
                )

            active = {
                **t.to_dict(),
                "current_price":    round(cp, 4),
                "mark_to_market":   t.mark_to_market(cp),
                "unrealized_pnl":   t.unrealized_pnl(cp),
                "tp_price":         round(t._tp_price, 4),
                "sl_price":         round(t._sl_price, 4),
                "dist_to_tp":       dist_to_tp,
                "dist_to_sl":       dist_to_sl,
                "trailing_active":  t._trailing_active,
                "progress_pct":     progress,
                "opposing_streak":  self._opposing_streak,
                # PnL potencial en $ si llega a TP o SL
                "tp_pnl":           round(t.shares * t._tp_price - t.bet_size, 4),
                "sl_pnl":           round(t.shares * t._sl_price - t.bet_size, 4),
            }

        exit_reasons: dict = {}
        for t in closed:
            r = t.exit_reason or "EXPIRE"
            exit_reasons[r] = exit_reasons.get(r, 0) + 1

        return {
            "initial_capital": self.initial_capital,
            "capital":         round(self.capital, 4),
            "equity":          equity,
            "realized_pnl":    round(realized_pnl, 4),
            "unrealized_pnl":  round(unrealized_pnl, 4),
            "total_pnl":       total_pnl,
            "total_pnl_pct":   round(total_pnl / self.initial_capital * 100, 2),
            "total_trades":    len(closed),
            "wins":            len(wins),
            "losses":          len(losses),
            "cancelled":       len([t for t in closed if t.status == "CANCELLED"]),
            "win_rate":        win_rate,
            "best_trade":      round(max((t.pnl for t in closed if t.pnl), default=0), 4),
            "worst_trade":     round(min((t.pnl for t in closed if t.pnl), default=0), 4),
            "avg_pnl":         round(realized_pnl / n_closed, 4) if n_closed else 0,
            "pnl_history":     self.pnl_history[-50:],
            "active_trade":    active,
            "trade_log":       [t.to_dict() for t in reversed(closed[-20:])],
            "signal_streak":   self._signal_streak,
            "exit_reasons":    exit_reasons,
        }
