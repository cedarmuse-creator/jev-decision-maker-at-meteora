#!/usr/bin/env python3
"""Flip JEV between its test and prod envelopes — one command, one source.

The book, the slot budget and the per-position floor are coupled, and the desk
seeds its Start dialog from `strategy.md`. Editing five numbers by hand is how
they drift apart, so this writes them together from the profile in `_jev_math`.

    python agents/jev/set_mode.py prod     # 800 USDC / 5 slots / 100.0 floor
    python agents/jev/set_mode.py test     # 100 USDC / 2 slots /  12.0 floor
    python agents/jev/set_mode.py          # report the current mode, change nothing

Then restart the desk: `_jev_math` reads the file's `mode:` key at import, so the
routine defaults and the dashboard both follow. `$JEV_MODE` still overrides the
file per process, for an organizer who would rather pin a profile from the env.
"""

from __future__ import annotations

import importlib.util
import os
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
STRATEGY = HERE / "strategies" / "jev_desk" / "strategy.md"


def _load_math():
    spec = importlib.util.spec_from_file_location(
        "_jev_math", HERE / "routines" / "_jev_math.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


M = _load_math()


def _split(text: str) -> tuple[str, str, str]:
    """(head, frontmatter, tail). Refuses to guess if there is no frontmatter."""
    parts = text.split("---")
    if len(parts) < 3:
        raise SystemExit(f"{STRATEGY} has no YAML frontmatter — refusing to edit")
    return parts[0], parts[1], "---".join(parts[2:])


def _set_key(front: str, key: str, value: str) -> tuple[str, list[str]]:
    """Set every `key:` in the frontmatter. Returns (front, old values).

    Every occurrence, not just the first: `max_open_executors` appears both in
    `default_config` and in `risk_limits`, and the two must agree.
    """
    pat = re.compile(rf"^([ \t]*{re.escape(key)}:[ \t]*)(.*)$", re.MULTILINE)
    olds = [m.group(2).strip() for m in pat.finditer(front)]
    if not olds:
        return front, []
    return pat.sub(lambda m: f"{m.group(1)}{value}", front), olds


def _targets(mode: str) -> dict[str, str]:
    prof = M.MODE_PROFILES[mode]
    slots = int(prof["max_positions"])
    book = int(prof["book_usd"])
    return {
        "mode": mode,
        "total_amount_quote": str(book),
        "max_open_executors": str(slots),
        "min_position_usd": f"{float(prof['min_position_usd']):.1f}",
        "portfolio_pct_max": f"{1.0 / slots:.2f}",
        # Condor's per-position risk limit must not sit below what the sizing
        # math is allowed to produce, or the risk gate refuses every slice the
        # mode just authorised. Cap x book <= book, so the book is the ceiling.
        "max_position_size_quote": str(book),
    }


def report() -> int:
    active = M._mode_key()
    prof = M.mode_profile(active)
    print(f"strategy file : {STRATEGY}")
    print(f"  mode: key   : {M.strategy_mode() or '(none — falls back to default)'}")
    print(f"  $JEV_MODE   : {os.environ.get('JEV_MODE') or '(unset)'}")
    print(f"  active mode : {active} ({prof['label']})"
          f"{'' if M.MODE_IS_KNOWN else '  <- unknown, fell back'}")
    print(f"  book        : {prof['book_usd']:.0f} USDC")
    print(f"  slots       : {prof['max_positions']}")
    print(f"  min slice   : {prof['min_position_usd']:.1f}")
    print(f"  per-pool cap: {1.0 / prof['max_positions']:.2f}")
    print()
    print("Available modes: " + ", ".join(sorted(M.MODE_PROFILES)))
    return 0


def apply(mode: str) -> int:
    if mode not in M.MODE_PROFILES:
        raise SystemExit(
            f"unknown mode {mode!r} — expected one of: "
            + ", ".join(sorted(M.MODE_PROFILES)))

    original = STRATEGY.read_text(encoding="utf-8")
    head, front, tail = _split(original)

    changes = []
    for key, value in _targets(mode).items():
        front, olds = _set_key(front, key, value)
        if not olds:
            print(f"  !! {key}: not found in the frontmatter — skipped")
            continue
        for old in olds:
            if old != value:
                changes.append(f"  {key}: {old} -> {value}")

    updated = f"{head}---{front}---{tail}"
    if updated == original:
        print(f"already on {mode} — nothing to write.")
        return report()

    STRATEGY.write_text(updated, encoding="utf-8")
    prof = M.MODE_PROFILES[mode]
    print(f"set mode={mode}  ({prof['book_usd']:.0f} USDC / "
          f"{prof['max_positions']} slots / {prof['min_position_usd']:.1f} floor)")
    print("\n".join(changes) if changes else "  (values already matched)")
    print()
    print("Restart the desk to pick it up.")
    return 0


def main(argv: list[str]) -> int:
    if len(argv) > 1:
        return apply(argv[1].strip().lower())
    return report()


if __name__ == "__main__":
    sys.exit(main(sys.argv))
