"""Validate JEV against Condor's real loaders (no network, no trading).

Organizers: run from the Condor repo root
  uv run python agents/jev/tests/validate_agent.py

The loader/strategy checks need a Condor checkout; the file-level checks
(identity leaks, isolation) run anywhere and still report.
"""
import ast
import inspect
import os
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO))
os.chdir(REPO)


def _local_list(env_var: str, filename: str) -> tuple[str, ...]:
    """Load a deny-list from the environment or a local file at the repo root.

    These lists are deliberately NOT hardcoded: naming sibling entries or model
    providers inside a public repo is itself the leak the checks below exist to
    prevent. Supply them locally via the env var (comma- or newline-separated)
    or a file beside this repo's root. With neither present the structural
    checks still run and simply report nothing to scan for.
    """
    raw = os.environ.get(env_var, "")
    path = REPO / filename
    if not raw and path.exists():
        raw = path.read_text(encoding="utf-8")
    return tuple(m.strip().lower() for m in re.split(r"[,\n]", raw) if m.strip())


# Strings that must never appear in the agent's own instructions.
PROVIDER_STRINGS = _local_list("JEV_PROVIDER_STRINGS", ".provider-strings")
# Identity markers of sibling entries. Bare English words are deliberately NOT
# listed: JEV uses SHIFT as its own re-site verb, so only slugs and file names
# count as a leak here.
SIBLING_MARKERS = _local_list("JEV_SIBLING_MARKERS", ".sibling-markers")

ok = True

AGENT_DIR = REPO / "agents/jev"

print("=== AGENT INSTRUCTIONS (file-level) ===")
agent_md = (AGENT_DIR / "AGENT.md").read_text(encoding="utf-8")
body = agent_md.lower()
leaked = [b for b in PROVIDER_STRINGS if b in body]
if leaked:
    print(f"  [FAIL] AGENT.md body leaks provider names: {leaked}")
    ok = False
else:
    print("  [OK] AGENT.md body has no provider names")
hit = [b for b in SIBLING_MARKERS if b in body]
if hit:
    print(f"  [FAIL] AGENT.md names another entry: {hit}")
    ok = False
else:
    print("  [OK] AGENT.md names no other entry")

print("\n=== SIBLING NAME SCAN ===")
siblings = []
for py in AGENT_DIR.rglob("*"):
    if py.suffix not in {".py", ".md"} or py.name == "validate_agent.py":
        continue
    text = py.read_text(encoding="utf-8").lower()
    for marker in SIBLING_MARKERS:
        if marker in text or marker in py.name.lower():
            siblings.append(f"{py.relative_to(AGENT_DIR)}:{marker}")
if siblings:
    print("  [FAIL] sibling names leaked:", siblings)
    ok = False
else:
    print("  [OK] no sibling names in agent files")

print("\n=== ISOLATION ===")
imports = []
for py in AGENT_DIR.rglob("*.py"):
    tree = ast.parse(py.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            imports.append((py.name, node.module))
        elif isinstance(node, ast.Import):
            for a in node.names:
                imports.append((py.name, a.name))
bad = [
    f"{f}: {m}"
    for f, m in imports
    if m.startswith("agents.") or any(k in m.lower() for k in SIBLING_MARKERS)
]
if bad:
    print("  [FAIL] cross-agent imports:", bad)
    ok = False
else:
    print("  [OK] no other-agent imports")

print("\n=== CONDOR LOADER CHECKS ===")
try:
    from routines.base import discover_routines_from_path
except ImportError as exc:  # not a Condor checkout
    print(f"  [SKIP] Condor not importable here ({exc}).")
    print("         Run this script from the Condor repo root for the loader checks.")
    print("\n=== RESULT:", "FILE CHECKS PASSED" if ok else "FAILURES PRESENT", "===")
    sys.exit(0 if ok else 1)

rdir = REPO / "agents/jev/routines"
found = discover_routines_from_path(rdir, agent_slug="jev")
print("=== ROUTINE DISCOVERY ===")
for name in sorted(found):
    info = found[name]
    fields = list(info.config_class.model_fields) if getattr(info, "config_class", None) else []
    print(f"  [OK] {name:<22} category={info.category:<12} config_fields={fields}")
for expected in ("jev_scan", "jev_scout_card", "jev_gate", "jev_select"):
    if expected not in found:
        print(f"  [FAIL] {expected} NOT discovered")
        ok = False
for helper in ("_jev_math", "_jev_sdk"):
    if helper in found:
        print(f"  [FAIL] {helper} should not be a routine (helper only)")
        ok = False

print("\n=== ROUTINE CONTRACT ===")
VALID = {"Market Data", "Analysis", "Arbitrage", "Monitoring"}
for name, info in sorted(found.items()):
    has_run = inspect.iscoroutinefunction(info.run_fn)
    good = has_run and info.config_class is not None and info.category in VALID
    ok &= good
    print(
        f"  [{'OK' if good else 'FAIL'}] {name:<22} "
        f"async_run={has_run} Config={bool(info.config_class)} CATEGORY={info.category!r}"
    )

print("\n=== AGENT / STRATEGY LOADING ===")
from condor.agents.agent import AgentStore
from condor.agents.strategy import StrategyStore
try:  # Condor renamed _slugify -> slugify; support both checkouts.
    from condor.agents.strategy import slugify as _slugify
except ImportError:  # pragma: no cover
    from condor.agents.strategy import _slugify

agent = AgentStore().get("jev")
if not agent:
    print("  [FAIL] agent 'jev' not loaded")
    ok = False
else:
    print(f"  [OK] agent slug={agent.slug} key={agent.agent_key} created_by={agent.created_by}")
    body = (agent.instructions or "").lower()
    leaked = [b for b in PROVIDER_STRINGS if b in body]
    if leaked:
        print(f"  [FAIL] AGENT.md body leaks provider names: {leaked}")
        ok = False
    else:
        print("  [OK] AGENT.md body has no provider names")
    hit = [b for b in SIBLING_MARKERS if b in body]
    if hit:
        print(f"  [FAIL] AGENT.md names another entry: {hit}")
        ok = False
    else:
        print("  [OK] AGENT.md names no other entry")

strats = [s for s in StrategyStore().list_all() if s.agent_slug == "jev"]
if not strats:
    print("  [FAIL] no strategy loaded for jev")
    ok = False
for s in strats:
    rl = (s.default_config or {}).get("risk_limits") or {}
    print(f"  [OK] strategy key={s.key} name={s.name}")
    print(f"       freq={s.default_config.get('frequency_sec')}s risk={rl}")
    if not isinstance(rl, dict):
        print("  [FAIL] risk_limits must be nested")
        ok = False
    exp = _slugify(s.name)
    d = (REPO / "agents/jev/strategies" / exp).is_dir()
    ok &= d
    print(f"  [{'OK' if d else 'FAIL'}] folder '{exp}' matches slugified name")

print("\n=== RESULT:", "ALL CHECKS PASSED" if ok else "FAILURES PRESENT", "===")
sys.exit(0 if ok else 1)
