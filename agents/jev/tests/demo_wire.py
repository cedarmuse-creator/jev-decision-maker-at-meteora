"""Show the exact request the JEV desk puts on the wire, and the verdict it reaches.

No network: the real TypeSafeClient talks to a mock transport.
Run:  ./.venv/Scripts/python.exe tests/demo_wire.py
"""
import importlib.util
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "routines"))

import httpx2
from typesafe_sdk import TypeSafeClient

import _jev_sdk as sdk

seen = {}


def handler(request):
    seen["body"] = json.loads(request.content.decode())
    seen["url"] = str(request.url)
    nouls = {k: {"type": "noul", "noul": v} for k, v in
             {"slot_0": 0.93, "slot_1": 0.71, "slot_2": 0.64, "slot_3": 0.31}.items()}
    nouls["size"] = {"type": "score", "score": 2.6, "confidence": 0.74,
                     "legend": {0: "stay out", 1: "small", 2: "mid", 3: "large"},
                     "probabilities": {"0": 0.0, "1": 0.05, "2": 0.3, "3": 0.65}}
    return httpx2.Response(200, json={"model": "jev-latest", "answers": nouls,
                                      "usage": {"input_tokens": 388, "output_tokens": 14}},
                           headers={"x-typesafe-request-id": "req_9f2c"})


client = TypeSafeClient(api_key="ts_demo", model="jev-latest",
                        http_client=httpx2.Client(transport=httpx2.MockTransport(handler)))

cands = [
    {"pool": "BVRbyLjjfSBcoyiYFuxbgKYnWuiFaF9CSXEa5vdSZ9Hh", "base": "So1111..1112",
     "pair": "SOL-USDC", "tab": "top", "composite": 78.4, "rug_noul": 1.0,
     "tvl": 2_400_000, "vol": 1_900_000, "fees": 760, "bin_step": 20},
    {"pool": "9nRFYC8RRVQJvZgWCjNwGDCwTL2nNEBm8T9VJhLZpuFF", "base": "Hs3Nv..BONK",
     "pair": "BONK-USDC", "tab": "trending", "composite": 72.1, "rug_noul": 0.92,
     "tvl": 320_000, "vol": 540_000, "fees": 650, "bin_step": 50},
    {"pool": "7qbRF6YsyGuLUVs6Y1q64bdVrfe4ZcUUz1JRdoVNUJnm", "base": "USDG..mint",
     "pair": "USDG-USDC", "tab": "rwa", "composite": 64.0, "rug_noul": 1.0,
     "tvl": 1_100_000, "vol": 600_000, "fees": 120, "bin_step": 20},
    {"pool": "3ucNos4NbumPLZNWztqGHNFFgkHeRMBQAVemeeomsUxv", "base": "Dirty..mint",
     "pair": "DRAIN-USDC", "tab": "new", "composite": 90.0, "rug_noul": 0.05,
     "tvl": 900_000, "vol": 800_000, "fees": 900, "bin_step": 80},
]

out = sdk.select_portfolio(client, cands, max_positions=5)
print("=" * 78)
print("REQUEST THE DESK SENDS  (", seen["url"], ")")
print("=" * 78)
print(json.dumps(seen["body"], indent=2)[:2200])
print("\n" + "=" * 78)
print("VERDICT")
print("=" * 78)
print(json.dumps({k: v for k, v in out.items()}, indent=2))

size = sdk.size_position(client, role="portfolio", tvl=2_400_000, vol24=1_900_000,
                         bin_step=20, dynamic_fee_pct=0.05, outside_slots=6,
                         rug_noul=1.0, math_pct=0.19)
print("\nSIZE (Score question -> % of book):")
print(json.dumps(size, indent=2))
