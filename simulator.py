"""
simulator.py — Portfolio simulation v4: Deep Value 3-TP

Estrategia:
  - Entrada: token < 0.18, solo en los primeros 1:20 (80s antes del fin = 220s restantes)
  - TP1: vender 33% cuando precio llega a 0.33
  - TP2: vender 33% cuando precio llega a 0.50
  - TP3: el 34% restante → resolución binaria (espera expiración: 0 o 1)
  - SL:  si precio cae a 0.05, cortar todo lo que queda
  - Capital: $100, apuesta 1% por trade
"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional


INITIAL_CAPITAL   = 100.0
TRADE_PCT         = 0.01        # 1% por trade

# ── Filtros de entrada ──────────────────────────────────────────────────────
MIN_ENTRY_PRICE   = 0.03        # no entrar por debajo de 3¢ (probablemente muerto)
MAX_ENTRY_PRICE   = 0.18        # zona deep value: token < 18¢
MAX_ENTRY_SPREAD  = 0.10        # spread máximo tolerable

# Ventana temporal: minuto 0 hasta minuto 1:20
# El mercado dura 300s → "hasta minuto 1:20" = quedan entre 220s y 300s
MIN_SECS_TO_ENTER = 220         # no entrar si quedan menos de 3:40
MAX_SECS_TO_ENTER = 300         # desde el primer segundo

# ── Salidas escalonadas ─────────────────────────────────────────────────────
TP1_PRICE         = 0.33        # vender 33% aquí
TP2_PRICE         = 0.50        # vender 33% aquí
# 34% restante → espera resolución binaria

TP1_FRAC          = 1 / 3       # fracción a vender en TP1
TP2_FRAC          = 1 / 3       # fracción a vender en TP2
# TP3_FRAC = 1/3 (implícito, shares_remaining)

SL_PRICE_ABS      = 0.05        # SL: si cae a 5¢, cortar todo

# ── Fee model ───────────────────────────────────────────────────────────────
FEE_RATE = 0.0625               # fee = p × (1-p) × FEE_RATE


def estimate_fee(price: float, usdc_amount: float) -> float:
    rate = max(0.0, price) * max(0.0, 1 - price) * FEE_RATE
    return round(rate * usdc_amount, 6)


@dataclass
class Trade:
    id:            int
    market:        str
    direction:     str           # "UP" or "DOWN"
    entry_price:   float
    shares:        float         # total shares compradas
    bet_size:      float         # USDC gastados
    entry_time:    str
    secs_at_entry: float = 0.0

    # ── Estado de TPs parciales ─────────────────────────────────────────────
    tp1_done:       bool            = field(default=False, repr=False)
    tp2_done:       bool            = field(default=False, repr=False)
    shares_remaining: float         = field(default=0.0,   repr=False)

    tp1_price_exec: Optional[float] = None
    tp1_pnl:        Optional[float] = None
    tp2_price_exec: Optional[float] = None
    tp2_pnl:        Optional[float] = None

    # ── Resultado final ─────────────────────────────────────────────────────
    entry_fee:   float           = 0.0
    exit_price:  Optional[float] = None
    exit_fee:    float           = 0.0
    pnl:         Optional[float] = None
    status:      str             = "OPEN"
    exit_reason: Optional[str]   = None

    def __post_init__(self):
        self.entry_fee        = estimate_fee(self.entry_price, self.bet_size)
        self.shares_remaining = self.shares

    # ── Checks ──────────────────────────────────────────────────────────────

    def should_tp1(self, price: float) -> bool:
        return not self.tp1_done and price >= TP1_PRICE

    def should_tp2(self, price: float) -> bool:
        return self.tp1_done and not self.tp2_done and price >= TP2_PRICE

    def should_sl(self, price: float) -> bool:
        return price <= SL_PRICE_ABS

    # ── Operaciones ─────────────────────────────────────────────────────────

    def _sell_fraction(self, frac: float, exit_price: float) -> float:
        """Vende `frac` del total original de shares. Retorna P&L neto."""
        shares_to_sell    = round(self.shares * frac, 6)
        proceeds          = shares_to_sell * exit_price
        fee               = estimate_fee(exit_price, proceeds)
        entry_fee_portion = self.entry_fee * frac
        cost_portion      = self.bet_size  * frac
        pnl               = round(proceeds - cost_portion - entry_fee_portion - fee, 6)
        self.shares_remaining = round(self.shares_remaining - shares_to_sell, 6)
        return pnl

    def do_tp1(self, exit_price: float) -> float:
        pnl = self._sell_fraction(TP1_FRAC, exit_price)
        self.tp1_done       = True
        self.tp1_price_exec = round(exit_price, 4)
        self.tp1_pnl        = round(pnl, 4)
        return round(pnl, 4)

    def do_tp2(self, exit_price: float) -> float:
        pnl = self._sell_fraction(TP2_FRAC, exit_price)
        self.tp2_done       = True
        self.tp2_price_exec = round(exit_price, 4)
        self.tp2_pnl        = round(pnl, 4)
        return round(pnl, 4)

    def close_binary(self, won: bool, exit_price: float,
                     exit_reason: str = "EXPIRE") -> float:
        """Resolución binaria del tramo restante (TP3)."""
        self.exit_price  = exit_price
        self.exit_reason = exit_reason

        # Fracción que queda sin vender
        sold_frac     = (TP1_FRAC if self.tp1_done else 0) + (TP2_FRAC if self.tp2_done else 0)
        remain_frac   = max(0.0, 1.0 - sold_frac)

        shares        = self.shares_remaining
        cost          = self.bet_size  * remain_frac
        entry_fee     = self.entry_fee * remain_frac

        if won:
            proceeds      = shares * 1.0
            self.exit_fee = estimate_fee(1.0, proceeds)
            final_pnl     = round(proceeds - cost - entry_fee - self.exit_fee, 4)
        else:
            self.exit_fee = 0.0
            final_pnl     = round(-cost - entry_fee, 4)

        partials    = (self.tp1_pnl or 0.0) + (self.tp2_pnl or 0.0)
        self.pnl    = round(partials + final_pnl, 4)
        self.status = "WIN" if self.pnl > 0 else "LOSS"
        return self.pnl

    def close_market(self, exit_price: float, exit_reason: str) -> float:
        """Cierre al precio de mercado (SL o forzado). Opera sobre shares_remaining."""
        self.exit_price  = round(exit_price, 4)
        self.exit_reason = exit_reason

        sold_frac   = (TP1_FRAC if self.tp1_done else 0) + (TP2_FRAC if self.tp2_done else 0)
        remain_frac = max(0.0, 1.0 - sold_frac)

        shares      = self.shares_remaining
        cost        = self.bet_size  * remain_frac
        entry_fee   = self.entry_fee * remain_frac

        proceeds      = shares * exit_price
        self.exit_fee = estimate_fee(exit_price, proceeds)
        final_pnl     = round(proceeds - cost - entry_fee - self.exit_fee, 4)

        partials    = (self.tp1_pnl or 0.0) + (self.tp2_pnl or 0.0)
        self.pnl    = round(partials + final_pnl, 4)
        self.status = "WIN" if self.pnl > 0 else "LOSS"
        return self.pnl

    # ── Mark-to-market ───────────────────────────────────────────────────────

    def unrealized_pnl(self, current_price: float) -> float:
        """P&L neto total estimado: parciales realizados + unrealized del resto."""
        sold_frac   = (TP1_FRAC if self.tp1_done else 0) + (TP2_FRAC if self.tp2_done else 0)
        remain_frac = max(0.0, 1.0 - sold_frac)

        shares    = self.shares_remaining
        cost      = self.bet_size  * remain_frac
        entry_fee = self.entry_fee * remain_frac
        proceeds  = shares * current_price
        exit_fee  = estimate_fee(current_price, proceeds)

        partials = (self.tp1_pnl or 0.0) + (self.tp2_pnl or 0.0)
        return round(partials + proceeds - cost - entry_fee - exit_fee, 4)

    def mark_to_market(self, current_price: float) -> float:
        return round(self.shares_remaining * current_price, 4)

    def to_dict(self) -> dict:
        return {
            "id":              self.id,
            "market":          self.market,
            "direction":       self.direction,
            "entry_price":     self.entry_price,
            "shares":          round(self.shares, 4),
            "shares_remaining":round(self.shares_remaining, 4),
            "bet_size":        self.bet_size,
            "entry_time":      self.entry_time,
            "secs_at_entry":   round(self.secs_at_entry, 1),
            "entry_fee":       round(self.entry_fee, 4),
            # TP1
            "tp1_done":        self.tp1_done,
            "tp1_price_exec":  self.tp1_price_exec,
            "tp1_pnl":         self.tp1_pnl,
            # TP2
            "tp2_done":        self.tp2_done,
            "tp2_price_exec":  self.tp2_price_exec,
            "tp2_pnl":         self.tp2_pnl,
            # Cierre
            "exit_price":      self.exit_price,
            "exit_fee":        round(self.exit_fee, 4),
            "pnl":             self.pnl,
            "status":          self.status,
            "exit_reason":     self.exit_reason,
            # Referencia
            "tp1_price":       TP1_PRICE,
            "tp2_price":       TP2_PRICE,
            "sl_price":        SL_PRICE_ABS,
        }


# ── Portfolio ────────────────────────────────────────────────────────────────

class Portfolio:
    def __init__(self, initial_capital: float = INITIAL_CAPITAL,
                 trade_pct: float = TRADE_PCT, db=None):
        self.initial_capital  = initial_capital
        self.capital          = initial_capital
        self.trade_pct        = trade_pct
        self.active_trade: Optional[Trade] = None
        self.closed_trades: list[Trade]    = []
        self.pnl_history:   list[float]    = [0.0]
        self._trade_counter = 0
        self._db            = db

    def restore(self, saved: dict) -> None:
        self.capital         = saved["capital"]
        self.initial_capital = saved["initial_capital"]
        self.pnl_history     = saved["pnl_history"]
        self._trade_counter  = saved["trade_counter"]
        self.closed_trades   = saved["closed_trades"]

    # ── Entry ────────────────────────────────────────────────────────────────

    def consider_entry(self, signal: dict, market_question: str,
                       up_price: float, down_price: float,
                       secs_left: float = None,
                       entry_depth_up: dict = None,
                       entry_depth_down: dict = None,
                       up_bid: float = None,
                       down_bid: float = None) -> bool:

        if self.active_trade is not None:
            return False
        if self.capital < 1.0:
            return False
        if secs_left is None:
            return False
        if secs_left < MIN_SECS_TO_ENTER or secs_left > MAX_SECS_TO_ENTER:
            return False

        # Buscar lado barato (< 0.18)
        direction   = None
        entry_price = None
        entry_depth = None
        bid_price   = None

        if MIN_ENTRY_PRICE <= up_price <= MAX_ENTRY_PRICE:
            direction   = "UP"
            entry_price = up_price
            entry_depth = entry_depth_up
            bid_price   = up_bid
        elif MIN_ENTRY_PRICE <= down_price <= MAX_ENTRY_PRICE:
            direction   = "DOWN"
            entry_price = down_price
            entry_depth = entry_depth_down
            bid_price   = down_bid

        if direction is None:
            return False

        # Filtro spread
        if bid_price is not None and bid_price > 0 and entry_price > 0:
            if (entry_price - bid_price) / entry_price > MAX_ENTRY_SPREAD:
                return False

        bet_size = round(self.capital * self.trade_pct, 2)
        if entry_depth and entry_depth.get("shares", 0) > 0:
            entry_price = entry_depth["avg_price"]
            shares      = entry_depth["shares"]
        else:
            shares = round(bet_size / entry_price, 4) if entry_price > 0 else 0

        if shares <= 0:
            return False

        if not (MIN_ENTRY_PRICE <= entry_price <= MAX_ENTRY_PRICE):
            return False

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

    # ── Exit checks ──────────────────────────────────────────────────────────

    def check_exits(self, signal: dict, up_price: float, down_price: float,
                    secs_left) -> Optional[str]:
        if not self.active_trade:
            return None

        trade = self.active_trade
        cp    = self.current_price_for_trade(up_price, down_price)

        # SL siempre tiene prioridad
        if trade.should_sl(cp):
            return "SL"

        # TP1: 33% cuando llega a 0.33
        if trade.should_tp1(cp):
            return "TP1"

        # TP2: 33% cuando llega a 0.50 (solo si TP1 ya se hizo)
        if trade.should_tp2(cp):
            return "TP2"

        # TP3: el resto espera la resolución binaria (no hay exit manual)
        return None

    # ── Ejecutar salidas ─────────────────────────────────────────────────────

    def _recover_fraction_to_capital(self, frac: float, pnl: float):
        """Devuelve al capital el costo de la fracción vendida + su P&L."""
        cost_recovered = self.active_trade.bet_size * frac
        self.capital   = round(self.capital + cost_recovered + pnl, 4)

    def apply_tp1(self, up_price: float, down_price: float,
                  exit_bid_price: float = None) -> float:
        trade = self.active_trade
        cp    = exit_bid_price if exit_bid_price is not None \
                else self.current_price_for_trade(up_price, down_price)
        pnl   = trade.do_tp1(cp)
        self._recover_fraction_to_capital(TP1_FRAC, pnl)
        if self._db:
            self._db.save_trade(trade)
        return pnl

    def apply_tp2(self, up_price: float, down_price: float,
                  exit_bid_price: float = None) -> float:
        trade = self.active_trade
        cp    = exit_bid_price if exit_bid_price is not None \
                else self.current_price_for_trade(up_price, down_price)
        pnl   = trade.do_tp2(cp)
        self._recover_fraction_to_capital(TP2_FRAC, pnl)
        if self._db:
            self._db.save_trade(trade)
        return pnl

    def exit_at_market_price(self, up_price: float, down_price: float,
                             exit_reason: str,
                             exit_bid_price: float = None) -> Optional[Trade]:
        """Cierre total del tramo restante (SL o forzado)."""
        if not self.active_trade:
            return None
        trade = self.active_trade
        cp    = exit_bid_price if exit_bid_price is not None \
                else self.current_price_for_trade(up_price, down_price)

        pnl = trade.close_market(cp, exit_reason)

        sold_frac      = (TP1_FRAC if trade.tp1_done else 0) + (TP2_FRAC if trade.tp2_done else 0)
        remain_frac    = max(0.0, 1.0 - sold_frac)
        cost_remaining = trade.bet_size * remain_frac
        partials       = (trade.tp1_pnl or 0.0) + (trade.tp2_pnl or 0.0)
        final_pnl_only = pnl - partials
        self.capital   = round(self.capital + cost_remaining + final_pnl_only, 4)

        self._finalize_trade(trade)
        return trade

    def close_trade(self, up_price: float, down_price: float,
                    force_winner: Optional[bool] = None) -> Optional[Trade]:
        """Resolución binaria al expirar (TP3)."""
        if not self.active_trade:
            return None
        trade = self.active_trade

        if force_winner is not None:
            won = force_winner
        else:
            won = (up_price >= 0.5) if trade.direction == "UP" else (down_price >= 0.5)

        exit_price = 1.0 if won else 0.0
        pnl        = trade.close_binary(won, exit_price, "EXPIRE")

        sold_frac      = (TP1_FRAC if trade.tp1_done else 0) + (TP2_FRAC if trade.tp2_done else 0)
        remain_frac    = max(0.0, 1.0 - sold_frac)
        cost_remaining = trade.bet_size * remain_frac
        partials       = (trade.tp1_pnl or 0.0) + (trade.tp2_pnl or 0.0)
        final_pnl_only = pnl - partials
        self.capital   = round(self.capital + cost_remaining + final_pnl_only, 4)

        self._finalize_trade(trade)
        return trade

    def cancel_active_trade(self):
        if not self.active_trade:
            return
        trade = self.active_trade
        trade.status      = "CANCELLED"
        trade.exit_reason = "FORCED"
        sold_frac      = (TP1_FRAC if trade.tp1_done else 0) + (TP2_FRAC if trade.tp2_done else 0)
        remain_frac    = max(0.0, 1.0 - sold_frac)
        self.capital   = round(self.capital + trade.bet_size * remain_frac, 4)
        self.closed_trades.append(trade)
        if self._db:
            self._db.save_trade(trade)
            self._db.save_portfolio_state(
                self.capital, self.initial_capital,
                self.pnl_history, self._trade_counter,
            )
        self.active_trade = None

    def _finalize_trade(self, trade: Trade):
        self.closed_trades.append(trade)
        self.active_trade = None
        self.pnl_history.append(round(self.capital - self.initial_capital, 4))
        if self._db:
            self._db.save_trade(trade)
            self._db.save_portfolio_state(
                self.capital, self.initial_capital,
                self.pnl_history, self._trade_counter,
            )

    # ── Mark-to-market ───────────────────────────────────────────────────────

    def current_price_for_trade(self, up_price: float, down_price: float) -> float:
        if not self.active_trade:
            return 0.0
        return up_price if self.active_trade.direction == "UP" else down_price

    def get_unrealized(self, up_price: float, down_price: float) -> float:
        if not self.active_trade:
            return 0.0
        cp = self.current_price_for_trade(up_price, down_price)
        return self.active_trade.unrealized_pnl(cp)

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
        # unrealized_pnl() ya incluye tp1_pnl + tp2_pnl internamente — NO sumar por separado
        total_pnl = round(realized_pnl + unrealized_pnl, 4)

        # Equity: capital libre + costo de shares restantes en juego + mark-to-market de esas shares
        # unrealized_pnl() incluye partials que ya están en capital → usar solo mark-to-market del resto
        if self.active_trade:
            t = self.active_trade
            cp_eq = self.current_price_for_trade(up_price, down_price)
            sold_frac   = (TP1_FRAC if t.tp1_done else 0) + (TP2_FRAC if t.tp2_done else 0)
            remain_frac = max(0.0, 1.0 - sold_frac)
            bet_in_play = t.bet_size * remain_frac
            mtm         = t.mark_to_market(cp_eq)        # valor actual shares restantes
            entry_fee_r = t.entry_fee * remain_frac
            equity      = round(self.capital + bet_in_play + mtm - entry_fee_r, 4)
        else:
            equity = round(self.capital, 4)

        # Active trade info
        active = None
        if self.active_trade:
            t  = self.active_trade
            cp = self.current_price_for_trade(up_price, down_price)
            active = {
                **t.to_dict(),
                "current_price":   round(cp, 4),
                "mark_to_market":  t.mark_to_market(cp),
                "unrealized_pnl":  t.unrealized_pnl(cp),
                "tp1_price":       TP1_PRICE,
                "tp2_price":       TP2_PRICE,
                "sl_price":        SL_PRICE_ABS,
                "dist_to_tp1":     round(TP1_PRICE - cp, 4) if not t.tp1_done else None,
                "dist_to_tp2":     round(TP2_PRICE - cp, 4) if not t.tp2_done else None,
                "dist_to_sl":      round(cp - SL_PRICE_ABS, 4),
                # Para compatibilidad con el dashboard
                "tp_price":        TP2_PRICE,
                "partial_exit_done": t.tp1_done,
                "partial_exit_price": t.tp1_price_exec,
                "partial_pnl":     (t.tp1_pnl or 0.0) + (t.tp2_pnl or 0.0),
                "shares_remaining": round(t.shares_remaining, 4),
            }

        exit_reasons: dict = {}
        for t in closed:
            r = t.exit_reason or "EXPIRE"
            exit_reasons[r] = exit_reasons.get(r, 0) + 1

        return {
            "initial_capital":  self.initial_capital,
            "capital":          round(self.capital, 4),
            "equity":           equity,
            "realized_pnl":     round(realized_pnl, 4),
            "unrealized_pnl":   round(unrealized_pnl, 4),
            "total_pnl":        total_pnl,
            "total_pnl_pct":    round(total_pnl / self.initial_capital * 100, 2),
            "total_fees_paid":  round(total_fees, 4),
            "total_trades":     len(closed),
            "wins":             len(wins),
            "losses":           len(losses),
            "cancelled":        len([t for t in closed if t.status == "CANCELLED"]),
            "tp1_exits":        sum(1 for t in closed if t.tp1_done),
            "tp2_exits":        sum(1 for t in closed if t.tp2_done),
            "win_rate":         win_rate,
            "best_trade":       round(max((t.pnl for t in closed if t.pnl), default=0), 4),
            "worst_trade":      round(min((t.pnl for t in closed if t.pnl), default=0), 4),
            "avg_pnl":          round(realized_pnl / n_closed, 4) if n_closed else 0,
            "pnl_history":      self.pnl_history[-50:],
            "active_trade":     active,
            "trade_log":        [t.to_dict() for t in reversed(closed[-20:])],
            "exit_reasons":     exit_reasons,
            "strategy": {
                "name":           "Deep Value 3-TP v4",
                "entry_zone":     f"{MIN_ENTRY_PRICE}–{MAX_ENTRY_PRICE}",
                "tp1_price":      TP1_PRICE,
                "tp2_price":      TP2_PRICE,
                "sl_price":       SL_PRICE_ABS,
                "tp1_frac":       round(TP1_FRAC, 4),
                "tp2_frac":       round(TP2_FRAC, 4),
                "min_secs_left":  MIN_SECS_TO_ENTER,
                "max_secs_left":  MAX_SECS_TO_ENTER,
                "trade_pct":      TRADE_PCT,
            },
        }
