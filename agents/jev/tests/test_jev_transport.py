"""End-to-end wire contract for the JEV decision client. No network, no key.

Drives the REAL `typesafe_sdk.TypeSafeClient` (the object `_jev_sdk._client()`
returns) against an `httpx2.MockTransport`, so this test covers the full path:

    JEV client -> real SDK serialization -> HTTP request -> API-shaped response
               -> real SDK parsing -> JEV answer handling -> verdict

It asserts the request the desk actually puts on the wire (endpoint, model,
question type, state shape) and that the verdict follows from the response.
If the SDK is not installed (a bare Condor checkout), the test skips.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROUTINES = Path(__file__).resolve().parents[1] / "routines"
sys.path.insert(0, str(ROUTINES))

from _jev_sdk import JEV_ON, size_levels, select_portfolio, size_position  # noqa: E402

try:
    import httpx2
    from typesafe_sdk import TypeSafeClient
except Exception:  # noqa: BLE001 — optional dependency
    httpx2 = None
    TypeSafeClient = None


class Recorder:
    """Mock transport handler: records the outgoing request, returns canned JSON."""

    def __init__(self, nouls=None, score=None, confidence=0.9, request_id="req-1"):
        self.requests: list[dict] = []
        self.nouls = nouls or {}
        self.score = score
        self.confidence = confidence
        self.headers = ({"x-typesafe-request-id": request_id} if request_id else {})

    def handler(self, request):
        body = json.loads(request.content.decode("utf-8"))
        self.requests.append({
            "method": request.method,
            "url": str(request.url),
            "path": request.url.path,
            "auth_present": bool(request.headers.get("authorization")),
            "body": body,
        })
        answers = {k: {"type": "noul", "noul": float(v)}
                   for k, v in self.nouls.items()}
        if self.score is not None:
            answers["size"] = {
                "type": "score",
                "score": float(self.score),
                "confidence": self.confidence,
                "legend": {0: "stay out", 1: "small", 2: "mid", 3: "large"},
                "probabilities": {"0": 0.0, "1": 0.1, "2": 0.2, "3": 0.7},
            }
        payload = {"model": "jev-latest", "answers": answers,
                   "usage": {"input_tokens": 240, "output_tokens": 9}}
        return httpx2.Response(200, json=payload, headers=self.headers)


def _sdk_client(rec: Recorder):
    http = httpx2.Client(transport=httpx2.MockTransport(rec.handler))
    return TypeSafeClient(api_key="ts_test_key", model="jev-latest", http_client=http)


def _cands():
    return [
        {"pool": "PoolAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA", "base": "BaseA",
         "pair": "AAA-USDC", "tab": "top", "score": 81.0, "composite": 81.0,
         "rug_noul": 0.94, "tvl": 2.1e6, "vol": 1.4e6, "fees": 900.0, "bin_step": 20},
        {"pool": "PoolBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBB", "base": "BaseB",
         "pair": "BBB-USDC", "tab": "trending", "score": 74.0, "composite": 74.0,
         "rug_noul": 0.88, "tvl": 8e5, "vol": 6e5, "fees": 420.0, "bin_step": 50},
    ]


def test_request_hits_systemone_with_a_noul_per_candidate():
    """The desk really can put a Noul fan-out on the wire."""
    rec = Recorder(nouls={"slot_0": 0.91, "slot_1": 0.30})
    out = select_portfolio(_sdk_client(rec), _cands(), max_positions=5)

    assert len(rec.requests) == 1
    req = rec.requests[0]
    assert req["method"] == "POST"
    assert req["path"].endswith("/v1/systemone")
    assert req["auth_present"] is True
    assert req["body"]["model"] == "jev-latest"

    questions = req["body"]["questions"]
    assert sorted(questions) == ["slot_0", "slot_1"]
    assert all(q["type"] == "noul" for q in questions.values())
    assert all("instructions" in q for q in questions.values())
    # state carries the candidate rows the model reasons over
    assert len(req["body"]["state"]["candidates"]) == 2
    assert req["body"]["state"]["candidates"][0]["pair"] == "AAA-USDC"
    assert req["body"]["state"]["book"]["max_positions"] == 5
    # answer handling: only the pool above the floor is kept
    assert out["jev"] == JEV_ON
    assert out["pools"] == ["PoolAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"]
    assert out["request_id"] == "req-1"
    assert out["usage"] == {"input_tokens": 240, "output_tokens": 9}


def test_request_hits_systemone_with_a_score_question():
    """Size really goes out as a Score with an ordered level legend."""
    rec = Recorder(score=3.0)
    out = size_position(_sdk_client(rec), role="major", tvl=2.1e6, vol24=1.4e6,
                        bin_step=20, dynamic_fee_pct=0.05, outside_slots=5,
                        rug_noul=0.94, math_pct=0.4)

    req = rec.requests[0]
    assert req["path"].endswith("/v1/systemone")
    q = req["body"]["questions"]["size"]
    assert q["type"] == "score"
    assert isinstance(q["criteria"], list) and len(q["criteria"]) == len(size_levels("major"))
    assert req["body"]["state"]["pool"]["rug_trust"] == 0.94
    # top level of the envelope -> full role allocation, parsed from the response
    assert out["jev"] == JEV_ON
    assert out["pct"] == 0.45
    assert out["confidence"] == 0.9
    assert out["usage"]["input_tokens"] == 240


def test_mid_score_lands_between_levels_end_to_end():
    rec = Recorder(score=1.5)
    out = size_position(_sdk_client(rec), role="portfolio", tvl=2e6, vol24=1e6,
                        bin_step=20, dynamic_fee_pct=0.05, outside_slots=5,
                        rug_noul=1.0)
    levels = size_levels("portfolio")
    assert out["pct"] == round(levels[1] * 0.5 + levels[2] * 0.5, 4)


def test_api_error_response_is_an_abstain_not_a_crash():
    """A 500 from the API must degrade to SIT, never break the tick."""
    def handler(request):
        return httpx2.Response(500, json={"detail": "boom"})

    http = httpx2.Client(transport=httpx2.MockTransport(handler))
    client = TypeSafeClient(api_key="ts_test_key", http_client=http)
    out = select_portfolio(client, _cands(), max_positions=5)
    assert out["verdict"] == "SIT" and out["jev"] != JEV_ON
    assert "failed" in out["reason"]


def test_missing_request_id_header_does_not_break_the_desk():
    """`response.request_id` raises when the header is absent — the desk must not."""
    rec = Recorder(nouls={"slot_0": 0.9, "slot_1": 0.1}, request_id=None)
    out = select_portfolio(_sdk_client(rec), _cands(), max_positions=5)
    assert out["jev"] == JEV_ON and out["count"] == 1
    assert out["request_id"] == ""


if __name__ == "__main__":
    if TypeSafeClient is None or httpx2 is None:
        print("SKIP: typesafe-sdk / httpx2 not installed (pip install typesafe-sdk)")
        sys.exit(0)
    tests = [v for k, v in list(globals().items()) if k.startswith("test_")]
    for fn in tests:
        fn()
        print("ok", fn.__name__)
    print(f"ALL {len(tests)} PASSED")
