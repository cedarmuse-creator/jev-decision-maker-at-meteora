# JEV — Decision Maker at Meteora (agent)

Condor agent. JEV (TypeSafe System One) ranks the live Meteora DLMM universe
across Top / Trending / New / RWA, enriches top names with live eight-flag rug
cards, and holds a portfolio of 3–5 pools sized from risk/market data.
One-sided walls, gated on the dynamic fee, dry-run by default.

## Layout

| Path | What |
|---|---|
| `AGENT.md` | Who JEV is, the decision verbs, safety invariants |
| `strategies/jev_desk/strategy.md` | Hands — tick sequence, sizing, width, executors |
| `routines/_jev_math.py` | Pure math: rug card, rank hard-gate, select, size, width, worth |
| `routines/_jev_sdk.py` | TypeSafe System One client (Noul fan-out + Score), offline mock fallback |
| `routines/jev_scan.py` | Meteora DLMM cross-tab universe |
| `routines/jev_enrich.py` | Live rug cards on top unique bases → `flags` + `rug_noul` |
| `routines/jev_rank.py` | Hard gate + composite (yield · depth · clean · tab) |
| `routines/jev_select.py` | Portfolio Noul fan-out: top 3–5 distinct bases |
| `routines/jev_size.py` | Score: % of book + range width + open-cost viability |
| `routines/jev_scout_card.py` | Single-mint eight-flag card (also used by enrich) |
| `routines/jev_gate.py` | One verb per pool: WAIT / SHIFT / REBUILD / SIT |

## Pipeline

```
scan → enrich (live cards) → rank → select → size → gate
```

## Model wiring

`_jev_sdk.py` talks to TypeSafe System One (`jev-latest`) with the typed
primitives — `Noul` for yes/no, `Score` for ordered levels, `Choice` for a
single option:

| Decision | Primitive | Why |
|---|---|---|
| Which pools earn a slot | one `Noul` per candidate, in **one** request | `Choice` is a single-select and cannot return a 3–5 pool portfolio; a Noul per candidate is a multi-select and the returned probabilities rank the picks |
| % of the book per pool | `Score` whose levels are the role envelope from `_jev_math` | the answer is an expected position on that ordered scale, interpolated to a pct |

The model proposes; `_jev_math` disposes. A pool that fails the rug trust
floor is never sent, the model can only choose among math-cleared names, and
`Config.model_veto` decides whether an empty model verdict parks the book.
Without `TYPESAFE_API_KEY` (or without the SDK) the desk runs pure math and
reports `jev=JEV off`.

## Tests

```bash
python agents/jev/tests/test_jev_math.py   # pure decision logic
python agents/jev/tests/test_jev.py        # SDK/decision wrapper (offline + fake client)
python agents/jev/tests/test_jev_transport.py  # wire contract vs the real SDK (mock transport)
```

`test_jev.py` and `test_jev_transport.py` need `typesafe-sdk` for the
wire-level assertions; without it they still run the offline cases (the
transport suite skips). See `requirements.txt`.

## Safety

Dry-run until session note `jev.live=yes`. Eight-flag rug card blocks dirty
books before size. The cost tier scales the slice instead of blocking it:
fee ≥ 1.2× open cost opens full size, ≥ 0.5× opens trimmed, below that no slice.
