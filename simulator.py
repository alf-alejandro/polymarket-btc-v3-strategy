"""
simulator.py — Portfolio simulation v5: TP/SL ajustados por fee real de Polymarket

CONTEXTO:
  Polymarket cobra taker fee en mercados crypto de 5min (desde enero 2026).
  Fee = p × (1-p) × FEE_RATE, donde p = precio del token (0-1).
  Fee máxima: 1.56% cuando p = 0.50. Decrece hacia los extremos.

  Con bet de $2 a p=0.50:
    Fee entrada ≈ $0.031
    Fee salida  ≈ $0.031
    Total fees  ≈ $0.063 por trade round-trip

  Esto significa que el ratio TP:SL bruto de 2:1 se reduce a ~1.35:1 neto.

AJUSTES v5 vs v4:
  - TRADE_PCT: 2% → 3%  (bet mayor = fees proporcionalmente menores)
  - TP_PRICE_MOVE: 0.08 → 0.10  (10 centavos: ratio neto sube a ~1.8:1)
  - SL_PRICE_MOVE: 0.04 → 0.04  (se mantiene igual)
  - TRAIL_TRIGGER: 0.06 → 0.07  (trailing activa a +7¢, antes del TP de 10¢)
  - Fee estimada embebida en el cálculo de P&L para mostrar neto real

RATIO NETO REAL (a p=0.50, bet=$3):
  TP bruto:  6 shares × 0.10 = $0.60
  Fee total: ~$0.094
  TP neto:   $0.506

  SL bruto:  6 shares × 0.04 = $0.24
  Fee salida: ~$0.047
  SL neto:   $0.287

  Ratio neto: 0.506 / 0.287 ≈ 1.76:1 ✓
"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional


INITIAL_CAPITAL  = 100.0
TRADE_PCT        = 0.03      # Subido 2% → 3%: fees menos pesadas proporcionalmente
MIN_CONFIDENCE   = 60
ENTRY_AFTER_N    = 3

# Entry filters
MIN_ENTRY_PRICE  = 0.20
MAX_ENTRY_SPREAD = 0.10

# ── TP/SL por movimiento de precio del token ───────────────────────────────────
TP_PRICE_MOVE    = 0.10    # Subido 8¢ → 10¢: más margen para cubrir fees
SL_PRICE_MOVE    = 0.04    # Se mantiene 4¢
TRAIL_TRIGGER    = 0.07    # Trailing activa a +7¢ (antes del TP de 10¢)
TRAIL_SL_MOVE    = 0.00    # Trailing SL sube a breakeven

# Signal / time exits
SIGNAL_EXIT_N    = 6
LATE_EXIT_SECS   = 75

# ── Fee model (Polymarket taker fee, 5min crypto markets) ─────────────────────
FEE_RATE         = 0.0625   # fee = p × (1-p) × FEE_RATE


def estimate_fee(price: float, usdc_amount: float) -> float:
    """
    Estima la taker fee de Polymarket para una operación.
    price: precio del token (0–1)
    usdc_amount: USDC invertidos (bet_size)
    Retorna fee en USDC.
    """
    rate = price * (1 - price) * FEE_RATE
    return round(rate * usdc_amount, 6)


@dataclass
class Trade:
    id:            int
    market:        str
    direction:     str
    entry_price:   float
    shares:        float
    bet_size:      float
    entry_time:    str
    entry_fee:     float = 0.0   # fee pagada al entrar
    exit_price:    Optional[float] = None
    exit_fee:      float = 0.0   # fee pagada al salir
    pnl:           Optional[float] = None   # P&L neto (después de fees)
    pnl_gross:     Optional[float] = None   # P&L bruto (antes de fees)
    status:        str = "OPEN"
    exit_reason:   Optional[str] = None

    _tp_price:        float = field(default=0.0, repr=False)
    _sl_price:        float = field(default=0.0, repr=False)
    _trailing_active: bool  = field(default=False, repr=False)

    def __post_init__(self):
        # Calcular fee de entrada
        self.entry_fee = estimate_fee(self.entry_price, self.bet_size)

        # TP y SL en precio del token
        if self.direction == "UP":
            self._tp_price = round(self.entry_price + TP_PRICE_MOVE, 4)
            self._sl_price = round(self.entry_price - SL_PRICE_MOVE, 4)
        else:
            self._tp_price = round(self.entry_price - TP_PRICE_MOVE, 4)
            self._sl_price = round(self.entry_price + SL_PRICE_MOVE, 4)

    def update_trailing(self, current_price: float):
        if self._trailing_active:
            return
        if self.direction == "UP":
            if current_price - self.entry_price >= TRAIL_TRIGGER:
                self._sl_price = round(self.entry_price + TRAIL_SL_MOVE, 4)
                self._trailing_active = True
        else:
            if self.entry_price - current_price >= TRAIL_TRIGGER:
                self._sl_price = round(self.entry_price - TRAIL_SL_MOVE, 4)
                self._trailing_active = True

    def check_tp_sl(self, current_price: float) -> Optional[str]:
        self.update_trailing(current_price)
        if self.direction == "UP":
            if current_price >= self._tp_price:
                return "TP"
            if current_price <= self._sl_price:
                return "TRAIL" if self._trailing_active else "SL"
        else:
            if current_price <= self._tp_price:
                return "TP"
            if current_price >= self._sl_price:
                return "TRAIL" if self._trailing_active else "SL"
        return None

    def mark_to_market(self, current_price: float) -> float:
        return round(self.shares * current_price, 4)

    def unrealized_pnl(self, current_price: float) -> float:
        """P&L neto estimado (descuenta fee de entrada ya pagada + fee de salida estimada)."""
        gross     = self.mark_to_market(current_price) - self.bet_size
        exit_fee  = estimate_fee(current_price, self.mark_to_market(current_price))
        return round(gross - self.entry_fee - exit_fee, 4)

    def unrealized_pnl_gross(self, current_price: float) -> float:
        """P&L bruto (sin fees, para referencia)."""
        return round(self.mark_to_market(current_price) - self.bet_size, 4)

    def close_binary(self, won: bool, exit_price: float, exit_reason: str = "EXPIRE") -> float:
        self.exit_price  = exit_price
        self.exit_reason = exit_reason
        if won:
            proceeds       = self.shares * 1.0
            self.pnl_gross = round(proceeds - self.bet_size, 4)
            # Fee al vender a precio ~1.0 es casi 0 (p*(1-p) → 0)
            self.exit_fee  = estimate_fee(exit_price, proceeds)
            self.pnl       = round(self.pnl_gross - self.entry_fee - self.exit_fee, 4)
            self.status    = "WIN"
        else:
            self.pnl_gross = round(-self.bet_size, 4)
            self.exit_fee  = 0.0
            self.pnl       = round(-self.bet_size - self.entry_fee, 4)
            self.status    = "LOSS"
        return self.pnl

    def close_market(self, exit_price: float, exit_reason: str) -> float:
        self.exit_price  = round(exit_price, 4)
        self.exit_reason = exit_reason
        proceeds         = self.shares * exit_price
        self.exit_fee    = estimate_fee(exit_price, proceeds)
        self.pnl_gross   = round(proceeds - self.bet_size, 4)
        self.pnl         = round(proceeds - self.bet_size - self.entry_fee - self.exit_fee, 4)
        self.status      = "WIN" if self.pnl > 0 else "LOSS"
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
            "entry_fee":        round(self.entry_fee, 4),
            "exit_price":       self.exit_price,
            "exit_fee":         round(self.exit_fee, 4),
            "pnl_gross":        self.pnl_gross,
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

    # ── Exit checks ───────────────────────────────────────────────────────────

    def check_exits(self, signal: dict, up_price: float, down_price: float,
                    secs_left) -> Optional[str]:
        if not self.active_trade:
            return None

        trade         = self.active_trade
        current_price = self.current_price_for_trade(up_price, down_price)

        # 1. TP / SL / TRAIL por precio del token
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

        if self._opposing_streak >= SIGNAL_EXIT_N:
            return "SIGNAL"

        # 3. Late exit con ganancia neta positiva
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

    # ── Binary close ──────────────────────────────────────────────────────────

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
        total_fees     = sum((t.entry_fee + t.exit_fee) for t in closed)
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

            if t.direction == "UP":
                dist_to_tp = round(t._tp_price - cp, 4)
                dist_to_sl = round(cp - t._sl_price, 4)
                progress   = round(max(0.0, min(1.0,
                    (cp - t._sl_price) / (TP_PRICE_MOVE + SL_PRICE_MOVE)
                )) * 100, 1)
            else:
                dist_to_tp = round(cp - t._tp_price, 4)
                dist_to_sl = round(t._sl_price - cp, 4)
                progress   = round(max(0.0, min(1.0,
                    (t._sl_price - cp) / (TP_PRICE_MOVE + SL_PRICE_MOVE)
                )) * 100, 1)

            exit_fee_est = estimate_fee(cp, t.mark_to_market(cp))
            active = {
                **t.to_dict(),
                "current_price":    round(cp, 4),
                "mark_to_market":   t.mark_to_market(cp),
                "unrealized_pnl":   t.unrealized_pnl(cp),      # neto (con fees)
                "unrealized_gross": t.unrealized_pnl_gross(cp), # bruto
                "exit_fee_est":     round(exit_fee_est, 4),
                "tp_price":         round(t._tp_price, 4),
                "sl_price":         round(t._sl_price, 4),
                "dist_to_tp":       dist_to_tp,
                "dist_to_sl":       dist_to_sl,
                "trailing_active":  t._trailing_active,
                "progress_pct":     progress,
                "opposing_streak":  self._opposing_streak,
                "tp_pnl":  round(t.shares * t._tp_price - t.bet_size - t.entry_fee
                                 - estimate_fee(t._tp_price, t.shares * t._tp_price), 4),
                "sl_pnl":  round(t.shares * t._sl_price - t.bet_size - t.entry_fee
                                 - estimate_fee(t._sl_price, t.shares * t._sl_price), 4),
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
            "total_fees_paid": round(total_fees, 4),      # cuánto se fue en fees
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
