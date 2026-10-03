# JEV — organizer capital ($800)

JEV is a **model-ranked Meteora DLMM** desk with a separate Binance stable volume sleeve. Capital does not auto-move between venues.

| Arm | Share | USD | Venue | Component |
|---|---|---|---|---|
| **Volume** | 60% | **480** | Binance spot | `jev_quote_gate` |
| **P&L** | 40% | **320** | Solana / Meteora DLMM (USDC) | `loops/jev_desk` |
| **Total** | 100% | **800** | two venues | — |

## Volume pair

1. **Primary:** `FDUSD-USDT`
2. **Fallback:** `USD1-USDT` if FDUSD book is missing, halted, or off-peg

Sample conf: `conf/jev_quote_gate.sample.yml`.

## P&L stop

| Rule | Value |
|---|---|
| Sleeve stop | **$90 USDC** absolute mark loss on the Meteora book |
| Basis | P&L arm NAV (not 10% of $800, not volume-desk PnL) |
| Per-pool | Still uses % stop/trail on filled inventory |
| Code | `portfolio_stop_usd` · `pnl_stop_loss_usd: 90` |

## Mode

`mode: pnl_race` in `loops/jev_desk/loop.md` (or `python agents/jev/set_mode.py pnl_race`).

Constants: `routines/_jev_math.py` → `MODE_PROFILES['pnl_race']` / `allocation_split()`.
