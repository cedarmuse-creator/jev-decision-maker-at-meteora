"""jev_quote_gate — a gated two-sided quoting controller for a zero-fee
stablecoin pair, phrased as a small set of verbs rather than inline branches:
each tick resolves to exactly one of SIT / HOLD / QUOTE / REACH / UNWIND, and
only that verb's action runs.

  SIT     — a hard stop has latched (fee or drawdown) or the pair is off its
            peg band: cancel everything, do nothing new.
  HOLD    — a supervisor set `hold_quotes`: cancel everything, keep inventory.
  REACH   — turnover trails the pace line by more than `reach_trigger_usd`:
            one priced crossing limit through the far touch (never a market
            order), capped by a slice of the resting depth on that side.
  QUOTE   — default: keep a two-sided LIMIT_MAKER book at the current touch.
  UNWIND  — a supervisor set `unwind_now`: cancel the book, cross back toward
            `unwind_base_share` of account value, then stop.

Nothing here reads or writes any external decision model; the gate is pure
arithmetic over live balances, the order book, and executor state.

Single-lane (1-sided), time-alternating: during QUOTE, only one side is ever
live — it flips BUY/SELL on a fixed clock (`side_switch_s`) rather than by
inventory skew, a deliberately different selection rule from the other
single-lane desk in this set so the two don't move in lockstep. The active
side's resting maker is a fixed `reach_order_usd` clip; unfilled past
`maker_timeout_s` it is cancelled and replaced one tick later with a
crossing limit of the same size.

Capital note: ~60% of the $800 entry (~$480) on this Binance stable desk;
~$320 USDC stays on Meteora for JEV's model-selected DLMM walls. Primary
pair is FDUSD-USDT; if that book is dead or off-peg, fall back to USD1-USDT.
P&L-arm dollar stop ($90 USDC) is enforced on the Meteora loop, not here.
"""
from decimal import ROUND_DOWN, ROUND_HALF_UP, Decimal
from enum import Enum
from typing import Dict, List, Optional

from pydantic import Field

from hummingbot.core.data_type.common import MarketDict, PriceType, TradeType
from hummingbot.strategy_v2.controllers.controller_base import ControllerBase, ControllerConfigBase
from hummingbot.strategy_v2.executors.order_executor.data_types import ExecutionStrategy, OrderExecutorConfig
from hummingbot.strategy_v2.models.executor_actions import CreateExecutorAction, ExecutorAction, StopExecutorAction

DUST_USD = Decimal("6")


class GateVerb(str, Enum):
    SIT = "SIT"
    HOLD = "HOLD"
    QUOTE = "QUOTE"
    REACH = "REACH"
    UNWIND = "UNWIND"


class JevQuoteGateConfig(ControllerConfigBase):
    controller_type: str = "generic"
    controller_name: str = "jev_quote_gate"

    connector_name: str = Field("binance")
    trading_pair: str = Field("FDUSD-USDT")
    fallback_pair: str = Field("USD1-USDT")
    use_pair_fallback: bool = Field(True, json_schema_extra={"is_updatable": True})

    pace_target_usd: Decimal = Field(Decimal("1100000"), json_schema_extra={"is_updatable": True})
    pace_window_s: int = Field(172800)
    pace_deadline_ts: int = Field(0, json_schema_extra={"is_updatable": True})

    reach_trigger_usd: Decimal = Field(Decimal("1600"), json_schema_extra={"is_updatable": True})
    reach_order_usd: Decimal = Field(Decimal("220"), json_schema_extra={"is_updatable": True})
    reach_depth_share: Decimal = Field(Decimal("0.28"), json_schema_extra={"is_updatable": True})
    reach_ticks: int = Field(2, json_schema_extra={"is_updatable": True})

    step_in_wide_spread: bool = Field(True, json_schema_extra={"is_updatable": True})
    tilt_gate_usd: Decimal = Field(Decimal("240"), json_schema_extra={"is_updatable": True})

    maker_timeout_s: float = Field(
        9.0, json_schema_extra={"is_updatable": True},
        description="The active side's resting maker is cancelled and replaced with a crossing limit of the same "
                    "clip once it has sat unfilled this long.")
    side_switch_s: float = Field(
        20.0, json_schema_extra={"is_updatable": True},
        description="How often the single active side flips BUY<->SELL, independent of inventory skew.")

    fee_gate_bp: Decimal = Field(Decimal("0.5"), json_schema_extra={"is_updatable": True})
    drawdown_gate_usd: Decimal = Field(Decimal("48"), json_schema_extra={"is_updatable": True})
    peg_lo: Decimal = Field(Decimal("0.9975"), json_schema_extra={"is_updatable": True})
    peg_hi: Decimal = Field(Decimal("1.0025"), json_schema_extra={"is_updatable": True})

    hold_quotes: bool = Field(False, json_schema_extra={"is_updatable": True})
    unwind_now: bool = Field(False, json_schema_extra={"is_updatable": True})
    unwind_base_share: Decimal = Field(Decimal("0.5"), json_schema_extra={"is_updatable": True})

    tick_interval_s: float = Field(1.0)

    def update_markets(self, markets: MarketDict) -> MarketDict:
        pairs = {self.trading_pair}
        fb = getattr(self, "fallback_pair", None)
        if fb:
            pairs.add(fb)
        markets[self.connector_name] = markets.get(self.connector_name, set()) | pairs
        return markets


class JevQuoteGateController(ControllerBase):

    def __init__(self, config: JevQuoteGateConfig, *args, **kwargs):
        self.config = config
        kwargs.setdefault("update_interval", float(config.tick_interval_s))
        super().__init__(config, *args, **kwargs)
        self._opened_ts: Optional[float] = None
        self._pace_anchor = None
        self._fill_wm: Dict[str, Decimal] = {}
        self._fee_wm: Dict[str, Decimal] = {}
        self._opening_value: Optional[Decimal] = None
        self._latch: Optional[str] = None
        self._last_verb: Optional[GateVerb] = None
        self._peg_logged = False
        self._cross_due: bool = False

    def update_config(self, new_config):
        keys = [k for k, f in type(self.config).model_fields.items() if (f.json_schema_extra or {}).get("is_updatable")]
        before = {k: getattr(self.config, k) for k in keys}
        super().update_config(new_config)
        moved = [f"{k}:{before[k]}->{getattr(self.config, k)}" for k in keys if before[k] != getattr(self.config, k)]
        if moved:
            self.logger().info(f"[{self.config.id}] " + " ".join(moved))

    def _coins(self):
        return self.config.trading_pair.split("-")

    def _book_alive(self) -> bool:
        try:
            bid = self._px(PriceType.BestBid)
            ask = self._px(PriceType.BestAsk)
            return bid > 0 and ask > 0 and ask >= bid
        except Exception:
            return False

    def _ensure_pair(self) -> None:
        """Prefer FDUSD-USDT; fall over to USD1-USDT when primary cannot trade."""
        c = self.config
        if not getattr(c, "use_pair_fallback", True):
            return
        fb = (getattr(c, "fallback_pair", None) or "").strip()
        if not fb or fb == c.trading_pair:
            return
        if self._book_alive():
            mid = self._px(PriceType.MidPrice)
            if c.peg_lo <= mid <= c.peg_hi:
                return
        prev = c.trading_pair
        c.trading_pair = fb
        self.logger().warning(f"[{c.id}] pair failover {prev} -> {fb}")

    def _balances(self, free_only: bool):
        base, quote = self._coins()
        conn = self.market_data_provider.get_connector(self.config.connector_name)
        reader = (getattr(conn, "get_available_balance", None) or conn.get_balance) if free_only else conn.get_balance
        return Decimal(str(reader(base))), Decimal(str(reader(quote)))

    def _tick_size(self) -> Decimal:
        try:
            r = self.market_data_provider.get_trading_rules(self.config.connector_name, self.config.trading_pair)
            return Decimal(str(r.min_price_increment))
        except Exception:
            return Decimal("0")

    def _px(self, kind) -> Decimal:
        return Decimal(str(self.market_data_provider.get_price_by_type(
            self.config.connector_name, self.config.trading_pair, kind)))

    def _depth(self):
        try:
            ob = self.market_data_provider.get_order_book(self.config.connector_name, self.config.trading_pair)
            bid, ask = next(ob.bid_entries(), None), next(ob.ask_entries(), None)
            return None if bid is None or ask is None else (Decimal(str(bid.amount)), Decimal(str(ask.amount)))
        except Exception:
            return None

    def _turnover_fees(self):
        for ex in self.executors_info:
            v = Decimal(str(ex.filled_amount_quote or 0))
            if v > self._fill_wm.get(ex.id, Decimal("0")):
                self._fill_wm[ex.id] = v
            f = Decimal(str(getattr(ex, "cum_fees_quote", 0) or 0))
            if f > self._fee_wm.get(ex.id, Decimal("0")):
                self._fee_wm[ex.id] = f
        return sum(self._fill_wm.values(), Decimal("0")), sum(self._fee_wm.values(), Decimal("0"))

    def _pace_line(self, now, deadline, turnover, idle) -> Decimal:
        target = self.config.pace_target_usd

        def at(anchor, t):
            t0, v0, tgt, dl = anchor
            frac = min(1.0, max(0.0, (t - t0) / max(1.0, dl - t0)))
            return v0 + (tgt - v0) * Decimal(str(frac))

        if self._pace_anchor is None:
            self._pace_anchor = (self._opened_ts, Decimal("0"), target, deadline)
        elif (self._pace_anchor[2], self._pace_anchor[3]) != (target, deadline) or idle:
            now_v = at(self._pace_anchor, now)
            self._pace_anchor = (now, min(now_v, turnover) if idle else now_v, target, deadline)
        return at(self._pace_anchor, now)

    def _resolve_verb(self, pd) -> GateVerb:
        c = self.config
        if self._latch or not pd["on_peg"]:
            return GateVerb.SIT
        if c.hold_quotes:
            return GateVerb.HOLD
        if c.unwind_now:
            return GateVerb.UNWIND
        if pd["pace"] - pd["turnover"] > c.reach_trigger_usd:
            return GateVerb.REACH
        return GateVerb.QUOTE

    async def update_processed_data(self):
        self._ensure_pair()
        now = self.market_data_provider.time()
        if self._opened_ts is None:
            self._opened_ts = now
        deadline = self.config.pace_deadline_ts or (self._opened_ts + self.config.pace_window_s)
        mid = self._px(PriceType.MidPrice)
        on_peg = self.config.peg_lo <= mid <= self.config.peg_hi
        base_bal, quote_bal = self._balances(free_only=False)
        turnover, fees = self._turnover_fees()
        idle = self.config.hold_quotes or self.config.unwind_now or not on_peg
        pace = self._pace_line(now, deadline, turnover, idle)
        self.processed_data = {
            "now": now, "mid": mid, "base": base_bal, "quote": quote_bal, "on_peg": on_peg,
            "turnover": turnover, "fees": fees, "pace": pace,
        }

    @staticmethod
    def _units(usd: Decimal, px: Decimal) -> Decimal:
        return Decimal("0") if px <= 0 else (usd / px).quantize(Decimal("1"), rounding=ROUND_DOWN)

    def _order(self, side: TradeType, amount: Decimal, price: Optional[Decimal], strat: ExecutionStrategy):
        cfg = OrderExecutorConfig(timestamp=self.market_data_provider.time(), connector_name=self.config.connector_name,
                                  trading_pair=self.config.trading_pair, side=side, amount=amount, price=price,
                                  execution_strategy=strat, controller_id=self.config.id)
        return CreateExecutorAction(executor_config=cfg, controller_id=self.config.id)

    def determine_executor_actions(self) -> List[ExecutorAction]:
        pd = self.processed_data
        if not pd:
            return []
        c = self.config
        active = [ex for ex in self.executors_info if ex.is_active]
        value = pd["base"] * pd["mid"] + pd["quote"]
        if self._opening_value is None:
            self._opening_value = value

        if self._latch is None:
            if self._opening_value - value > c.drawdown_gate_usd:
                self._latch = "drawdown"
            elif pd["turnover"] > Decimal("1000") and (pd["fees"] / pd["turnover"] * 10000) > c.fee_gate_bp:
                self._latch = "fee"
            if self._latch:
                self.logger().error(f"[{c.id}] latched: {self._latch}")

        verb = self._resolve_verb(pd)
        if verb != self._last_verb:
            self.logger().info(f"[{c.id}] verb -> {verb.value}")
            self._last_verb = verb

        if verb in (GateVerb.SIT, GateVerb.HOLD):
            if verb is GateVerb.SIT and not pd["on_peg"] and not self._peg_logged:
                self.logger().warning(f"[{c.id}] off peg {pd['mid']}")
                self._peg_logged = True
            if pd["on_peg"]:
                self._peg_logged = False
            return [StopExecutorAction(controller_id=c.id, executor_id=ex.id) for ex in active]
        self._peg_logged = False

        makers: Dict[TradeType, list] = {TradeType.BUY: [], TradeType.SELL: []}
        takers: list = []
        for ex in active:
            (makers[ex.config.side] if getattr(ex.config, "execution_strategy", None) == ExecutionStrategy.LIMIT_MAKER
             else takers).append(ex)

        base_val, quote_val = pd["base"] * pd["mid"], pd["quote"]
        tilt = base_val - (base_val + quote_val) / 2
        bid, ask = self._px(PriceType.BestBid), self._px(PriceType.BestAsk)
        tick = self._tick_size()

        if verb is GateVerb.UNWIND:
            if makers[TradeType.BUY] or makers[TradeType.SELL]:
                return [StopExecutorAction(controller_id=c.id, executor_id=ex.id)
                       for ex in makers[TradeType.BUY] + makers[TradeType.SELL]]
            if takers:
                return []
            total = base_val + quote_val
            gap = base_val - total * c.unwind_base_share
            if abs(gap) < DUST_USD:
                return []
            if gap > 0:
                px = bid - tick * c.reach_ticks
                return [self._order(TradeType.SELL, self._units(gap, px), px, ExecutionStrategy.LIMIT)]
            px = ask + tick * c.reach_ticks
            return [self._order(TradeType.BUY, self._units(-gap, px), px, ExecutionStrategy.LIMIT)]

        if verb is GateVerb.REACH and not takers:
            side = TradeType.SELL if tilt >= 0 else TradeType.BUY
            if makers[side]:
                return [StopExecutorAction(controller_id=c.id, executor_id=ex.id) for ex in makers[side]]
            px = (bid - tick * c.reach_ticks) if side == TradeType.SELL else (ask + tick * c.reach_ticks)
            size_usd = c.reach_order_usd
            depth = self._depth()
            if depth is not None:
                far = depth[0] if side == TradeType.SELL else depth[1]
                size_usd = min(size_usd, far * pd["mid"] * c.reach_depth_share)
            avail = base_val if side == TradeType.SELL else quote_val
            size_usd = min(size_usd, avail)
            if size_usd >= DUST_USD:
                return [self._order(side, self._units(size_usd, px), px, ExecutionStrategy.LIMIT)]

        # Single-lane, time-alternating: only one side is ever quoted during QUOTE,
        # chosen by a fixed clock rather than skew (see module docstring).
        active_side = TradeType.BUY if int(pd["now"] // c.side_switch_s) % 2 == 0 else TradeType.SELL
        idle_side = TradeType.SELL if active_side == TradeType.BUY else TradeType.BUY
        actions: List[ExecutorAction] = [
            StopExecutorAction(controller_id=c.id, executor_id=ex.id) for ex in makers[idle_side]
        ]

        timed_out = [ex for ex in makers[active_side] if pd["now"] - float(ex.config.timestamp) >= c.maker_timeout_s]
        if timed_out:
            self._cross_due = True
            return actions + [StopExecutorAction(controller_id=c.id, executor_id=ex.id) for ex in timed_out]

        touch = {TradeType.BUY: bid, TradeType.SELL: ask}
        if c.step_in_wide_spread and tick > 0:
            width = ((ask - bid) / tick).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
            if width >= 3:
                touch = {TradeType.BUY: bid + tick, TradeType.SELL: ask - tick}
            elif width == 2:
                touch = ({TradeType.BUY: bid, TradeType.SELL: ask - tick} if base_val >= quote_val
                        else {TradeType.BUY: bid + tick, TradeType.SELL: ask})

        allow = tilt <= c.tilt_gate_usd if active_side == TradeType.BUY else tilt >= -c.tilt_gate_usd
        if not allow:
            return actions

        if not makers[active_side] and self._cross_due and not takers:
            self._cross_due = False
            px = (bid - tick * c.reach_ticks) if active_side == TradeType.SELL else (ask + tick * c.reach_ticks)
            avail = base_val if active_side == TradeType.SELL else quote_val
            size_usd = min(c.reach_order_usd, avail)
            if size_usd >= DUST_USD:
                actions.append(self._order(active_side, self._units(size_usd, px), px, ExecutionStrategy.LIMIT))
            return actions

        stale = [ex for ex in makers[active_side] if Decimal(str(ex.config.price)) != touch[active_side]]
        actions += [StopExecutorAction(controller_id=c.id, executor_id=ex.id) for ex in stale]
        if makers[active_side] and not stale:
            return actions
        free_base, free_quote = self._balances(free_only=True)
        spend = free_quote if active_side == TradeType.BUY else free_base * pd["mid"]
        size_usd = min(spend, c.reach_order_usd)
        if size_usd >= DUST_USD:
            actions.append(self._order(active_side, self._units(size_usd, touch[active_side]), touch[active_side],
                                       ExecutionStrategy.LIMIT_MAKER))
        return actions

    def to_format_status(self) -> List[str]:
        pd = self.processed_data or {}
        verb = self._last_verb.value if self._last_verb else "?"
        return [f"=== {self.config.id} verb={verb} ===",
                f"  turnover={pd.get('turnover', 0):.0f} pace={pd.get('pace', 0):.0f}"]
