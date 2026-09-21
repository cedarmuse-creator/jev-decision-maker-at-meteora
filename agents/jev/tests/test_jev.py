"""Tests for the JEV System One decision client. No network. No key.

Two layers:

  * offline — no client, the desk falls back to pure math (JEV off)
  * live — against a fake client that records the request the desk sends and
    returns real-shaped answers (the SDK's own typed primitives are asserted
    when `typesafe_sdk` is installed, so a wire-shape regression fails here)
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from types import SimpleNamespace

ROUTINES = Path(__file__).resolve().parents[1] / "routines"
sys.path.insert(0, str(ROUTINES))

from _jev_math import (  # noqa: E402
    MAJOR_PCT_MAX, MINOR_PCT_MAX, PORTFOLIO_PCT_MAX,
)

from _jev_sdk import (  # noqa: E402
    JEV_OFF, JEV_ON, MODEL_TAG, SELECT_NOUL_FLOOR, SIZE_CONF_FLOOR, _client,
    lerp_levels, size_levels, select_portfolio, size_position,
)

try:  # optional: installed in a Condor checkout / local venv
    import typesafe_sdk
except Exception:  # noqa: BLE001
    typesafe_sdk = None


# ─────────────────────────────── fake client ──────────────────────────────


class FakeResponse:
    """Mimics SystemOneResponse's cached accessors."""

    def __init__(self, nouls=None, score=None, confidence=0.9, as_dict=False):
        if as_dict:
            self.answers = {
                **{k: {"type": "noul", "noul": float(v)} for k, v in (nouls or {}).items()},
                **({"size": {"type": "score", "score": float(score),
                             "confidence": float(confidence)}}
                   if score is not None else {}),
            }
        else:
            self.nouls = {k: SimpleNamespace(noul=float(v))
                          for k, v in (nouls or {}).items()}
            self.scores = ({} if score is None else
                           {"size": SimpleNamespace(score=float(score),
                                                    confidence=float(confidence))})
        self.usage = SimpleNamespace(input_tokens=120, output_tokens=8)
        self.request_id = "req-test"


class FakeClient:
    """Stands in for TypeSafeClient: records the request, returns canned answers."""

    def __init__(self, nouls=None, score=None, confidence=0.9, boom=None, as_dict=False):
        self.calls: list[dict] = []
        self._nouls = nouls or {}
        self._score = score
        self._confidence = confidence
        self._boom = boom
        self._as_dict = as_dict

    def system_one(self, state=None, questions=None, **kwargs):
        self.calls.append({"state": state, "questions": questions, "kwargs": kwargs})
        if self._boom is not None:
            raise self._boom
        return FakeResponse(self._nouls, self._score, self._confidence, self._as_dict)


def _cands():
    return [
        {"pool": "POOLA" * 8, "base": "BASEA", "pair": "AAA-USDC", "tab": "top",
         "score": 80, "composite": 80, "rug_noul": 1.0, "tvl": 2e6, "vol": 1e6,
         "bin_step": 20},
        {"pool": "POOLB" * 8, "base": "BASEB", "pair": "BBB-USDC", "tab": "trending",
         "score": 40, "composite": 40, "rug_noul": 1.0, "tvl": 5e5, "vol": 3e5,
         "bin_step": 20},
    ]


# ───────────────────────── offline (pure-math) path ───────────────────────


def test_select_offline_picks_best_clean():
    out = select_portfolio(None, _cands())
    assert out["verdict"] == "SELECT"
    assert out["pools"][0] == "POOLA" * 8
    assert out["count"] == 2
    assert out["jev"] == JEV_OFF


def test_select_offline_excludes_dirty():
    cands = [
        {"pool": "A", "base": "AAA", "score": 90, "rug_noul": 0.1},   # dirty
        {"pool": "B", "base": "BBB", "score": 40, "rug_noul": 1.0},
    ]
    out = select_portfolio(None, cands)
    assert out["pools"] == ["B"]


def test_select_offline_dedupes_base():
    cands = [
        {"pool": "A2", "base": "AAA", "score": 65, "rug_noul": 1.0},
        {"pool": "A1", "base": "AAA", "score": 80, "rug_noul": 1.0},
    ]
    out = select_portfolio(None, cands, max_positions=5)
    assert out["pools"] == ["A2"]          # one slot per base, first in math order


def test_select_offline_caps_max_positions():
    cands = [{"pool": f"P{i}", "base": f"B{i}", "score": 90 - i, "rug_noul": 1.0}
             for i in range(8)]
    out = select_portfolio(None, cands, max_positions=3)
    assert out["count"] == 3


def test_select_sits_when_none_clean():
    out = select_portfolio(None, [{"pool": "A", "base": "AAA", "score": 80,
                                   "rug_noul": 0.1}])
    assert out["verdict"] == "SIT"
    assert out["pools"] == []
    assert out["count"] == 0


def test_size_offline_returns_zero_pct():
    # Offline the SDK defers sizing to _jev_math.jev_size.
    out = size_position(None, role="major", tvl=5e6, vol24=1e6, bin_step=10,
                        dynamic_fee_pct=0.05, outside_slots=5, rug_noul=1.0)
    assert out["pct"] == 0.0
    assert out["jev"] == JEV_OFF


def test_size_zero_on_rug_smell():
    out = size_position(None, role="minor", tvl=5e6, vol24=1e6, bin_step=10,
                        dynamic_fee_pct=0.05, outside_slots=5, rug_noul=0.1)
    assert out["pct"] == 0.0
    assert "rug" in out["reason"]


def test_client_none_without_key():
    old = os.environ.pop("TYPESAFE_API_KEY", None)
    try:
        assert _client() is None
    finally:
        if old is not None:
            os.environ["TYPESAFE_API_KEY"] = old


def test_client_with_key_matches_installed_sdk():
    old = os.environ.get("TYPESAFE_API_KEY")
    os.environ["TYPESAFE_API_KEY"] = "ts_test_key_not_used_for_network"
    try:
        c = _client()
    finally:
        if old is None:
            os.environ.pop("TYPESAFE_API_KEY", None)
        else:
            os.environ["TYPESAFE_API_KEY"] = old
    if typesafe_sdk is None:
        assert c is None                     # SDK missing -> pure math, no crash
    else:
        assert isinstance(c, typesafe_sdk.TypeSafeClient)
        assert callable(c.system_one)
        assert MODEL_TAG == "jev-latest"     # pinned model tag


# ───────────────────────────── live (model) path ──────────────────────────


def test_select_asks_one_noul_per_candidate_in_one_call():
    client = FakeClient(nouls={"slot_0": 0.9, "slot_1": 0.2})
    select_portfolio(client, _cands(), max_positions=5)

    assert len(client.calls) == 1                     # one request, fan-out
    q = client.calls[0]["questions"]
    assert isinstance(q, dict)
    assert sorted(q) == ["slot_0", "slot_1"]
    assert all(v.type == "noul" for v in q.values())  # Noul, not Choice/list
    assert all("instructions" in v.model_dump() for v in q.values())
    state = client.calls[0]["state"]
    assert state["book"]["max_positions"] == 5
    assert len(state["candidates"]) == 2
    if typesafe_sdk is not None:
        assert all(isinstance(v, typesafe_sdk.Noul) for v in q.values())


def test_select_applies_noul_floor_and_orders_by_noul():
    cands = [
        {"pool": "P1", "base": "B1", "pair": "B1-USDC", "score": 90, "rug_noul": 1.0},
        {"pool": "P2", "base": "B2", "pair": "B2-USDC", "score": 80, "rug_noul": 1.0},
        {"pool": "P3", "base": "B3", "pair": "B3-USDC", "score": 70, "rug_noul": 1.0},
    ]
    # P1 below the floor (dropped), P2/P3 kept, P3 preferred by the model.
    client = FakeClient(nouls={"slot_0": 0.2, "slot_1": 0.61, "slot_2": 0.93})
    out = select_portfolio(client, cands, max_positions=5)
    assert out["jev"] == JEV_ON
    assert out["pools"] == ["P3", "P2"]
    assert out["count"] == 2
    assert out["nouls"]["P3"] == 0.93
    assert out["request_id"] == "req-test"
    assert out["usage"]["input_tokens"] == 120


def test_select_respects_max_positions_against_model():
    cands = [{"pool": f"P{i}", "base": f"B{i}", "score": 90, "rug_noul": 1.0}
             for i in range(5)]
    client = FakeClient(nouls={f"slot_{i}": 0.9 for i in range(5)})
    out = select_portfolio(client, cands, max_positions=2)
    assert out["count"] == 2


def test_select_model_veto_when_no_noul_clears_floor():
    client = FakeClient(nouls={"slot_0": 0.1, "slot_1": 0.2})
    out = select_portfolio(client, _cands(), max_positions=5)
    assert out["verdict"] == "SIT"
    assert out["pools"] == []
    assert out["jev"] == JEV_ON
    assert "no pool" in out["reason"]


def test_select_never_sends_dirty_candidates():
    cands = [
        {"pool": "DIRTY", "base": "BAD", "score": 99, "rug_noul": 0.05},
        {"pool": "CLEAN", "base": "OK", "score": 50, "rug_noul": 1.0},
    ]
    client = FakeClient(nouls={"slot_0": 0.9})
    out = select_portfolio(client, cands, max_positions=5)
    assert list(client.calls[0]["questions"]) == ["slot_0"]   # only the clean one
    assert out["pools"] == ["CLEAN"]


def test_select_parses_answers_union_fallback():
    client = FakeClient(nouls={"slot_0": 0.95, "slot_1": 0.1}, as_dict=True)
    out = select_portfolio(client, _cands(), max_positions=5)
    assert out["pools"] == ["POOLA" * 8]


def test_select_sits_when_call_raises():
    client = FakeClient(boom=RuntimeError("401 unauthorized"))
    out = select_portfolio(client, _cands(), max_positions=5)
    assert out["verdict"] == "SIT"
    assert out["jev"] == JEV_OFF
    assert "failed" in out["reason"] and "401" in out["reason"]


def test_select_ignores_client_when_rug_floor_drops_all():
    client = FakeClient(nouls={"slot_0": 1.0})
    out = select_portfolio(client, [{"pool": "A", "base": "A", "rug_noul": 0.0}])
    assert out["jev"] == JEV_OFF and client.calls == []


def test_size_live_uses_score_levels():
    levels = size_levels("major")
    client = FakeClient(score=len(levels) - 1, confidence=0.9)   # top level
    out = size_position(client, role="major", tvl=5e6, vol24=1e6, bin_step=20,
                        dynamic_fee_pct=0.05, outside_slots=5, rug_noul=1.0)
    assert out["jev"] == JEV_ON
    assert out["pct"] == levels[-1] == 0.45
    q = client.calls[0]["questions"]["size"]
    assert q.type == "score"
    assert len(q.criteria) == len(levels)            # one legend entry per level
    assert "0%" in q.criteria[0] and "%" in q.criteria[-1]
    if typesafe_sdk is not None:
        assert isinstance(q, typesafe_sdk.Score)


def test_size_live_mid_score_interpolates_levels():
    levels = size_levels("portfolio")
    client = FakeClient(score=0.5, confidence=0.9)
    out = size_position(client, role="portfolio", tvl=5e6, vol24=1e6, bin_step=20,
                        dynamic_fee_pct=0.05, outside_slots=5, rug_noul=1.0)
    assert out["pct"] == levels[0] * 0.5 + levels[1] * 0.5


def test_size_live_respects_envelope_cap():
    client = FakeClient(score=9.0, confidence=1.0)   # model overshoots the scale
    out = size_position(client, role="portfolio", tvl=5e6, vol24=1e6, bin_step=20,
                        dynamic_fee_pct=0.05, outside_slots=5, rug_noul=1.0)
    assert out["pct"] == PORTFOLIO_PCT_MAX             # the envelope cap, not 9x


def test_size_live_sits_on_low_confidence():
    client = FakeClient(score=3.0, confidence=0.05)
    out = size_position(client, role="major", tvl=5e6, vol24=1e6, bin_step=20,
                        dynamic_fee_pct=0.05, outside_slots=5, rug_noul=1.0)
    assert out["pct"] == 0.0
    assert out["jev"] == JEV_ON
    assert "confidence" in out["reason"]


def test_size_confidence_floor_boundary():
    """Pin the floor itself — the model's live band straddles it, not sits inside."""
    below = FakeClient(score=3.0, confidence=SIZE_CONF_FLOOR - 0.01)
    out = size_position(below, role="major", tvl=5e6, vol24=1e6, bin_step=20,
                        dynamic_fee_pct=0.05, outside_slots=5, rug_noul=1.0)
    assert out["pct"] == 0.0
    assert "confidence" in out["reason"]

    above = FakeClient(score=3.0, confidence=SIZE_CONF_FLOOR + 0.01)
    out = size_position(above, role="major", tvl=5e6, vol24=1e6, bin_step=20,
                        dynamic_fee_pct=0.05, outside_slots=5, rug_noul=1.0)
    assert out["pct"] > 0.0


def test_live_confidence_band_is_above_the_floor():
    """The band the live model actually reports must clear the floor.

    Regression guard: a floor inside the model's observed 0.29-0.37 band made the
    desk abstain on ~2 of 3 ticks for no signal reason.
    """
    for conf in (0.29, 0.31, 0.34, 0.37):
        client = FakeClient(score=2.4, confidence=conf)
        out = size_position(client, role="portfolio", tvl=1.9e5, vol24=2.7e6,
                            bin_step=20, dynamic_fee_pct=0.19, outside_slots=0,
                            rug_noul=1.0)
        assert out["pct"] > 0.0, f"confidence {conf} was treated as an abstain"
        assert out["jev"] == JEV_ON


def test_size_live_sits_at_zero_level():
    client = FakeClient(score=0.0, confidence=0.95)
    out = size_position(client, role="major", tvl=5e6, vol24=1e6, bin_step=20,
                        dynamic_fee_pct=0.05, outside_slots=5, rug_noul=1.0)
    assert out["pct"] == 0.0


def test_size_live_never_sends_dirty_pool():
    client = FakeClient(score=3.0)
    out = size_position(client, role="major", tvl=5e6, vol24=1e6, bin_step=20,
                        dynamic_fee_pct=0.05, outside_slots=5, rug_noul=0.1)
    assert out["pct"] == 0.0 and client.calls == []


def test_size_sits_when_call_raises():
    client = FakeClient(boom=TimeoutError("connect timeout"))
    out = size_position(client, role="major", tvl=5e6, vol24=1e6, bin_step=20,
                        dynamic_fee_pct=0.05, outside_slots=5, rug_noul=1.0)
    assert out["pct"] == 0.0
    assert out["jev"] == JEV_OFF
    assert "failed" in out["reason"]


def test_levels_are_ordered_and_inside_the_envelope():
    # The top Score level must sit inside the role's own envelope -- the envelope
    # moves with MAX_POSITIONS, so a literal cap here would rot on every change.
    caps = {"major": MAJOR_PCT_MAX, "portfolio": PORTFOLIO_PCT_MAX,
            "minor": MINOR_PCT_MAX}
    for role in ("major", "portfolio", "minor"):
        levels = size_levels(role)
        assert len(levels) >= 3                       # a Score scale needs levels
        assert levels == sorted(levels)               # ordered: required by Score
        assert levels[0] == 0.0
        assert 0.0 < levels[-1] <= caps[role] + 1e-9


def test_lerp_levels_bounds():
    lv = [0.0, 8.0, 16.0, 25.0]
    assert lerp_levels(lv, 0) == 0.0
    assert lerp_levels(lv, 3) == 25.0
    assert lerp_levels(lv, -5) == 0.0                 # clamped, never negative
    assert lerp_levels(lv, 99) == 25.0
    assert lerp_levels([], 1.0) == 0.0


def test_select_noul_floor_is_a_probability():
    assert 0.5 <= SELECT_NOUL_FLOOR <= 0.9


if __name__ == "__main__":
    tests = [v for k, v in list(globals().items()) if k.startswith("test_")]
    for fn in tests:
        fn()
        print("ok", fn.__name__)
    print(f"ALL {len(tests)} PASSED")
