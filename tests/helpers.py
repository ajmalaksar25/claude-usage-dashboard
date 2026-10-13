"""Shared fixtures for the v5 tests: build transcripts in a temp dir and index them.

Stdlib only. Pricing is reset to the seed tables and pricing.sync is patched out, so nothing
touches the network, ~/.claude* or usage.db.
"""
import json
import sys
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import indexer  # noqa: E402
import pricing  # noqa: E402

M = 10**6


def reset_pricing():
    pricing._live, pricing._src, pricing._fast, pricing._fast_src = {}, {}, {}, {}
    pricing._geo, pricing._meta, pricing._ext = {}, {}, {}


def asst(ts, sid, mid, model="claude-sonnet-5", inp=0, out=0, cr=0, c5=0, usage=None, stop="end_turn", **meta):
    """An assistant transcript record. meta keys are top-level record fields (isSidechain, ...).

    stop=None makes a partial record (written before the final usage, as subagent transcripts do)."""
    u = usage if usage is not None else {
        "input_tokens": inp, "output_tokens": out, "cache_read_input_tokens": cr,
        "cache_creation_input_tokens": c5,
        "cache_creation": {"ephemeral_5m_input_tokens": c5, "ephemeral_1h_input_tokens": 0},
        "server_tool_use": {"web_search_requests": 0, "web_fetch_requests": 0}, "speed": "standard",
    }
    rec = {"type": "assistant", "timestamp": ts, "sessionId": sid, "uuid": f"u-{mid}",
           "cwd": "/work/proj", "message": {"id": mid, "model": model, "usage": u, "stop_reason": stop}}
    rec.update(meta)
    return rec


def ms(ts: str) -> int:
    """ISO UTC timestamp -> epoch ms (cost-state startTime)."""
    from datetime import datetime
    return int(datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp() * 1000)


def cost_state(sid, start, total, models, duration=1000):
    """models: {model: (costUSD, webSearchRequests[, {token field overrides}])}; tokens default to 1."""
    def mu(v):
        d = {"inputTokens": 1, "outputTokens": 1, "thinkingTokens": 0, "cacheReadInputTokens": 0,
             "cacheCreationInputTokens": 0, "webSearchRequests": v[1], "costUSD": v[0]}
        d.update(v[2] if len(v) > 2 else {})
        return d
    return {"type": "cost-state", "sessionId": sid, "totalCostUSD": total, "totalAPIDuration": 10,
            "totalDuration": duration, "totalLinesAdded": 1, "totalLinesRemoved": 0, "startTime": start,
            "hasUnknownModelCost": False,
            "modelUsage": {m: mu(v) for m, v in models.items()}}


def write_jsonl(path: Path, records: list):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r) + "\n" for r in records))


def index(db: Path, accounts: list[dict]):
    """reindex with pricing.sync patched out (seed prices, no network)."""
    with mock.patch.object(indexer.pricing, "sync", return_value={}):
        return indexer.reindex(db, accounts=accounts, verbose=False)
