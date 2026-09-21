"""Regression tests for the jev_gate input contract.

Both cases here were silent-wrong-default traps found on a live run: no error,
no traceback — just a plausible-looking SIT/$0 that read like a real decision.

  1. `pool_state` is the executor's CURRENT state, never a verb. Passing "WAIT"
     used to satisfy `has_position = state not in {"NONE", ""}` and take the
     "already holding" branch.
  2. `pool_usd` is DERIVED (wallet_usd x pool_pct). Passing it was ignored, so
     pool_pct stayed 0 and the gate sized the position at $0.
"""

from __future__ import annotations

import asyncio
import importlib.util
import sys
from pathlib import Path

ROUTINES = Path(__file__).resolve().parents[1] / "routines"
sys.path.insert(0, str(ROUTINES))

import _jev_math as M  # noqa: E402

import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def _isolate_cwd(tmp_path, monkeypatch):
    """`run()` writes a gate snapshot relative to cwd — keep it out of the repo."""
    monkeypatch.chdir(tmp_path)


def _load_gate():
    spec = importlib.util.spec_from_file_location("jev_gate_under_test",
                                                  ROUTINES / "jev_gate.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


gate = _load_gate()
VERBS = (M.WAIT, M.SHIFT, M.REBUILD, M.SIT)


def test_config_has_no_pool_usd_field():
    """pool_usd is derived, not an input.

    If it ever becomes a real field, a caller passing both pool_usd and pool_pct
    would have two sources of truth for the amount — exactly the trap this
    guards. Keep the single input (`pool_pct`) and the derivation.
    """
    assert "pool_usd" not in gate.Config.model_fields
    assert "pool_pct" in gate.Config.model_fields


def test_pool_usd_input_is_ignored_and_leaves_amount_zero():
    """The documented-but-wrong call: passing pool_usd alone sizes at $0."""
    cfg = gate.Config(pool_state="NONE", pool_usd=42.0)
    assert cfg.pool_pct == 0.0
    assert cfg.wallet_usd * cfg.pool_pct == 0.0


def test_pool_pct_drives_the_amount():
    """pool_pct IS the input: the slice is pool_pct x book, whatever the mode."""
    cfg = gate.Config(pool_state="NONE", pool_pct=0.31, price=0.16)
    out = asyncio.run(gate.run(cfg, None))
    assert "model_pct=0.310" in out
    assert f"pool_usd=${cfg.wallet_usd * 0.31:.2f}" in out


def test_verb_passed_as_pool_state_is_refused_loudly():
    """WAIT/SHIFT/REBUILD/SIT are this routine's OUTPUT, not its input.

    Each must be refused with BAD INPUT instead of being silently mapped onto
    the has_position branch and answered with a believable SIT.
    """
    for verb in VERBS:
        out = asyncio.run(gate.run(gate.Config(pool_state=verb), None))
        assert "BAD INPUT" in out, f"{verb} was not refused: {out!r}"
        assert "VERB" in out, f"{verb} refusal does not explain itself"
        assert "VERB=" not in out, f"{verb} still produced a verdict"


def test_verb_check_is_case_insensitive():
    """The gate upper-cases pool_state, so lowercase verbs must refuse too."""
    out = asyncio.run(gate.run(gate.Config(pool_state="wait"), None))
    assert "BAD INPUT" in out


def test_legitimate_states_still_run():
    """The guard must not swallow the real states."""
    for state in ("NONE", "IN_RANGE", "OUT_OF_RANGE", "FILLED_BUY"):
        out = asyncio.run(gate.run(gate.Config(pool_state=state, pool_pct=0.2), None))
        assert "BAD INPUT" not in out, f"{state} was wrongly refused"
