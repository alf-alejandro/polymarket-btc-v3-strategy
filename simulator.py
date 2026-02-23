"""
simulator.py — Portfolio simulation v8: Mean Reversion, código limpio

Estrategia: comprar token barato (0.15–0.35) en primeros 2 minutos,
esperar que suba a 0.47 (TP parcial 50%), dejar el resto a resolución binaria.

Fixes v8:
  - Eliminado cierre anticipado por tiempo ("LATE" a los 20s)
  - Tras el PARTIAL_TP, shares_remaining esperan resolución binaria (0 o 1)
  - SL sigue activo en todo momento para proteger el resto del capital
"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional


INITIAL_CAPITAL   = 100.0
TRADE_PCT         = 0.02      # 2% por trade

# ── Filtros de entrada ─────────────────────────────────────────────────────────
MIN_ENTRY_PRICE   = 0.15      # no entrar por debajo de 15¢
MAX_ENTRY_PRICE   = 0.35      # zona barata: token < 35¢
MAX_ENTRY_SPREAD  = 0.08      # spread máximo tolerable

# Ventana: solo primeros 2 minutos (observación empírica)
MIN_SECS_TO_ENTER = 180       # no entrar si quedan < 3 minutos
MAX_SECS_TO_ENTER = 300       # desde el primer segundo

# ── Salida ─────────────────────────────────────────────────────────────────────
TARGET_PRICE      = 0.47      # TP parcial: vender 50% cuando token llega a 0.47
PARTIAL_EXIT_PCT  = 0.50      # % a vender en TP parcial
SL_PRICE_ABS      = 0.10      # SL: si el token cae a 10¢, cortar todo

# ── Fee model ──────────────────────────────────────────────────────────────────
FEE_RATE = 0.0625             # fee = p × (1-p) × FEE_RATE


def estimate_fee(price: float, usdc_amount: float) -> float:
    rate = max(0.0, price) * max(0.0, 1 - price) * FEE_RATE
    return round(rate * usdc_amount, 6)


@dataclass
class Trade:
    id:             int
    market:         str
    direction:      str           # "UP" or "DOWN"
    entry_price:    float         # precio del token al entrar (0–1)
    shares:         float         # total shares compradas
    bet_size:       float         # USDC gastados
    entry_time:     str
    secs_at_entry:  float = 0.0

    # Estado parcial
    partial_exit_done:  bool            = field(default=False, repr=False)
    shares_remaining:   float           = field(default=0.0,   repr=False)
    partial_exit_price: Optional[float] = None
    partial_pnl:        Optional[float] = None

    # Resultado final
    entry_fee:   float          = 0.0
    exit_price:  Optional[float] = None
    exit_fee:    float          = 0.0
    pnl:         Optional[float] = None
    status:      str            = "OPEN"
    exit_reason: Optional[str]  = None

    def __post_init__(self):
        self.entry_fee       = estimate_fee(self.entry_price, self.bet_size)
        self.shares_remaining = self.shares

    # ── Lógica de TP/SL ───────────────────────────────────────────────────────
    #
    # Para UP:   compramos a 0.25, ganamos si sube hacia 0.50+
    #   TP: current_price >= TARGET_PRICE (0.47)   → sube ✓
    #   SL: current_price <= SL_PRICE_ABS  (0.10)  → baja mucho ✓
    #
    # Para DOWN: compramos token DOWN a 0.25, ganamos si ese token sube hacia 0.50+
    #   El token DOWN sube cuando el mercado se equilibra (SOL no sube tanto)
    #   TP: current_price >= TARGET_PRICE (0.47)   → token DOWN sube ✓
    #   SL: current_price <= SL_PRICE_ABS  (0.10)  → token DOWN baja mucho ✓
    #
    # En ambos casos current_price es el precio del TOKEN que compramos,
    # no el precio del activo subyacente. La lógica es simétrica.

    def should_partial_exit(self, current_price: float) -> bool:
        if self.partial_exit_done:
            return False
        return current_price >= TARGET_PRICE   # aplica igual para UP y DOWN

    def should_sl(self, current_price: float) -> bool:
        return current_price <= SL_PRICE_ABS   # aplica igual para UP y DOWN

    # ── Operaciones ───────────────────────────────────────────────────────────

    def do_partial_exit(self, exit_price: float) -> float:
        """Vende PARTIAL_EXIT_PCT shares. Retorna P&L neto de esta venta."""
        shares_to_sell    = round(self.shares * PARTIAL_EXIT_PCT, 4)
        proceeds          = shares_to_sell * exit_price
        fee               = estimate_fee(exit_price, proceeds)
        entry_fee_portion = self.entry_fee * PARTIAL_EXIT_PCT
        cost_portion      = self.bet_size * PARTIAL_EXIT_PCT
        pnl               = round(proceeds - cost_portion - entry_fee_portion - fee, 4)

        self.partial_exit_done  = True
        self.partial_exit_price = round(exit_price, 4)
        self.partial_pnl        = pnl
        self.shares_remaining   = round(self.shares - shares_to_sell, 4)
        return pnl

    def close_binary(self, won: bool, exit_price: float,
                     exit_reason: str = "EXPIRE") -> float:
        """Resolución binaria al expirar. Opera sobre shares_remaining."""
        self.exit_price  = exit_price
        self.exit_reason = exit_reason

        if self.partial_exit_done:
            shares    = self.shares_remaining
            cost      = self.bet_size * (1 - PARTIAL_EXIT_PCT)
            entry_fee = self.entry_fee * (1 - PARTIAL_EXIT_PCT)
        else:
            shares    = self.shares
            cost      = self.bet_size
            entry_fee = self.entry_fee

        if won:
            proceeds      = shares * 1.0
            self.exit_fee = estimate_fee(1.0, proceeds)
            final_pnl     = round(proceeds - cost - entry_fee - self.exit_fee, 4)
        else:
            self.exit_fee = 0.0
            final_pnl     = round(-cost - entry_fee, 4)

        partial = self.partial_pnl or 0.0
        self.pnl    = round(partial + final_pnl, 4)
        self.status = "WIN" if self.pnl > 0 else "LOSS"
        return self.pnl

    def close_market(self, exit_price: float, exit_reason: str) -> float:
        """Cierre al precio de mercado (SL, LATE). Opera sobre shares_remaining."""
        self.exit_price  = round(exit_price, 4)
        self.exit_reason = exit_reason

        if self.partial_exit_done:
            shares    = self.shares_remaining
            cost      = self.bet_size * (1 - PARTIAL_EXIT_PCT)
            entry_fee = self.entry_fee * (1 - PARTIAL_EXIT_PCT)
        else:
            shares    = self.shares
            cost      = self.bet_size
            entry_fee = self.entry_fee

        proceeds      = shares * exit_price
        self.exit_fee = estimate_fee(exit_price, proceeds)
        final_pnl     = round(proceeds - cost - entry_fee - self.exit_fee, 4)

        partial     = self.partial_pnl or 0.0
        self.pnl    = round(partial + final_pnl, 4)
        self.status = "WIN" if self.pnl > 0 else "LOSS"
        return self.pnl

    # ── Mark-to-market ────────────────────────────────────────────────────────

    def unrealized_pnl(self, current_price: float) -> float:
        """P&L neto estimado de las shares restantes (descontando fees)."""
        shares    = self.shares_remaining if self.partial_exit_done else self.shares
        cost      = self.bet_size * (1 - PARTIAL_EXIT_PCT if self.partial_exit_done else 1.0)
        entry_fee = self.entry_fee * (1 - PARTIAL_EXIT_PCT if self.partial_exit_done else 1.0)
        proceeds  = shares * current_price
        exit_fee  = estimate_fee(current_price, proceeds)
        partial   = self.partial_pnl or 0.0
        return round(partial + proceeds - cost - entry_fee - exit_fee, 4)

    def mark_to_market(self, current_price: float) -> float:
        shares = self.shares_remaining if self.partial_exit_done else self.shares
        return round(shares * current_price, 4)

    def to_dict(self) -> dict:
        return {
            "id":                  self.id,
            "market":              self.market,
            "direction":           self.direction,
            "entry_price":         self.entry_price,
            "shares":              round(self.shares, 4),
            "shares_remaining":    round(self.shares_remaining, 4),
            "bet_size":            self.bet_size,
            "entry_time":          self.entry_time,
            "secs_at_entry":       round(self.secs_at_entry, 1),
            "entry_fee":           round(self.entry_fee, 4),
            "partial_exit_done":   self.partial_exit_done,
            "partial_exit_price":  self.partial_exit_price,
            "partial_pnl":         self.partial_pnl,
            "exit_price":          self.exit_price,
            "exit_fee":            round(self.exit_fee, 4),
            "pnl":                 self.pnl,
            "status":              self.status,
            "exit_reason":         self.exit_reason,
            "tp_price":            TARGET_PRICE,
            "sl_price":            SL_PRICE_ABS,
        }


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

    # ── Entry ─────────────────────────────────────────────────────────────────

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

        # Filtro tiempo: solo primeros 2 minutos
        if secs_left is None:
            return False
        if secs_left < MIN_SECS_TO_ENTER or secs_left > MAX_SECS_TO_ENTER:
            return False

        # Buscar lado barato
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

        # Precio real del depth walk
        bet_size = round(self.capital * self.trade_pct, 2)
        if entry_depth and entry_depth.get("shares", 0) > 0:
            entry_price = entry_depth["avg_price"]
            shares      = entry_depth["shares"]
        else:
            shares = round(bet_size / entry_price, 4) if entry_price > 0 else 0

        if shares <= 0:
            return False

        # Verificar que tras el depth walk sigue en zona barata
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

    # ── Exit checks ───────────────────────────────────────────────────────────

    def check_exits(self, signal: dict, up_price: float, down_price: float,
                    secs_left) -> Optional[str]:
        if not self.active_trade:
            return None

        trade         = self.active_trade
        current_price = self.current_price_for_trade(up_price, down_price)

        # 1. TP parcial — token llegó a 0.47+
        # Solo aplica si aún no se hizo el parcial
        if trade.should_partial_exit(current_price):
            return "PARTIAL_TP"

        # 2. SL — token cayó a 0.10 o menos
        if trade.should_sl(current_price):
            return "SL"

        # El resto (shares_remaining tras el parcial) se deja a resolución binaria.
        # No hay cierre anticipado por tiempo: el contrato expira y paga 0 o 1.
        return None

    # ── Ejecutar salidas ──────────────────────────────────────────────────────

    def apply_partial_exit(self, up_price: float, down_price: float,
                           exit_bid_price: float = None) -> float:
        """Vende 50% de las shares. Retorna el P&L de esa venta."""
        trade = self.active_trade
        cp    = exit_bid_price if exit_bid_price is not None \
                else self.current_price_for_trade(up_price, down_price)

        partial_pnl = trade.do_partial_exit(cp)

        # Devolver al capital: costo de las shares vendidas + P&L de esa venta
        cost_recovered = trade.bet_size * PARTIAL_EXIT_PCT
        self.capital   = round(self.capital + cost_recovered + partial_pnl, 4)

        if self._db:
            self._db.save_trade(trade)
        return partial_pnl

    def exit_at_market_price(self, up_price: float, down_price: float,
                             exit_reason: str,
                             exit_bid_price: float = None) -> Optional[Trade]:
        """Cierre total de lo que queda (después del parcial o sin él)."""
        if not self.active_trade:
            return None
        trade = self.active_trade
        cp    = exit_bid_price if exit_bid_price is not None \
                else self.current_price_for_trade(up_price, down_price)

        pnl = trade.close_market(cp, exit_reason)

        # Capital: recuperar el costo de las shares restantes + P&L de esa venta
        # (el parcial ya fue devuelto en apply_partial_exit)
        if trade.partial_exit_done:
            cost_remaining = trade.bet_size * (1 - PARTIAL_EXIT_PCT)
            final_pnl_only = pnl - (trade.partial_pnl or 0.0)
            self.capital   = round(self.capital + cost_remaining + final_pnl_only, 4)
        else:
            self.capital   = round(self.capital + trade.bet_size + pnl, 4)

        self._finalize_trade(trade)
        return trade

    def close_trade(self, up_price: float, down_price: float,
                    force_winner: Optional[bool] = None) -> Optional[Trade]:
        """Resolución binaria al expirar el mercado."""
        if not self.active_trade:
            return None
        trade = self.active_trade

        if force_winner is not None:
            won = force_winner
        else:
            # Para UP: ganamos si up_price >= 0.5 (SOL subió)
            # Para DOWN: ganamos si down_price >= 0.5 (SOL bajó)
            won = (up_price >= 0.5) if trade.direction == "UP" else (down_price >= 0.5)

        exit_price = 1.0 if won else 0.0
        pnl        = trade.close_binary(won, exit_price, "EXPIRE")

        if trade.partial_exit_done:
            cost_remaining = trade.bet_size * (1 - PARTIAL_EXIT_PCT)
            final_pnl_only = pnl - (trade.partial_pnl or 0.0)
            self.capital   = round(self.capital + cost_remaining + final_pnl_only, 4)
        else:
            self.capital   = round(self.capital + trade.bet_size + pnl, 4)

        self._finalize_trade(trade)
        return trade

    def cancel_active_trade(self):
        if not self.active_trade:
            return
        self.active_trade.status      = "CANCELLED"
        self.active_trade.exit_reason = "FORCED"
        # Devolver el bet restante al capital
        if self.active_trade.partial_exit_done:
            self.capital = round(self.capital
                                 + self.active_trade.bet_size * (1 - PARTIAL_EXIT_PCT), 4)
        else:
            self.capital = round(self.capital + self.active_trade.bet_size, 4)
        self.closed_trades.append(self.active_trade)
        if self._db:
            self._db.save_trade(self.active_trade)
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
        equity         = round(self.capital + unrealized_pnl
                               + (self.active_trade.bet_size * (1 - PARTIAL_EXIT_PCT)
                                  if self.active_trade and self.active_trade.partial_exit_done
                                  else (self.active_trade.bet_size if self.active_trade else 0)),
                               4)

        active = None
        if self.active_trade:
            t  = self.active_trade
            cp = self.current_price_for_trade(up_price, down_price)
            active = {
                **t.to_dict(),
                "current_price":      round(cp, 4),
                "mark_to_market":     t.mark_to_market(cp),
                "unrealized_pnl":     t.unrealized_pnl(cp),
                "tp_price":           TARGET_PRICE,
                "sl_price":           SL_PRICE_ABS,
                "dist_to_tp":         round(TARGET_PRICE - cp, 4),
                "dist_to_sl":         round(cp - SL_PRICE_ABS, 4),
                "partial_exit_done":  t.partial_exit_done,
                "partial_exit_price": t.partial_exit_price,
                "partial_pnl":        t.partial_pnl,
                "tp_pnl": round(
                    t.shares * TARGET_PRICE - t.bet_size - t.entry_fee
                    - estimate_fee(TARGET_PRICE, t.shares * TARGET_PRICE), 4
                ) if not t.partial_exit_done else None,
                "sl_pnl": round(
                    t.shares * SL_PRICE_ABS - t.bet_size - t.entry_fee
                    - estimate_fee(SL_PRICE_ABS, t.shares * SL_PRICE_ABS), 4
                ) if not t.partial_exit_done else None,
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
            "partial_exits":    sum(1 for t in closed if t.partial_exit_done),
            "win_rate":         win_rate,
            "best_trade":       round(max((t.pnl for t in closed if t.pnl), default=0), 4),
            "worst_trade":      round(min((t.pnl for t in closed if t.pnl), default=0), 4),
            "avg_pnl":          round(realized_pnl / n_closed, 4) if n_closed else 0,
            "pnl_history":      self.pnl_history[-50:],
            "active_trade":     active,
            "trade_log":        [t.to_dict() for t in reversed(closed[-20:])],
            "exit_reasons":     exit_reasons,
            "strategy": {
                "name":             "Mean Reversion v7",
                "entry_zone":       f"{MIN_ENTRY_PRICE}–{MAX_ENTRY_PRICE}",
                "target_price":     TARGET_PRICE,
                "sl_price":         SL_PRICE_ABS,
                "partial_exit_pct": PARTIAL_EXIT_PCT,
                "min_secs_left":    MIN_SECS_TO_ENTER,
                "max_secs_left":    MAX_SECS_TO_ENTER,
                "trade_pct":        TRADE_PCT,
            },
        }
