---
name: JEV Desk
description: >-
  Decision Maker at Meteora. One structured-decision model (TypeSafe System
  One) ranks the live Meteora DLMM universe across Top Performers / Trending /
  New / RWA tabs and selects a portfolio of 3-5 pools, sizing each from risk
  and market condition. One-sided BUY walls under price, re-sited to SELL-only
  after a fill. Selection + size are model-driven; execution, rug gate, and bin
  clamp stay pure math.
agent_key: null
skills: []
default_config:
  frequency_sec: 300
  execution_mode: loop
  # RUN MODE — test | prod | pnl_race. THIS KEY IS THE SWITCH: `_jev_math`
  # reads it at import, so the routine defaults and the dashboard both follow.
  #
  #   test     — 100 USDC / 2 slots / 12 floor
  #   prod     — 800 USDC / 5 slots / 50 floor   (full book, no volume split)
  #   pnl_race — split-book $800: P&L $320 USDC / 2 slots (this desk) + volume $480
  #              (Binance FDUSD-USDT, fallback USD1-USDT). Patient momentum gate,
  #              no timer rotates. Per-pool SL 3% + trail; sleeve stop $90 USDC.
  #
  # Switch with `python agents/jev/set_mode.py pnl_race` then restart the desk.
  mode: pnl_race
  total_amount_quote: 320
  quote_asset: USDC
  # Pinned so the dashboard's Start dialog seeds the right server. Left blank it
  # resolved to a nonexistent "local" and the loop could not reach the venue.
  server_name: local
  range_width_pct: 1.2
  min_outside_slots: 3
  dynamic_fee_pct: 0.0
  fee_floor_pct: 0.02
  can_reuse_position: false
  # The live gate is the memory key `jev.live` (a human sets it). This flag is
  # NOT settable from the dashboard's Start dialog, so leaving it true made a
  # UI-started loop print its intended orders and never place them.
  dry_run_writes: false
  # Model sizing (JEV Score -> % of book). Overridden live by jev_size.
  portfolio_pct_max: 0.50
  major_pct_min: 0.30
  major_pct_max: 0.45
  minor_pct_max: 0.20
  trust_noul_floor: 0.30
  select_conf_floor: 0.60
  worth_margin: 1.35
  # Portfolio / discovery. These track the run mode above — test: 2 slots /
  # 12.0 floor / 0.50 cap; prod: 5 slots / 100.0 floor / 0.20 cap.
  max_open_executors: 2
  min_position_usd: 40.0
  max_new_slots: 1
  enrich_top_k: 12
  scan_tabs: ["top", "trending", "rwa"]
  per_tab: 25
  min_tvl: 20000
  min_vol: 5000
  max_bin_step: 400
  risk_limits:
    max_position_size_quote: 320
    max_open_executors: 2
    max_drawdown_pct: 28
    pnl_stop_loss_usd: 90
    max_leverage: 1
    require_triple_barrier: false
    require_trailing_stop: false
    min_wallet_sol_reserve: 0.40
  volume_arm_usd: 480
  pnl_arm_usd: 320
  race_envelope_usd: 800
  volume_controller: jev_quote_gate
  volume_pair: FDUSD-USDT
  volume_pair_fallback: USD1-USDT
  require_momentum_pct: 3.0
  stop_loss_pct: 0.03
  trail_activation_pct: 0.025
  trail_delta_pct: 0.015
default_trading_context: 'Trade Meteora DLMM on solana-mainnet-beta (P&L arm $320 USDC). Quote USDC only. pnl_race: 2 slots, momentum>=3%, conf>=0.60, per-pool SL 3% + trail; sleeve stop $90 USDC NAV. Volume arm $480 is jev_quote_gate on Binance FDUSD-USDT (fallback USD1-USDT) — not this loop. Place walls when jev.live=yes, SOL reserve>=0.4, free USDC funds the floor. Live only when jev.live=yes.'
created_by: 0
created_at: '2026-09-19T00:00:00+00:00'
---

# JEV Desk

> Identity and risk philosophy live in `AGENT.md`. This file is the tick:
> connector, sides, sizing, exits. Follow it exactly.

Venue is **Meteora DLMM**. Network is `solana-mainnet-beta`.
`lp_provider` is `meteora/clmm`. `swap_provider` is `jupiter/router`.
Quote is **USDC**. Do not invent another house.

Goal in the 48h race (**pnl_race**): **grow the Meteora P&L sleeve ($320 USDC)** while the **volume arm ($480) runs on Binance FDUSD-USDT** (`jev_quote_gate`, auto-fallback **USD1-USDT** if FDUSD is unusable). This loop does **not** churn for tape. Wait for momentum, open few high-quality USDC walls, hold through fee + round-trip spread, and exit on per-pool SL/trail, **sleeve stop $90 USDC**, rug, or a completed SELL — not on a clock.

## Split-book capital ($800)

| Arm | USD | Venue | Component |
|---|---|---|---|
| **Volume** | **$480 (60%)** | Binance spot FDUSD-USDT → USD1-USDT | `jev_quote_gate` |
| **P&L** | **$320 (40%)** | Meteora DLMM (USDC quote) | this loop |
| **P&L stop** | **$90 USDC** | Meteora sleeve NAV | `portfolio_stop_usd` / `pnl_stop_loss_usd` |
| **Total** | **$800** | two venues | — |

Do not put $800 on Meteora. Do not size walls in USDT while the wallet holds USDC.

## Portfolio, model-selected

JEV ranks the **entire live Meteora DLMM universe** across the site's tabs —
Top Performers, Trending, New, and RWA — and holds a portfolio of **3–5
pools** at once. Each pool is one-sided; sizing and the bin width are decided
per pool from risk and market condition.

| Slot | Role | How it is chosen | How it is sized |
|---|---|---|---|
| **Portfolio** | Diversified fee income | `jev_scan` → `jev_enrich` (live rug cards) → `jev_rank` → `jev_select` top N (3–5) | `jev_size` per pool: % of book + width + cost/fee viability |

`jev_scan` tags every candidate with its tab (`top` / `trending` / `new` /
`rwa`). `jev_rank` scores depth + fee yield + tape, with a tab momentum
multiplier (trending/new boosted, rwa steady) and a cleanliness proxy.
`jev_select` takes the top N distinct-base pools under the slot budget and
per-position floor. No fixed per-pool dollar split; the book is allocated across
the chosen portfolio. Each pool is sized from depth / heat / rug scores: calm
deep book → larger, wider. Thin hot book → smaller, tighter. Confidence low → SIT.

## Book (set by the run mode)

The book, the slot budget and the per-position floor all come from the active run
mode (`_jev_math.MODE_PROFILES`), which the `mode:` key above selects:

| mode | book (this desk) | slots | min slice | per-pool cap | volume arm | notes |
|---|---|---|---|---|---|---|
| `test` | 100 USDC | 2 | 12.0 | 0.50 | — | organizer tests |
| `prod` | 800 USDC | 5 | 50.0 | 0.20 | — | full book, no split |
| `pnl_race` | **320 USDC** | **2** | **45.0** | **0.50** | **480 USDC** (Binance USD1 desk) | split-book competition |

The per-pool cap is derived so the portfolio can never oversubscribe the wallet:

```
PORTFOLIO_PCT_MAX = 1 / MAX_POSITIONS
test:  1/2 = 0.50   2 slots x 50% x $100 = $100   <- exactly the book, never over
prod:  1/5 = 0.20   5 slots x 20% x $800 = $800   <- exactly the book, never over
```

**Why test runs two slots, not five.** On a $100 book a slice must be big enough
for its fee income to cover the *fixed* ~$0.60 one-time open cost. At 5 slots the
model's mid answer sized ~$12.80, which scored ~0.3× on deep books — refused
every tick. At 2 slots the same answer sizes ~$32, which clears the floor on
mid-depth pools. Fewer, larger positions also cut total open-cost drag from
~$4.10 to ~$1.60 per full book. Prod's 5 slots on an $800 book do not have this
problem — the same answer sizes ~$160, so the floor is not in play.

`min_position_usd` must sit at or below the model's *smallest non-zero* Score
level, or that answer can never clear the floor. The levels derive from the role
cap (`[0, 0.32·cap, 0.64·cap, cap]`, `cap = PORTFOLIO_PCT_MAX`):

| mode | cap | book | Score levels | floor |
|---|---|---|---|---|
| `test` | 0.50 | $100 | `[0, 16.00, 32.00, 50.00]` | **$12** ✓ |
| `prod` | 0.20 | $800 | `[0, 51.20, 102.40, 160.00]` | **$50** ✓ |

Set the floor above level 1 and that answer is dead: the desk still looks alive
on mid/top answers, and at a higher floor it stops trading altogether while the
dashboard shows nothing wrong. Raise the floor only alongside the book — a $100
floor needs `0.32·cap·book ≥ 100`, i.e. at most ~2.5 slots on an $800 book.
`test_every_mode_keeps_the_smallest_score_level_reachable` asserts this per mode.

**A $100 book is a fee-dominated regime.** The one-time open cost is fixed
(`open_cost_usd` ≈ 0.0055 SOL ≈ $0.83 at SOL $150) while expected fee scales
with the slice. Deep calm pools (large TVL, low fee %) will therefore land in
the **NO** worth tier and SIT — the math is working, not broken. Expect the desk
to open mainly in the livelier/higher-fee books, on fewer slots than 5.

**SOL reserve.** Each open DLMM position needs its own position-account rent
(refundable on close) plus tx fees, so the wallet must hold more than the USDC
book. `min_wallet_sol_reserve` is **0.40 SOL** for up to 5 concurrent positions.
Fund the wallet with ~$100 USDC **plus** ≥0.5 SOL before going live, and confirm
the current rent per position on-chain rather than trusting the model's
`OPEN_RENT_SOL` (which only sizes the worth gate, it is not a funding plan).

## Constants

```
connector_name = "solana-mainnet-beta"
lp_provider    = "meteora/clmm"
swap_provider  = "jupiter/router"
quote_mint     = EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v
```

Use **mint pairs** when the base is not SOL (`<BaseMint>-USDC`).
Gateway cannot resolve most hot books by ticker.

`side`: `1` = BUY / quote-only · `2` = SELL / base-only · `3` = RANGE / both.

Re-site after a dump fill is **side=2**, never side=3.

## Range (price first, then the wall)

**bin_step is FIXED per pool on-chain — JEV cannot change it.** What JEV
decides is the **range width `W`** (the price span it covers), which sets how
many of those fixed bins the position spans. `jev_size` sweeps widths and
picks the one with the best per-day net fee after subtracting the one-time
open cost, then clamps so Meteora bins `ln(Pu/Pl) / ln(1 + bin_step/10000)`
stay **< 69**. Pull `bin_step` from pool info every open — never assume.

**WAIT / BUY only** (cash under price):

```
upper_price = P × (1 − 0.0005)
lower_price = P × (1 − W)
side = 1
quote_amount = pool_usd
base_amount  = 0
```

Verify `lower_price < upper_price < P`. If a bound crosses `P`, shrink.

**SHIFT / SELL only** (after a BUY fill):

```
lower_price = P × (1 + 0.0005)
upper_price = P × (1 + W)
side = 2
base_amount  = haircut inventory × 0.995
quote_amount = leftover USDC (may be 0)
```

Verify `P < lower_price < upper_price`.

Do **not** center 50/50 after a dump fill. That is the old reopen.

## Tick sequence

**1 — Scan (cross-tab).** One call pulls all tabs:

```python
manage_routines(action="run", name="jev_scan",
  strategy_id="jev.jev_desk",
  config={"quote_asset":"USDC","tabs":["top","trending","new","rwa"],"per_tab":25})
```

Each candidate is tagged with its tab (`top`/`trending`/`new`/`rwa`). The
routine always writes a Condor report (KPIs + table) and a machine snapshot
to `state/jev_scan.json` (+ memory key `jev_scan` when Condor memory is up).
If the live API is dark, scan falls back to a labeled DEMO universe so the
desk still has rows — never invent a pool yourself. Prefer live rows when
the report source is not demo.

**Reports & data chain (every Condor run).** Each routine below saves:
- a **Condor ReportBuilder** card (source `jev/<routine>`, tags `jev`/`meteora`/`dlmm`) so the Routines page shows KPIs + tables
- a **JSON snapshot** under `state/jev_<name>.json`
- best-effort **manage_memory** write `jev_<name>`

If you omit `candidates` on a later step, that routine loads the prior
snapshot automatically (`enrich` ← scan, `rank` ← enrich|scan, `select` ←
rank|enrich|scan). Still pass candidates explicitly when you have them.

**2 — Enrich (live rug cards).** Scout the top unique-base names before rank.
Writes `flags`, `rug_noul`, `scout_allowed` onto each stamped row. Fail closed
on fetch errors (blocked card). Snapshot → `state/jev_enrich.json`.

```python
manage_routines(action="run", name="jev_enrich",
  strategy_id="jev.jev_desk",
  config={"candidates":[<rows from scan>],"top_k":12,"concurrency":4,
          "output_path":"state/jev_enriched.json"})
# candidates may be omitted → loads state/jev_scan.json
```

Then load `state/jev_enrich.json` (or `jev_enriched.json`) → `candidates` for
rank. Unscouted rows may still carry proxy trust; scouted reds get
`rug_noul=0` and will hard-fail rank.

**3 — Rank.** Hard gate + composite (yield · depth · clean · tab). Live flags
from enrich feed the trust gate. Snapshot → `state/jev_rank.json`.

```python
manage_routines(action="run", name="jev_rank",
  strategy_id="jev.jev_desk",
  config={"candidates":[<enriched rows>],"top_n":8})
# or config={} to load snapshot:enrich
```

**4 — Select portfolio (model Noul fan-out).** Pick the top N (3–5) distinct-base
pools under the slot budget and per-position floor. Soft-cap new-tab slots.
System One is asked **one Noul question per candidate in a single call**
("should this pool take one of the slots?"); pools at or above the noul floor
are kept, ordered by that probability, and the model may only choose from names
math already cleared. **Pass `model_veto: false` on every call** — a model that
keeps no pool now falls back to the math picks instead of parking the whole
book. Rationale: math already cleared these candidates on rug safety and fee
viability before the model ever sees them, so a cautious model reply should
narrow the picks, not zero out deployment for the tick. An idle book earns no
fees and registers no volume — the competitive cost of an empty slot is real,
the cost of a math-cleared pick that underperforms is small and bounded by
the risk gate. Offline, math picks the top N regardless.
Snapshot → `state/jev_select.json`.

```python
manage_routines(action="run", name="jev_select",
  strategy_id="jev.jev_desk",
  config={"candidates":[<ranked rows>],"max_new_slots":2,"model_veto":False})
```

`max_positions`, `book_usd` and `min_position_usd` are omitted **on purpose**:
they default from the active run mode, so hand-passing them is how a prod run
ends up sizing to the test floor. Only pass them to deliberately deviate.

**5 — Confirm card (optional belt).** If a pick was not enriched, run the single
card now. Any red → drop it.

```python
manage_routines(action="run", name="jev_scout_card",
  strategy_id="jev.jev_desk",
  config={"mint":"<BaseMint>","pool_address":"<Pool>"})
```

**6 — Size + width + cost/fee (per pool).** For each surviving pool, call the
`jev_size` routine: System One is asked a **Score** question ("what slice of
the book?") whose levels are the role envelope, and the math then clamps that
answer, sweeps range widths, and runs the cost/worth tier.

```python
manage_routines(action="run", name="jev_size",
  strategy_id="jev.jev_desk",
  config={
    "role":"portfolio","tvl":<tvl>,"vol24":<vol>,"bin_step":<step>,
    "dynamic_fee_pct":<fee>,"outside_slots":<n>,"rug_noul":<0-1>,
    "vol_daily_pct":<est daily move %>,"sol_usd":0,
    "pool":<pool address>,"base":<base mint>,"base_symbol":<SYM>,
    "book_usd":<book>
  })
```

`jev_size` returns the model's `pct` of the book (math-clamped; `pct_source`
says `model` or `math`), the chosen `width_pct` (-> bin count), and the
open-cost **worth tier** for that amount:

| Tier | Ratio (fee ÷ open cost) | What to do |
|---|---|---|
| **GO** | ≥ `worth_margin` (1.2) | open the full proposed slice |
| **MARGINAL** | ≥ 0.5, below the margin | open the **trimmed** slice (`pct` already reduced, never below the size where the ratio clears 0.5) |
| **NO** | < 0.5 | no slice — the fee does not pay for the position |

The tier scales the size; it does not outrank the other criteria. Only a `NO`
tier, a `pct` of 0, or a `pool_usd` under `min_position_usd` stops an open —
a rug red or the dynamic fee floor still SIT the pool on their own. Journal pct,
width, open_cost, expected_fee, tier.

**Momentum required to open (pnl_race).** `require_momentum_pct` default **3**: if 24h momentum is below that (or 6h ≤ -5%), `jev_size` returns pct=0 — wait for the move. Pass `momentum_pct` / `momentum_short_pct` from scout every size call.

**Trend allowance (the deep-book case).** JEV's position is a one-sided **BID
wall under price**, so its income is not fees alone: when the wall fills it buys
base at a discount, and the re-sited ask sells that base back higher. `jev_size`
credits that round-trip spread when the asset is trending **up** — an appreciating
asset buys its dips back, a falling knife does not. With no trend read the credit
is 0 and the tier is fee-only, exactly as before.

Supply `pool` + `base` so the sweep picks up the momentum already scouted for that
pool, or pass `momentum_pct` (24h) / `momentum_short_pct` (6h) directly. ≥ 3%
earns credit, rising to full at 15%; a negative 24h read, or a 6h read at/below
-5%, voids it. The credited spread never overrides the rug card or trust floor —
a red pool stays red however hard it is trending. The reply line shows
`trend=`, `spread_credit=` and `income=` (fees + credited spread) next to the tier.

**Never a stable-vs-stable pair.** If BOTH sides are stablecoins there is no
directional leg at all — no trend to ride, no dip to buy back, only fee dust — so
it can never pay for its own open cost. The scan drops such pools at parse time
(reported as `stable-stable dropped=N`), `jev_rank` hard-fails any that arrive by
another route, and `jev_size` sizes them 0. Detection uses the mint, the symbol,
Jupiter's own `stable` tag, and a $1-ish peg with USD-styled naming.

**6 — Gate (per pool).** One verb per pool:

```python
manage_routines(action="run", name="jev_gate",
  strategy_id="jev.jev_desk",
  config={
    "role":"portfolio","wallet_usd":<book>,"pool_state":<NONE|IN_RANGE|OUT_OF_RANGE|FILLED_BUY>,
    "pool_side":<BUY|SELL|NONE>,"price":<P>,"lower_price":<lo>,"upper_price":<hi>,
    "extra_fees_usd":<est>,"slip_usd":<est>,"priority_usd":<est>,"rent_usd":<est>,
    "outside_slots":<N>,"dynamic_fee_pct":<fee>,"fee_floor_pct":0.02,
    "can_reuse_position":<bool>,"pool_pct":<fraction of book, 0..1>
  })
```

Expect WAIT / SHIFT / REBUILD / SIT per pool. `pool_state` is the executor's
**current** state on that pool — never a verb: pass `NONE` when no executor
exists yet (opening a fresh wall), `IN_RANGE` / `OUT_OF_RANGE` / `FILLED_BUY`
when one does. Passing `WAIT` is rejected with `BAD INPUT`; it used to take the
"already holding" branch and answer with a misleading SIT.

The model-sized `pool_pct` (fraction of book, 0..1)
drives the executor amount — `jev_gate` has **no `pool_usd` input**; it derives
`pool_usd = wallet_usd × pool_pct`. Passing `pool_usd` is silently ignored, so
`pool_pct` stays 0 and the gate returns SIT/$0. The math gate still runs last: a
red flag, a low confidence, or `rug_noul < trust_floor` → SIT, regardless of the
model.

**6 — Portfolio / slots.** `[CORE DATA]` first. On a fresh session with empty
executors, one `list_executors(executor_types=["lp_executor"], status="RUNNING")`
and **adopt** every RUNNING slot you own. Never open a
duplicate on a pool you already hold.

Need wallet USDC / SOL? `get_portfolio_overview(connector_names=["solana-mainnet-beta"])`.
Keep `min_wallet_sol_reserve` free.

**7 — Act. At most one write per pool per tick.**

Read the live gate first:

```python
live = manage_memory(action="read", name="jev.live")
LIVE = (isinstance(live, dict) and str(live.get("content", "")).strip() == "yes")
```

If `LIVE` is False, **print** the would-be create/stop and stop — do **not**
call `create_lp_executor` or `stop_executor`. (Default until a human writes
`jev.live = yes` to memory.)

| Gate verb | What you do |
|---|---|
| **SIT** | Nothing. Journal why (fee below floor, low confidence, flicker). |
| **WAIT** | If no position: open one-sided BUY-only `lp_executor` sized by the model. |
| **TAKE** | Fill already happened. Do not close. Next verb should be SHIFT or REBUILD. |
| **SHIFT** | Re-site to SELL-only one-sided. Only if `can_reuse_position` is proven true. |
| **REBUILD** | Default path. `stop` (keep_position=true) then open SELL-only one-sided. |
| **LEARN** | `entry_type="learning"`: why the minor pick lived or died. Play again. |

`dry_run_writes` **must be `false`** for a loop started from the dashboard: that
Start dialog has no field for it, so a `true` here makes the desk describe orders
it never places. The live gate is the memory key `jev.live` **alone** — exactly
`yes` means real orders, anything else means stay out.

**8 — Journal.** One `entry_type="action"` line per tick:

```
pool=<role> <addr> side=<BUY|SELL> P=<px> lo=<lo> hi=<hi>
size=<pct>% select_conf=<c> size_score=<s> rug_noul=<r>
decision=<verb> jev=<on|off>
```

## Open BUY-only wall (model-sized)

Fetch schema first, then:

```python
create_lp_executor(
  connector_name="solana-mainnet-beta",
  lp_provider="meteora/clmm",
  swap_provider="jupiter/router",
  trading_pair=<MintPair or SOL-USDC>,
  pool_address=<Pool>,
  lower_price=<lo>,
  upper_price=<hi>,
  side=1,                      # 1 = BUY / quote-only
  base_amount=0,
  quote_amount=<model_sized_usd>,
  keep_position=True,
  controller_id="jev",
  extra_params={"strategyType": 0}
)
```

`connector_name` is the **network** (`solana-mainnet-beta`), never the DEX — the
DEX belongs in `lp_provider`. `controller_id` is **required for isolation**: an
autonomous agent must tag its own executors with its agent id, or they are
attributed to `main`.

`keep_position=True` so a later stop does not Jupiter-dump the wall
into quote and erase inventory you still need for the SELL wall.

## SHIFT vs REBUILD

The name SHIFT is the re-site, not a cheap same-ticket slide. Both paths put
the dump inventory back on the book as a **SELL-only one-sided wall**. They
differ only in whether we reuse the position account.

- **REBUILD** (default until proven): `stop_executor(executor_id=...,
  keep_position=True)` then create side=2 SELL-only.
  A new ticket. This is the honest normal path right now: Hummingbot's
  `lp_executor` has no native resize, so we stop-and-reopen.
- **SHIFT** (only while `can_reuse_position=true`, proven live in the session
  note): same `position_address`, remove-then-readd one-sided to the SELL
  bounds. Reusing the account avoids hosting a fresh one, but it is still a
  remove + readd — not a magic cheap slide, and never a "Dynamic Positions"
  promise. Dry-run the readd first. Until `can_reuse_position` is proven,
  do not write SHIFT in the journal; write REBUILD.

Never `keep_position=false` on a wall just to "reset." That is tear-down.

## Split-book capital (pnl_race)

| Arm | Venue | Capital | Job |
|---|---|---|---|
| **P&L (this desk)** | Meteora DLMM | **$320** (40%) | Patient one-sided walls; fee + round-trip spread |
| **Volume** | Binance USD1 stable desk (`jev_quote_gate`) | **$480** (60%) | Race volume only — separate account/controller |

Do not fund the volume desk from the Meteora wallet or vice versa. `book_usd` /
`total_amount_quote` on this loop are the **P&L sleeve only**.

## Portfolio refresh (patient — score-chase OFF in pnl_race)

When `portfolio_rotate_hours` is **0** (pnl_race default): **do not** rotate a
held slot just because another candidate scored higher. Hold through fee income
and the SELL wall. Volume is the other arm's job.

If a future mode sets `portfolio_rotate_hours` > 0, only then: every N hours,
re-check rank and REBUILD into a candidate ≥10 composite points stronger, never
abandoning a live TAKE fill, at most one rotate per tick, journal
`decision=REBUILD_ROTATE`.

Still always exit for **risk** (below), rug, or dead route — those are not churn.

## Stop-loss and trailing stop (filled inventory)

LP executors have no native triple barrier. The tick **must** mark-to-market:

1. On open / BUY fill, journal and remember `entry_price = P` and `peak_price = P`
   (memory key e.g. `jev.peak.<pool>` or the session journal).
2. Every tick while holding base (FILLED_BUY or SELL wall), pass to `jev_gate`:
   `entry_price`, `peak_price`, `price=P`, barriers on.
3. Update `peak_price = max(peak_price, P)` each tick.
4. If gate RISK action is **`KILL_SL`** or **`KILL_TRAIL`**:
   `stop_executor(..., keep_position=False)` (or equivalent full close) **this
   tick** — do not leave a bleeding wall. Journal `decision=KILL_SL|KILL_TRAIL`.

Defaults in pnl_race (from mode profile):

| Barrier | Value | Meaning |
|---|---|---|
| Stop loss | **3%** | `(P - entry)/entry <= -3%` → close |
| Trail activation | **+2.5%** | arm once peak pnl ≥ 2.5% |
| Trail delta | **1.5%** | from peak price, give-back ≥ 1.5% → close |

Unfilled BUY walls are **not** SL'd for sitting (price must come to the wall).
If momentum flips hard down before fill, prefer SIT on new size next tick via
the momentum gate rather than chasing.

## Position leash (no timer kill in pnl_race)

Enter only if `jev_select` cleared `select_conf_floor` AND the rug card is clean
AND `jev_size` cleared momentum + worth tier.

Exit on the **first** of:

| Trigger | Action |
|---|---|
| RISK `KILL_SL` / `KILL_TRAIL` | Close inventory immediately |
| Rug noul drops below `trust_noul_floor` | Close / SIT |
| A flag appears after entry | Close |
| Runner fills the SELL wall | Bank. LEARN what worked. |
| Dead tape / no Jupiter route | Close |
| Age timer | **Off** in pnl_race (`minor_max_sec=0`) |

Do not mix pool ids. Do not force-close a healthy fee-accruing wall for volume.

## Do not

- Do not hardcode SOL–USDC if the scan printed a better clean book.
- Do not open a minor when any flag is red or the model confidence is low.
- Do not fund the minor from the major — each pool is sized independently.
- Do not open a NEW position when the `jev_size` worth tier is **NO** (fee below
  0.5× the one-time open cost) — SIT or reuse an existing position. A
  **MARGINAL** tier is not a veto: open the trimmed slice it returns.
- Do not try to change a pool's `bin_step`; it is fixed on-chain. JEV chooses
  the range width (how many of those bins to span), not the bin size.
- Do not call a close/reopen **SHIFT** — it is REBUILD until reuse is proven.
- Do not claim "Dynamic Positions" / same-ticket resize until reuse is proven.
- Do not claim a perp hedge.
- Do not launch DBC / DAMM pools with this book.
- Do not retry a FAILED executor blindly — journal and SIT.
- Do not invent a bin price the gate did not print.


## Order placement checklist (do not permanent-SIT)

1. Human sets memory `jev.live=yes` (agent never self-writes it).
2. `dry_run_writes: false` for live; SOL free ≥ `min_wallet_sol_reserve` (0.40).
3. Quote mint **USDC** matches wallet USDC (not USDT walls on a USDC book).
4. `jev_select` / `jev_size` use **effective_min_position_usd** so a tight free
   book still opens one slot ≥ $20 rather than SIT forever under a stale floor.
5. Momentum gate is **3%** in pnl_race (patient, not paralyzed).
6. On `KILL_PORTFOLIO` ($90 sleeve loss) or `KILL_SL` / `KILL_TRAIL`: `stop_executor` same tick.
7. Volume is **out of band** — never open Meteora walls to manufacture turnover.
