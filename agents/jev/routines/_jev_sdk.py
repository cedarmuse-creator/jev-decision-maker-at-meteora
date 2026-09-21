"""JEV System One client — Decision Maker at Meteora.

Wraps the TypeSafe System One model (`jev-latest`) as the desk's decision
layer. Two calls the routines rely on:

  select_portfolio(client, candidates)  -> Noul fan-out: which pools earn a slot
  size_position(client, ...)            -> Score: position % of the book

Primitive contract (https://docs.typesafe.ai/primitives):

  * ``Noul``   asks a yes/no question and returns P(yes) in ``noul``.
  * ``Score``  asks for a position on an ORDERED list of levels and returns an
    expected ``score`` between ``0`` and ``len(criteria) - 1`` (plus
    ``confidence`` and ``probabilities``).
  * ``Choice`` returns ONE option out of a criteria map — it is a
    single-select, so it cannot pick a 3–5 pool portfolio on its own.

Membership in the book is therefore asked as ONE ``Noul`` per candidate in a
single request (TypeSafe "speculative fan-out": one call, N questions), and
size is a ``Score`` whose levels ARE the role envelope from ``_jev_math``.
The model proposes; the math in ``_jev_math`` / ``jev_gate`` still disposes.

When ``TYPESAFE_API_KEY`` is absent (or the SDK is not installed) the desk
falls back to the pure-math path and reports ``jev=JEV off`` — the model is an
advisor, never a hard requirement.
"""

from __future__ import annotations

import importlib.util
import logging
import os
from pathlib import Path as _P
from typing import Any

logger = logging.getLogger(__name__)

MODEL_TAG = "jev-latest"
ENV_KEY = "TYPESAFE_API_KEY"
ENV_MODEL = "TYPESAFE_MODEL"

# Noul >= this probability → the model wants the pool in the book.
SELECT_NOUL_FLOOR = 0.55
# Score confidence below this → the model is not sure enough; treat as abstain.
# The model used live self-reports confidence in a narrow band straddling this
# value (0.29-0.37 observed on a clean, high-score pool), so a 0.30 floor made it
# abstain on roughly two ticks in three. A 0.01 difference in a self-reported
# confidence deciding whether a position goes out is noise, not judgement, so the
# floor sits below the band instead of inside it.
SIZE_CONF_FLOOR = 0.25
# Cap on candidates per select call (one Noul each; keeps the fan-out cheap).
MAX_SELECT_QUESTIONS = 12

JEV_ON = "JEV on"
JEV_OFF = "JEV off"

# Answer collection per question kind on SystemOneResponse
# (.nouls / .choices / .scores are keyed by the question name; .answers is the
# untyped union of all three).
_ANSWER_COLLECTION = {"noul": "nouls", "choice": "choices", "score": "scores"}


def _math():
    """Load `_jev_math` by path (routines are loaded from a directory, not a package)."""
    path = _P(__file__).with_name("_jev_math.py")
    spec = importlib.util.spec_from_file_location("jev__jev_math", path)
    if spec is None or spec.loader is None:
        raise ImportError("_jev_math")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_m = _math()


# ─────────────────────────── client / primitives ───────────────────────────


def _client() -> Any | None:
    """A live ``TypeSafeClient``, or ``None`` → the desk runs pure math."""
    key = (os.environ.get(ENV_KEY) or "").strip()
    if not key:
        return None
    try:
        from typesafe_sdk import TypeSafeClient
    except Exception as exc:  # noqa: BLE001 — SDK is optional (pip install typesafe-sdk)
        logger.warning("typesafe_sdk unavailable, JEV off: %s", exc)
        return None
    model = (os.environ.get(ENV_MODEL) or "").strip() or MODEL_TAG
    try:
        return TypeSafeClient(api_key=key, model=model)
    except Exception as exc:  # noqa: BLE001 — never crash the tick
        logger.warning("typesafe client init failed, JEV off: %s", exc)
        return None


def _primitives():
    """The typed question primitives (`Choice`, `Noul`, `Score`)."""
    from typesafe_sdk import Choice, Noul, Score

    return Choice, Noul, Score


# ──────────────────────────── response access ─────────────────────────────


def _field(obj: Any, name: str, default: Any = None) -> Any:
    """Read `name` off an object OR a plain dict (forward-compatible).

    The SDK's cached response properties can raise (``request_id`` raises when
    the API omitted the header), so a failed read degrades to `default` rather
    than breaking the tick.
    """
    if obj is None:
        return default
    if isinstance(obj, dict):
        return obj.get(name, default)
    try:
        return getattr(obj, name, default)
    except Exception:  # noqa: BLE001 — never let a metadata read kill the desk
        return default


def answer_for(response: Any, kind: str, name: str) -> Any | None:
    """Typed answer for question `name` of answer-kind `kind`.

    Reads ``response.nouls`` / ``.choices`` / ``.scores`` (the SDK's cached
    accessors), falling back to the ``.answers`` union.
    """
    coll = _field(response, _ANSWER_COLLECTION.get(kind, ""), None)
    if isinstance(coll, dict) and name in coll:
        return coll[name]
    answers = _field(response, "answers", None)
    if isinstance(answers, dict):
        return answers.get(name)
    return None


def answer_value(answer: Any, field: str, default: Any = None) -> Any:
    """Read a field off an answer object (``.noul`` / ``.score`` / ``.confidence``)."""
    return _field(answer, field, default)


def usage_of(response: Any) -> dict:
    """Token usage as a plain dict, when the API reported it."""
    usage = _field(response, "usage", None)
    if usage is None:
        return {}
    if isinstance(usage, dict):
        return dict(usage)
    return {k: v for k in ("input_tokens", "output_tokens")
            if (v := getattr(usage, k, None)) is not None}


def _meta(response: Any) -> dict:
    return {"request_id": str(_field(response, "request_id", "") or ""),
            "usage": usage_of(response)}


# ──────────────────────────── portfolio select ────────────────────────────


def _select_row(i: int, cand: dict) -> dict:
    """Compact, model-facing view of one candidate (state stays small)."""
    pool = str(cand.get("pool") or "")
    base = str(cand.get("base") or "")
    return {
        "slot": i,
        "pool": pool[:8] or f"pool{i}",
        "pair": cand.get("pair") or base[:8],
        "base": base[:8],
        "tab": cand.get("tab") or "",
        "tvl_usd": round(float(cand.get("tvl") or 0.0), 2),
        "vol24_usd": round(float(cand.get("vol", cand.get("vol24")) or 0.0), 2),
        "fees24_usd": round(float(cand.get("fees", cand.get("fees24")) or 0.0), 2),
        "bin_step": cand.get("bin_step"),
        "rug_trust": round(float(cand.get("rug_noul") or 0.0), 3),
        "math_composite": round(float(cand.get("composite", cand.get("score")) or 0.0), 2),
    }


def _offline_pick(candidates: list[dict], max_positions: int) -> list[str]:
    """Deterministic fallback: math order, one slot per base."""
    seen: set = set()
    picks: list[str] = []
    for cand in candidates:
        base = cand.get("base")
        if base in seen:
            continue
        seen.add(base)
        picks.append(str(cand.get("pool") or ""))
        if len(picks) >= max_positions:
            break
    return picks


def select_portfolio(client: Any, candidates: list[dict],
                     max_positions: int = 5) -> dict:
    """Model multi-select: which candidates earn a slot in the portfolio.

    One ``Noul`` question per candidate, all in a single request. A question is
    a "yes" when ``noul >= SELECT_NOUL_FLOOR``; survivors are ordered by their
    noul (the model's own preference) and capped at `max_positions`.

    Returns ``{verdict, pools, count, nouls, request_id, usage, jev, reason}``.
    ``pools`` holds full pool addresses, best first. Never raises.
    """
    trust_floor = float(_m.TRUST_NOUL_FLOOR)
    clean = [c for c in candidates if float(c.get("rug_noul") or 0.0) >= trust_floor]
    clean = clean[:MAX_SELECT_QUESTIONS]

    out: dict = {"verdict": "SIT", "pools": [], "count": 0, "nouls": {},
                 "request_id": "", "usage": {}, "jev": JEV_OFF, "reason": ""}

    if not clean:
        out["reason"] = "no clean candidate for portfolio"
        return out

    if client is None:
        picks = _offline_pick(clean, max_positions)
        out.update(verdict="SELECT" if picks else "SIT", pools=picks,
                   count=len(picks), reason="mock portfolio pick (no model)")
        return out

    try:
        _, Noul, _ = _primitives()
    except Exception as exc:  # noqa: BLE001
        out["reason"] = f"model select failed: {exc}"
        return out

    state = {
        "book": {
            "quote": "USDC",
            "venue": "Meteora DLMM",
            "max_positions": max_positions,
            "style": "one-sided fee walls, up to one slot per token",
        },
        "candidates": [_select_row(i, c) for i, c in enumerate(clean)],
    }
    questions: dict = {}
    for i, cand in enumerate(clean):
        pair = cand.get("pair") or str(cand.get("base") or "")[:8] or f"candidate {i}"
        questions[f"slot_{i}"] = Noul(instructions=(
            f"Should {pair} (candidate {i}, {cand.get('tab') or 'top'} tab) earn one "
            f"of the {max_positions} concurrent slots in this DLMM fee book? Judge it "
            "on expected fee yield, depth, tape texture and rug cleanliness against "
            "the other candidates."
        ))

    try:
        resp = client.system_one(state=state, questions=questions)
    except Exception as exc:  # noqa: BLE001 — surface the failure as an abstain
        out["reason"] = f"model select failed: {exc}"
        return out

    nouls: dict[str, float] = {}
    for i, cand in enumerate(clean):
        raw = answer_value(answer_for(resp, "noul", f"slot_{i}"), "noul")
        try:
            nouls[str(cand.get("pool") or i)] = float(raw) if raw is not None else 0.0
        except (TypeError, ValueError):
            nouls[str(cand.get("pool") or i)] = 0.0

    want = sorted(((p, n) for p, n in nouls.items() if n >= SELECT_NOUL_FLOOR),
                  key=lambda kv: kv[1], reverse=True)
    pools = [p for p, _ in want[:max_positions]]
    best = max(nouls.values(), default=0.0)
    out.update(
        verdict="SELECT" if pools else "SIT",
        pools=pools,
        count=len(pools),
        nouls={p: round(n, 4) for p, n in nouls.items()},
        jev=JEV_ON,
        reason=(f"model kept {len(pools)}/{len(clean)} pools at noul>={SELECT_NOUL_FLOOR:.2f}"
                if pools else
                f"model kept no pool (best noul {best:.2f} < {SELECT_NOUL_FLOOR:.2f})"),
        **_meta(resp),
    )
    return out


# ─────────────────────────────── position size ────────────────────────────


def size_levels(role: str) -> list[float]:
    """Ordered %-of-book levels for a role, taken from the `_jev_math` envelope.

    Score levels must be ordered and finite, so the envelope's floor/ceiling
    (and the portfolio/minor caps) define the top of the scale and 0 defines
    "stay out".
    """
    role = (role or "portfolio").lower()
    if role == "major":
        lo, hi = float(_m.MAJOR_PCT_MIN), float(_m.MAJOR_PCT_MAX)
        return [0.0, round(lo, 4), round((lo + hi) / 2.0, 4), round(hi, 4)]
    cap = float(_m.PORTFOLIO_PCT_MAX if role == "portfolio" else _m.MINOR_PCT_MAX)
    return [0.0, round(cap * 0.32, 4), round(cap * 0.64, 4), round(cap, 4)]


def _level_text(levels: list[float]) -> list[str]:
    """Level legend: one dimension (how big a slice of the book)."""
    return [
        "0% of the book — stay out, this pool does not earn a slice",
        f"~{levels[1] * 100:.0f}% of the book — a small slice",
        f"~{levels[2] * 100:.0f}% of the book — a mid slice",
        f"~{levels[3] * 100:.0f}% of the book — the largest slice this role allows",
    ]


def lerp_levels(levels: list[float], frac: float) -> float:
    """Value at fractional index `frac` across an ordered level list."""
    if not levels:
        return 0.0
    top = float(len(levels) - 1)
    x = max(0.0, min(top, float(frac)))
    i = int(x)
    if i >= len(levels) - 1:
        return float(levels[-1])
    t = x - i
    return float(levels[i]) * (1.0 - t) + float(levels[i + 1]) * t


def size_position(client: Any, *, role: str, tvl: float, vol24: float,
                  bin_step: float, dynamic_fee_pct: float,
                  outside_slots: int, rug_noul: float,
                  math_pct: float = 0.0) -> dict:
    """Model Score: what fraction of the book this pool should get.

    A Score answer's ``score`` is a position on the ordered `size_levels`
    scale, so the reported pct is the interpolation of the level values at that
    position — then clamped to the role envelope from `_jev_math`, and zeroed
    if the model's confidence is below `SIZE_CONF_FLOOR` or the rug trust floor
    fails (the math veto).

    Returns ``{pct, score, confidence, levels, math_pct, request_id, usage,
    jev, reason}``. ``pct == 0`` means SIT. Never raises.
    """
    role = (role or "portfolio").lower()
    if role not in ("major", "portfolio", "minor"):
        role = "portfolio"
    levels = size_levels(role)
    cap = float(_m.MAJOR_PCT_MAX if role == "major" else
                (_m.PORTFOLIO_PCT_MAX if role == "portfolio" else _m.MINOR_PCT_MAX))

    out: dict = {"pct": 0.0, "score": None, "confidence": None, "levels": levels,
                 "math_pct": round(float(math_pct or 0.0), 4),
                 "request_id": "", "usage": {}, "jev": JEV_OFF, "reason": ""}

    if float(rug_noul or 0.0) < float(_m.TRUST_NOUL_FLOOR):
        out["reason"] = "rug below trust floor -> SIT"
        return out
    if client is None:
        out["reason"] = "mock: math sizes (see _jev_math.jev_size)"
        return out

    try:
        _, _, Score = _primitives()
    except Exception as exc:  # noqa: BLE001
        out["reason"] = f"model size failed: {exc}"
        return out

    state = {"pool": {
        "role": role,
        "tvl_usd": float(tvl or 0.0),
        "vol24_usd": float(vol24 or 0.0),
        "bin_step": float(bin_step or 0.0),
        "dynamic_fee_pct": float(dynamic_fee_pct or 0.0),
        "out_of_range_slots": int(outside_slots or 0),
        "rug_trust": round(float(rug_noul or 0.0), 3),
        "math_baseline_pct": round(float(math_pct or 0.0), 4),
    }}
    questions = {"size": Score(
        instructions=(f"What slice of the fee book should this {role} Meteora DLMM "
                      "position get, given its depth, tape heat, volatility fee and "
                      "rug cleanliness?"),
        criteria=_level_text(levels),
    )}

    try:
        resp = client.system_one(state=state, questions=questions)
    except Exception as exc:  # noqa: BLE001
        out["reason"] = f"model size failed: {exc}"
        return out

    ans = answer_for(resp, "score", "size")
    raw_score = answer_value(ans, "score")
    raw_conf = answer_value(ans, "confidence")
    if raw_score is None:
        out["reason"] = "model size returned no score"
        return out
    try:
        score = float(raw_score)
        conf = float(raw_conf) if raw_conf is not None else 1.0
    except (TypeError, ValueError):
        out["reason"] = "model size returned an unreadable score"
        return out

    pct = lerp_levels(levels, score)
    # Math disposes: envelope clamp, then the confidence gate.
    pct = max(0.0, min(cap, pct))
    if role == "major" and pct > 0:
        pct = max(float(_m.MAJOR_PCT_MIN), pct)
    if conf < SIZE_CONF_FLOOR:
        reason = f"model low confidence {conf:.2f} < {SIZE_CONF_FLOOR:.2f} -> SIT"
        pct = 0.0
    elif pct <= 0.0:
        reason = f"model scored {score:.2f}/{len(levels) - 1} -> stay out"
    else:
        reason = (f"model score {score:.2f}/{len(levels) - 1} conf {conf:.2f} "
                  f"-> {pct:.1%} of book")

    out.update(pct=round(pct, 4), score=round(score, 3), confidence=round(conf, 3),
               jev=JEV_ON, reason=reason, **_meta(resp))
    return out
