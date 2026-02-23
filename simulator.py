"""
simulator.py — Portfolio simulation v6: Mean Reversion Strategy

ESTRATEGIA CENTRAL:
  Comprar tokens baratos (0.20–0.35) en los primeros 2 minutos del ciclo
  y esperar que graviten hacia 0.50. Tomar ganancia parcial en 0.50,
  dejar el resto correr a resolución binaria.

LÓGICA:
  En los primeros 2 minutos de un mercado de 5min, hay incertidumbre genuina.
  Un token a 0.20 dice "80% de probabilidad de bajar" — pero esa estimación
  es muy imprecisa tan temprano. El precio tiende a gravitarse hacia 0.50
  mientras la información llega al mercado. Ese es el movimiento a capturar.

  En el último minuto el mercado YA DECIDIÓ. Entrar ahí es apostar contra
  certeza — exactamente el bug que producía los trades #2 y #3 del log.

FILTROS DUROS:
  - Solo entrar en primeros 240s del ciclo (primeros 4 minutos de 5)
  - Solo entrar si el token está en zona barata: precio < MAX_ENTRY_PRICE
  - No entrar si el precio ya salió de la zona barata (mercado decidió)
  - No entrar si quedan < MIN_SECS_TO_ENTER segundos
  - No entrar si spread > MAX_ENTRY_SPREAD

SALIDA EN DOS ETAPAS:
  Etapa 1 (TP parcial): cuando el token llega a TARGET_PRICE (0.47–0.50),
    vender 50% de la posición y asegurar ganancia.
  Etapa 2: dejar correr el 50% restante a resolución binaria.
    Si resuelve a 1.0 → jackpot. Si resuelve a 0.0 → ya aseguramos la mitad.

  Esto hace que el EV sea positivo incluso con win rate bajo:
    - Si toca 0.50 y resuelve UP (50%): ganamos en ambas etapas
    - Si toca 0.50 y resuelve DOWN (50%): ganamos etapa 1, perdemos etapa 2
    - Si nunca toca 0.50 y resuelve UP: ganamos (precio sube a 1.0)
    - Si nunca toca 0.50 y resuelve DOWN: perdemos todo
"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional


INITIAL_CAPITAL  = 100.0
TRADE_PCT        = 0.04       # 4% por trade: tamaño mayor porque el riesgo es asimétrico

# ── Filtros de entrada ─────────────────────────────────────────────────────────
MIN_ENTRY_PRICE  = 0.15       # no entrar por debajo de 15¢ (demasiado extremo)
MAX_ENTRY_PRICE  = 0.35       # zona barata: solo entrar si token < 35¢
MAX_ENTRY_SPREAD = 0.08       # spread máximo tolerable al entrar
MIN_SECS_TO_ENTER = 60        # no entrar si quedan menos de 60s (mercado decidió)
MAX_SECS_TO_ENTER = 240       # no entrar después de los primeros 4 minutos

# ── Salida ─────────────────────────────────────────────────────────────────────
TARGET_PRICE     = 0.47       # TP parcial: vender 50% cuando llega cerca de 0.50
PARTIAL_EXIT_PCT = 0.50       # porcentaje a vender en el TP parcial
SL_PRICE_ABS     = 0.10       # SL absoluto: si el token cae a 10¢, cortar todo
                               # (significa que el mercado decidió muy en contra)

# ── Fee model ──────────────────────────────────────────────────────────────────
FEE_RATE = 0.0625   # fee = p × (1-p) × FEE_RATE


def estimate_fee(price: float, usdc_amount: float) -> float:
    rate = price * (1 - price) * FEE_RATE
    return round(rate * usdc_amount, 6)


@dataclass
class Trade:
    id:            int
    market:        str
    direction:     str            # "UP" or "DOWN"
    entry_price:   float
    shares:        float
    bet_size:      float
    entry_time:    str
    secs_at_entry: float = 0.0    # segundos restantes cuando entró

    # Estado de salida parcial
    partial_exit_done:  bool  = field(default=False, repr=False)
    shares_remaining:   float = field(default=0.0,   repr=False)
    partial_exit_price: Optional[float] = None
    partial_pnl:        Optional[float] = None

    # Salida final
    entry_fee:   float = 0.0
    exit_price:  Optional[float] = None
    exit_fee:    float = 0.0
    pnl:         Optional[float] = None
    pnl_gross:   Optional[float] = None
    status:      str = "OPEN"
    exit_reason: Optional[str] = None

    def __post_init__(self):
        self.entry_fee      = estimate_fee(self.entry_price, self.bet_size)
        self.shares_remaining = self.shares

    # ── Checks ────────────────────────────────────────────────────────────────

    def should_partial_exit(self, current_price: float) -> bool:
        """¿El precio llegó a la zona de TP parcial?"""
        if self.partial_exit_done:
            return False
        if self.direction == "UP":
            return current_price >= TARGET_PRICE
        else:  # DOWN: compramos token DOWN barato, esperamos suba a 0.50
            return current_price >= TARGET_PRICE

    def should_sl(self, current_price: float) -> bool:
        """SL duro: el mercado se fue muy en contra."""
        return current_price <= SL_PRICE_ABS

    def do_partial_exit(self, exit_price: float) -> float:
        """
        Vende PARTIAL_EXIT_PCT de las shares al precio actual.
        Retorna el P&L de esta salida parcial.
        """
        shares_to_sell      = round(self.shares * PARTIAL_EXIT_PCT, 4)
        proceeds            = shares_to_sell * exit_price
        fee                 = estimate_fee(exit_price, proceeds)
        # Fee de entrada proporcional a las shares que salen
        entry_fee_portion   = self.entry_fee * PARTIAL_EXIT_PCT
        pnl                 = round(proceeds - (self.bet_size * PARTIAL_EXIT_PCT)
                                    - entry_fee_portion - fee, 4)

        self.partial_exit_done  = True
        self.partial_exit_price = round(exit_price, 4)
        self.partial_pnl        = pnl
        self.shares_remaining   = round(self.shares - shares_to_sell, 4)
        return pnl

    def close_binary(self, won: bool, exit_price: float, exit_reason: str = "EXPIRE") -> float:
        """Resolución binaria del resto de shares (o todas si no hubo parcial)."""
        self.exit_price  = exit_price
        self.exit_reason = exit_reason

        if self.partial_exit_done:
            # Solo liquidamos las shares restantes
            shares = self.shares_remaining
            bet_remaining = self.bet_size * (1 - PARTIAL_EXIT_PCT)
            entry_fee_remaining = self.entry_fee * (1 - PARTIAL_EXIT_PCT)
        else:
            shares = self.shares
            bet_remaining = self.bet_size
            entry_fee_remaining = self.entry_fee

        if won:
            proceeds       = shares * 1.0
            self.exit_fee  = estimate_fee(1.0, proceeds)
            final_pnl      = round(proceeds - bet_remaining - entry_fee_remaining - self.exit_fee, 4)
        else:
            self.exit_fee  = 0.0
            final_pnl      = round(-bet_remaining - entry_fee_remaining, 4)

        # P&L total = parcial (si hubo) + final
        partial = self.partial_pnl or 0.0
        self.pnl       = round(partial + final_pnl, 4)
        self.pnl_gross = self.pnl  # simplificado
        self.status    = "WIN" if self.pnl > 0 else "LOSS"
        return self.pnl

    def close_market(self, exit_price: float, exit_reason: str) -> float:
        """Salida al precio de mercado (SL)."""
        self.exit_price  = round(exit_price, 4)
        self.exit_reason = exit_reason

        shares       = self.shares_remaining if self.partial_exit_done else self.shares
        bet_rem      = self.bet_size * (1 - PARTIAL_EXIT_PCT if self.partial_exit_done else 1.0)
        entry_fee_rem = self.entry_fee * (1 - PARTIAL_EXIT_PCT if self.partial_exit_done else 1.0)

        proceeds      = shares * exit_price
        self.exit_fee = estimate_fee(exit_price, proceeds)
        final_pnl     = round(proceeds - bet_rem - entry_fee_rem - self.exit_fee, 4)

        partial       = self.partial_pnl or 0.0
        self.pnl      = round(partial + final_pnl, 4)
        self.pnl_gross = self.pnl
        self.status   = "WIN" if self.pnl > 0 else "LOSS"
        return self.pnl

    def unrealized_pnl(self, current_price: float) -> float:
        shares    = self.shares_remaining if self.partial_exit_done else self.shares
        bet_rem   = self.bet_size * (1 - PARTIAL_EXIT_PCT if self.partial_exit_done else 1.0)
        ef_rem    = self.entry_fee * (1 - PARTIAL_EXIT_PCT if self.partial_exit_done else 1.0)
        proceeds  = shares * current_price
        exit_fee  = estimate_fee(current_price, proceeds)
        partial   = self.partial_pnl or 0.0
        return round(partial + proceeds - bet_rem - ef_rem - exit_fee, 4)

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
            "sl_price":            SL_PRICE_ABS,
            "target_price":        TARGET_PRICE,
        }


class Portfolio:
    def __init__(self, initial_capital: float = INITIAL_CAPITAL,
                 trade_pct: float = TRADE_PCT, db=None):
        self.initial_capital   = initial_capital
        self.capital           = initial_capital
        self.trade_pct         = trade_pct
        self.active_trade: Optional[Trade]  = None
        self.closed_trades: list[Trade]     = []
        self.pnl_history:   list[float]     = [0.0]
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

        # ── FILTRO 1: tiempo — solo entrar en los primeros 4 minutos ──────────
        if secs_left is None:
            return False
        if secs_left < MIN_SECS_TO_ENTER:
            return False   # mercado ya decidió, no tocar
        if secs_left > MAX_SECS_TO_ENTER:
            return False   # demasiado pronto (raro, pero por si acaso)

        # ── FILTRO 2: buscar el lado barato ───────────────────────────────────
        # ¿Hay algún token en zona de compra (precio < MAX_ENTRY_PRICE)?
        direction    = None
        entry_price  = None
        entry_depth  = None
        bid_price    = None

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
            return False   # ningún token en zona barata

        # ── FILTRO 3: spread ──────────────────────────────────────────────────
        if bid_price is not None and bid_price > 0 and entry_price > 0:
            spread_pct = (entry_price - bid_price) / entry_price
            if spread_pct > MAX_ENTRY_SPREAD:
                return False

        # ── FILTRO 4: usar precio real del depth walk si está disponible ──────
        if entry_depth and entry_depth.get("shares", 0) > 0:
            entry_price = entry_depth["avg_price"]
            shares      = entry_depth["shares"]
        else:
            bet_size = round(self.capital * self.trade_pct, 2)
            shares   = round(bet_size / entry_price, 4)

        bet_size = round(self.capital * self.trade_pct, 2)

        # Verificar que la entrada sigue siendo barata después del depth walk
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

        # 1. TP parcial: el token llegó a 0.47+ → vender 50%
        if trade.should_partial_exit(current_price):
            return "PARTIAL_TP"

        # 2. SL duro: token cayó a 10¢ o menos → el mercado decidió, cortar
        if trade.should_sl(current_price):
            return "SL"

        # 3. Tiempo crítico: si quedan < 30s y el token NO llegó al TP,
        #    salir con lo que haya si estamos en ganancia.
        #    Si estamos en pérdida, aguantar a resolución binaria
        #    (ya entramos barato, la resolución nos puede dar jackpot)
        if secs_left is not None and secs_left <= 30:
            upnl = trade.unrealized_pnl(current_price)
            if upnl > 0:
                return "LATE"
            # Si estamos en pérdida, NO salir — dejar resolver binary

        return None

    def apply_partial_exit(self, up_price: float, down_price: float,
                           exit_bid_price: float = None) -> Optional[float]:
        """Ejecuta la salida parcial. Retorna el P&L de la salida parcial."""
        if not self.active_trade:
            return None
        trade = self.active_trade
        cp    = exit_bid_price if exit_bid_price is not None \
                else self.current_price_for_trade(up_price, down_price)
        partial_pnl = trade.do_partial_exit(cp)
        # Devolver el 50% del bet + P&L parcial al capital
        returned = round(trade.bet_size * PARTIAL_EXIT_PCT + partial_pnl, 4)
        self.capital = round(self.capital + returned, 4)
        if self._db:
            self._db.save_trade(trade)
        return partial_pnl

    def exit_at_market_price(self, up_price: float, down_price: float,
                             exit_reason: str,
                             exit_bid_price: float = None) -> Optional[Trade]:
        if not self.active_trade:
            return None
        trade = self.active_trade
        cp    = exit_bid_price if exit_bid_price is not None \
                else self.current_price_for_trade(up_price, down_price)
        pnl   = trade.close_market(cp, exit_reason)

        # Devolver el bet restante + P&L de las shares restantes
        bet_remaining = trade.bet_size * (1 - PARTIAL_EXIT_PCT if trade.partial_exit_done else 1.0)
        self.capital  = round(self.capital + bet_remaining + pnl
                              - (trade.partial_pnl or 0.0), 4)
        # Corrección: capital ya recibió el parcial antes, solo agregar el final neto
        self.capital  = round(self.capital + pnl - (trade.partial_pnl or 0.0), 4)

        self._finalize_trade(trade)
        return trade

    def exit_at_market_price(self, up_price: float, down_price: float,
                             exit_reason: str,
                             exit_bid_price: float = None) -> Optional[Trade]:
        """Cierra el trade completo (o lo que queda después del parcial)."""
        if not self.active_trade:
            return None
        trade = self.active_trade
        cp    = exit_bid_price if exit_bid_price is not None \
                else self.current_price_for_trade(up_price, down_price)
        pnl   = trade.close_market(cp, exit_reason)

        # El partial ya fue devuelto al capital en apply_partial_exit.
        # Aquí solo devolvemos el bet restante + P&L final de esas shares.
        if trade.partial_exit_done:
            bet_remaining = trade.bet_size * (1 - PARTIAL_EXIT_PCT)
            # pnl ya incluye el parcial en close_market, descontar para no duplicar
            final_only = pnl - (trade.partial_pnl or 0.0)
            self.capital = round(self.capital + bet_remaining + final_only, 4)
        else:
            self.capital = round(self.capital + trade.bet_size + pnl, 4)

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
            won = (up_price >= 0.5) if trade.direction == "UP" else (down_price >= 0.5)

        exit_price = 1.0 if won else 0.0
        pnl        = trade.close_binary(won, exit_price, "EXPIRE")

        if trade.partial_exit_done:
            bet_remaining = trade.bet_size * (1 - PARTIAL_EXIT_PCT)
            final_only    = pnl - (trade.partial_pnl or 0.0)
            self.capital  = round(self.capital + bet_remaining + final_only, 4)
        else:
            self.capital  = round(self.capital + trade.bet_size + pnl, 4)

        self._finalize_trade(trade)
        return trade

    def _finalize_trade(self, trade: Trade):
        total_pnl = round(self.capital - self.initial_capital, 4)
        self.pnl_history.append(total_pnl)
        self.closed_trades.append(trade)
        self.active_trade = None
        if self._db:
            self._db.save_trade(trade)
            self._db.save_portfolio_state(
                self.capital, self.initial_capital,
                self.pnl_history, self._trade_counter,
            )

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
        self.active_trade = None

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

        realized_pnl = sum(t.pnl for t in closed if t.pnl is not None)
        total_fees   = sum((t.entry_fee + t.exit_fee) for t in closed)
        partial_wins = sum(1 for t in closed if t.partial_exit_done)

        unrealized_pnl = self.get_unrealized(up_price, down_price)
        total_pnl      = round(realized_pnl + unrealized_pnl, 4)
        equity         = round(
            self.capital
            + (self.active_trade.bet_size * (1 - PARTIAL_EXIT_PCT)
               if self.active_trade and self.active_trade.partial_exit_done
               else (self.active_trade.bet_size if self.active_trade else 0))
            + unrealized_pnl, 4,
        )

        active = None
        if self.active_trade:
            t  = self.active_trade
            cp = self.current_price_for_trade(up_price, down_price)
            active = {
                **t.to_dict(),
                "current_price":     round(cp, 4),
                "mark_to_market":    t.mark_to_market(cp),
                "unrealized_pnl":    t.unrealized_pnl(cp),
                "target_price":      TARGET_PRICE,
                "sl_price":          SL_PRICE_ABS,
                "dist_to_target":    round(TARGET_PRICE - cp, 4),
                "dist_to_sl":        round(cp - SL_PRICE_ABS, 4),
                "partial_exit_done": t.partial_exit_done,
                "partial_exit_price": t.partial_exit_price,
                "partial_pnl":       t.partial_pnl,
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
            "partial_exits":    partial_wins,
            "win_rate":         win_rate,
            "best_trade":       round(max((t.pnl for t in closed if t.pnl), default=0), 4),
            "worst_trade":      round(min((t.pnl for t in closed if t.pnl), default=0), 4),
            "avg_pnl":          round(realized_pnl / n_closed, 4) if n_closed else 0,
            "pnl_history":      self.pnl_history[-50:],
            "active_trade":     active,
            "trade_log":        [t.to_dict() for t in reversed(closed[-20:])],
            "exit_reasons":     exit_reasons,
            # Parámetros de la estrategia (útil para mostrar en dashboard)
            "strategy": {
                "name":             "Mean Reversion",
                "entry_zone":       f"{MIN_ENTRY_PRICE}–{MAX_ENTRY_PRICE}",
                "target_price":     TARGET_PRICE,
                "sl_price":         SL_PRICE_ABS,
                "partial_exit_pct": PARTIAL_EXIT_PCT,
                "max_entry_secs":   MAX_SECS_TO_ENTER,
                "min_secs_left":    MIN_SECS_TO_ENTER,
            },
        }
