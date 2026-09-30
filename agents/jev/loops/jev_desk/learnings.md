# Learnings

## Market Observations
- [2026-09-20 04:58] 6p6xgH-EPjF/3C5YE97H (bin_step 10, tvl ~$7M) has landed NO tier 2 ticks running (fee $0.03-0.14 vs $0.60-0.82 open cost) — too deep/calm relative to a $100 book to ever clear the cost bar; needs a bigger book or a livelier book to be worth opening.
- [2026-09-20 06:22] CARDS-USDC (trending, comp 76.9, tvl ~$191k, dynamic_fee ~0.20%) topped rank but 24h momentum -10.6% / 6h -5.1% voided the trend/spread credit and its fee/open-cost ratio was only 0.27 (NO) — a top composite score does not imply it clears the $100-book cost bar when the tape is falling.
- [2026-09-20 06:33] jev_enrich rug-scout feed recovered from the prior 2-tick full outage: this tick scouted 12/22 with a real split (9 allowed, 3 blocked on specific flags like creator_pile/ghost_tape) instead of failing closed feed-wide.

## Execution Notes
- [2026-09-20 04:28] Live scout_card returned identical flags (creator_pile, lp_unlocked, ghost_tape) and tvl=0/vol24=0 for both a low-cap token AND wrapped SOL's SOL-USDC pool, despite scan showing millions in real volume — looks like the rug-check data source is degraded/down and failing closed rather than reading per-token risk.
- [2026-09-20 04:34] jev_enrich blocked 12/12 scouted candidates (100%) at rug_noul=0.00 for a 2nd straight tick, incl. SOL-USDC — feed-wide failure, not per-pool signal; portfolio pipeline is unusable until scout data source is fixed.
- [2026-09-20 04:43] jev_enrich scouts only one tab-tagged copy of a pool listed under 2 tabs; the unscouted duplicate keeps rug_noul=1.0 default and can reach select even when the scouted copy of the SAME pool is red (e.g. creator_pile) -- must cross-check all tab copies of a pool address, not just select's row.
- [2026-09-20 04:51] jev_scan/enrich rows carry no live dynamic_fee_pct field, forcing a placeholder estimate at jev_size time — worth-tier verdict is only as good as that guess until scan surfaces the real per-pool fee.
- [2026-09-20 04:58] jev_scan/enrich/rank rows now carry live dynamic_fee_pct and fee_rate_pct fields (e.g. 0.101% on 6p6xgH) — the earlier placeholder-estimate gap at jev_size appears resolved, at least for this tick's universe.
- [2026-09-20 06:22] jev_select's model noul (0.67, cleared 0.55 floor) did not carry over to jev_size's model confidence (0.03, well under its 0.25 floor) for the same pool (CARDS-USDC) — select-stage confidence and size-stage confidence are independent gates, both must be checked per pool.
- [2026-09-20 06:33] jev_gate's pool_state input describes the CURRENT executor state, not the desired verb: passing "WAIT" (meant as "open fresh wall") makes has_position=True internally and returns VERB=SIT; use pool_state="NONE" when there is no existing executor on the pool.
- [2026-09-20 06:33] jev_gate sizes the position from the field `pool_pct` (fraction of book), not `pool_usd` — passing pool_usd is silently ignored, model_pct defaults to 0 and the gate returns SIT/pool_usd=$0.
- [2026-09-20 06:46] jev_select with max_positions=2 kept only 1 slot (model=SELECT note "kept 1/2 at noul>=0.55") even though a 2nd clean distinct-base candidate (STONK-USDC, rug 1.00, comp 74.2) was in the ranked top 8 — slot 2 sits idle by model choice, not a math/rug constraint.

## Retired Insights
