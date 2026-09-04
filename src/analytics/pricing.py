"""What a model call cost.

Providers bill per token, at a rate that differs by model and differs again
between input and output tokens — output is typically three to five times the
price of input. Nothing on the platform records money, so without this the
whole spend side of the console is structurally zero: a `cost_usd` column that
every writer leaves NULL, summed into tiles that always read `$0.00`. A zero
that means "we never priced this" and a zero that means "this was free" look
identical in a report, which is the worst of both.

So: a table of published rates, applied at record time.

Two things follow from prices being external facts that change without warning:

* **Rates are overridable at runtime.** The `pricing.overrides` setting takes a
  model name to a `[input, output]` pair in dollars per million tokens, so an
  operator can correct a stale rate or price a newly added model from the
  console rather than waiting on a deploy.
* **An unknown model is priced NULL, never 0.** Reports count those separately
  and say so, because a spend figure that quietly omits half the traffic is
  worse than one that admits it is partial.

Rates below are the providers' published list prices as of February 2026, in
US dollars per million tokens. They are an estimate of list cost, not an
invoice: they ignore negotiated rates, batch discounts and cached-input
pricing.
"""

from __future__ import annotations

import logging
from typing import Dict, Optional, Tuple

logger = logging.getLogger(__name__)

#: The date the rates below were taken from the providers' pricing pages. Shown
#: in the console next to any spend figure, so nobody treats a year-old table
#: as an invoice.
PRICES_AS_OF = "2026-02-01"

#: model name -> (USD per 1M input tokens, USD per 1M output tokens).
#: Keys are matched case-insensitively, and with the provider prefix optional:
#: `openai/gpt-oss-20b` and `gpt-oss-20b` both hit the same row.
DEFAULT_PRICES: Dict[str, Tuple[float, float]] = {
    # --- Groq-hosted open models (what the platform actually runs on) ------
    "llama-3.1-8b-instant": (0.05, 0.08),
    "llama-3.3-70b-versatile": (0.59, 0.79),
    "llama-4-scout-17b-16e-instruct": (0.11, 0.34),
    "llama-4-maverick-17b-128e-instruct": (0.20, 0.60),
    "gpt-oss-20b": (0.10, 0.50),
    "gpt-oss-120b": (0.15, 0.75),
    "qwen3-32b": (0.29, 0.59),
    "kimi-k2-instruct": (1.00, 3.00),
    "deepseek-r1-distill-llama-70b": (0.75, 0.99),
    "gemma2-9b-it": (0.20, 0.20),
    "mixtral-8x7b-32768": (0.24, 0.24),
    # --- OpenAI, for the few places it is configured ----------------------
    "gpt-4o-mini": (0.15, 0.60),
    "gpt-4o": (2.50, 10.00),
    "text-embedding-3-small": (0.02, 0.0),
    "text-embedding-3-large": (0.13, 0.0),
    # --- Models we host ourselves -----------------------------------------
    # Not free in reality — they cost GPU time — but they cost no *tokens*,
    # and inventing a per-token rate for them would make the spend figure a
    # fiction. Zero here means "billed elsewhere", which is true.
    "bge-small-en-v1.5": (0.0, 0.0),
    "nomic-embed-text": (0.0, 0.0),
    "nomic-embed-text-v1.5": (0.0, 0.0),
}


def normalise(model: Optional[str]) -> Optional[str]:
    """The lookup key for a model name.

    Providers disagree about prefixes for the same weights: Groq serves
    `openai/gpt-oss-20b`, others call it `gpt-oss-20b`, and a deployment may
    pin `gpt-oss-20b:latest`. All three are one rate, so all three normalise to
    one key.
    """
    if not model:
        return None
    key = str(model).strip().lower()
    if not key:
        return None
    # Strip a provider prefix (`openai/`, `meta-llama/`, `moonshotai/`) and any
    # tag suffix (`:latest`, `@2026-01`).
    key = key.rsplit("/", 1)[-1]
    for separator in (":", "@"):
        key = key.split(separator, 1)[0]
    return key.strip() or None


def rate_for(model: Optional[str], overrides: Optional[Dict[str, object]] = None):
    """The (input, output) dollars-per-million rate, or None if unknown.

    An override wins over the built-in table, including an override that sets a
    model to zero — that is how an operator declares a model self-hosted.
    """
    key = normalise(model)
    if key is None:
        return None
    if overrides:
        for raw_name, raw_rate in overrides.items():
            if normalise(raw_name) != key:
                continue
            pair = _coerce_rate(raw_rate)
            if pair is not None:
                return pair
            # A malformed override falls through to the built-in rate rather
            # than pricing the model at nothing.
            logger.warning("analytics.pricing_override_invalid", extra={"model": raw_name})
            break
    return DEFAULT_PRICES.get(key)


def _coerce_rate(value: object) -> Optional[Tuple[float, float]]:
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        return None
    try:
        rate_in, rate_out = float(value[0]), float(value[1])
    except (TypeError, ValueError):
        return None
    if rate_in < 0 or rate_out < 0:
        return None
    return rate_in, rate_out


def estimate_cost(
    *,
    model: Optional[str],
    input_tokens: Optional[int] = None,
    output_tokens: Optional[int] = None,
    total_tokens: Optional[int] = None,
    overrides: Optional[Dict[str, object]] = None,
) -> Optional[float]:
    """Dollars for one call, or None when the model has no known rate.

    When only a combined token count is available — several callers report just
    a total — the input rate is applied to the lot. That undercounts, because
    output tokens cost more, and undercounting knowably is better than
    inventing an input/output split that was never measured. The fix is for the
    caller to report both counts, not for this function to guess them.
    """
    rate = rate_for(model, overrides)
    if rate is None:
        return None
    rate_in, rate_out = rate
    if input_tokens is None and output_tokens is None:
        if not total_tokens:
            return 0.0
        return round((int(total_tokens) / 1_000_000.0) * rate_in, 6)
    cost = ((input_tokens or 0) / 1_000_000.0) * rate_in
    cost += ((output_tokens or 0) / 1_000_000.0) * rate_out
    return round(cost, 6)
