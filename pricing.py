"""Anthropic API pricing per 1M tokens (USD).

Lifted from D:\\Ajmal\\Projects\\claude-cost-estimator\\calc_cost.py and kept in
sync with platform.claude.com/docs/en/about-claude/pricing.

Tuple order: (input, output, cache_5m_write, cache_1h_write, cache_read).
"""

PRICING = {
    "opus_new": (5.00, 25.00, 6.25, 10.00, 0.50),    # Opus 4.7 / 4.6 / 4.5
    "opus_old": (15.00, 75.00, 18.75, 30.00, 1.50),  # Opus 4.1 / 4 / 3
    "sonnet":   (3.00, 15.00, 3.75, 6.00, 0.30),     # Sonnet 4.6 / 4.5 / 4 / 3.7
    "haiku_45": (1.00, 5.00, 1.25, 2.00, 0.10),
    "haiku_35": (0.80, 4.00, 1.00, 1.60, 0.08),
    "haiku_3":  (0.25, 1.25, 0.30, 0.50, 0.03),
}


def model_tier(model: str) -> str | None:
    m = (model or "").lower()
    if "opus" in m:
        if any(v in m for v in ("4-7", "4.7", "4-6", "4.6", "4-5", "4.5")):
            return "opus_new"
        return "opus_old"
    if "sonnet" in m:
        return "sonnet"
    if "haiku" in m:
        if "4-5" in m or "4.5" in m:
            return "haiku_45"
        if "3-5" in m or "3.5" in m:
            return "haiku_35"
        return "haiku_3"
    return None


def cost_for(tier: str, inp: int, out: int, c5w: int, c1w: int, cr: int) -> float:
    p = PRICING[tier]
    return (
        inp * p[0]
        + out * p[1]
        + c5w * p[2]
        + c1w * p[3]
        + cr * p[4]
    ) / 1_000_000


def cost_for_model(model: str, inp: int, out: int, c5w: int, c1w: int, cr: int) -> tuple[float, str | None]:
    tier = model_tier(model)
    if tier is None:
        return 0.0, None
    return cost_for(tier, inp, out, c5w, c1w, cr), tier
