"""
Pricing layer. The source is LiteLLM's model_prices_and_context_window.json.

Resolution order:
    fresh cache (24h) -> live fetch -> stale cache

A stale cache is served indefinitely, so once the table has been fetched even
once, losing the network never blocks a calculation. The level actually used is
reported to the caller so the output can state which data produced the numbers.
"""

from __future__ import annotations

import hashlib
import json
import time
import urllib.error
import urllib.request
from pathlib import Path

LITELLM_URL = (
    "https://raw.githubusercontent.com/BerriAI/litellm/main/"
    "model_prices_and_context_window.json"
)

CACHE = Path.home() / ".cache" / "cc-spend" / "litellm.json"
OVERRIDES = Path.home() / ".config" / "cc-spend" / "pricing_overrides.json"

CACHE_TTL = 24 * 3600
FETCH_TIMEOUT = 10

# Anthropic's long-context threshold. A request whose total input exceeds this
# is billed entirely at the premium *_above_200k rates.
TIER_THRESHOLD = 200_000

# Sanity anchors for downloaded data. If a price has drifted more than 3x from
# the expected value the payload is treated as broken and the previous level of
# the chain is used instead. Silently substituting garbage is worse than not
# updating at all: a corrupted chart still looks plausible.
ANCHORS = {
    "claude-opus-5": 5e-06,
    "claude-sonnet-5": 2e-06,
    "claude-haiku-4-5": 1e-06,
}
MIN_ENTRIES = 10


class PricingError(RuntimeError):
    pass


def fingerprint(table: dict) -> str:
    """
    Short content hash over the price fields only, printed in the report. Two
    runs that disagree can then be told apart: an identical fingerprint means
    the difference came from the transcripts, not from a price change.

    Non-cost metadata is excluded on purpose, so a cached table and a freshly
    downloaded one hash identically when the actual rates match.
    """
    prices = {
        model: {k: v for k, v in entry.items() if "cost" in k}
        for model, entry in table.items()
    }
    blob = json.dumps(prices, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode()).hexdigest()[:7]


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------

def _anthropic_only(raw: dict) -> dict:
    """
    Claude Code only ever calls Anthropic models; the other ~3500 entries are
    ballast. Filtering here keeps the cache at kilobytes.
    """
    return {
        k: v for k, v in raw.items()
        if isinstance(v, dict)
        and v.get("litellm_provider") == "anthropic"
        and "input_cost_per_token" in v
    }


def _valid(table: dict | None) -> bool:
    if not table or len(table) < MIN_ENTRIES:
        return False
    for model, expected in ANCHORS.items():
        got = table.get(model, {}).get("input_cost_per_token")
        if got is None:
            continue  # an older table may predate the model; not a failure
        if not (expected / 3 <= got <= expected * 3):
            return False
    return True


def _read(path: Path) -> dict | None:
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, json.JSONDecodeError):
        return None


def _fetch() -> dict | None:
    try:
        req = urllib.request.Request(
            LITELLM_URL, headers={"User-Agent": "cc-spend/1.0"})
        with urllib.request.urlopen(req, timeout=FETCH_TIMEOUT) as resp:
            return _anthropic_only(json.loads(resp.read().decode("utf-8")))
    except (urllib.error.URLError, OSError, json.JSONDecodeError, ValueError):
        return None


def _cache_age() -> float | None:
    try:
        return time.time() - CACHE.stat().st_mtime
    except OSError:
        return None


def _write_cache(table: dict) -> None:
    try:
        CACHE.parent.mkdir(parents=True, exist_ok=True)
        tmp = CACHE.with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(table, fh)
        tmp.replace(CACHE)
    except OSError:
        pass


def load(refresh: bool = False) -> tuple[dict, str]:
    """Return (table, source_label)."""
    age = _cache_age()
    if not refresh and age is not None and age < CACHE_TTL:
        table = _read(CACHE)
        if _valid(table):
            return table, "cache"

    fetched = _fetch()
    if _valid(fetched):
        _write_cache(fetched)
        return fetched, "LiteLLM"

    if age is not None:
        table = _read(CACHE)
        if _valid(table):
            return table, "stale cache"

    raise PricingError(
        f"could not reach {LITELLM_URL} and no usable cache exists at {CACHE}. "
        "One successful fetch is enough; after that a stale cache keeps working "
        "offline.")


def load_overrides() -> dict:
    """Keys are lower-cased so overrides go through the same matcher as the
    LiteLLM table and win on merge."""
    return {k.lower(): v for k, v in (_read(OVERRIDES) or {}).items()}


# ---------------------------------------------------------------------------
# Model matching
# ---------------------------------------------------------------------------

def match(model: str, table: dict) -> dict | None:
    """
    Exact id -> provider prefix stripped -> date suffix stripped -> longest
    matching prefix. Prefixes are tried longest-first because 'claude-opus-4'
    must not capture 'claude-opus-4-5'; they are priced differently.

    Returning None on no match is deliberate: a warning beats silently
    borrowing a neighbouring model's price.
    """
    m = (model or "").strip().lower()
    if not m or m.startswith("<"):
        return None

    if m in table:
        return table[m]

    if "/" in m:  # anthropic/claude-opus-5, bedrock/..., etc.
        m = m.rsplit("/", 1)[-1]
        if m in table:
            return table[m]

    parts = m.split("-")
    if len(parts) > 1 and parts[-1].isdigit() and len(parts[-1]) == 8:
        undated = "-".join(parts[:-1])
        if undated in table:
            return table[undated]

    for key in sorted(table, key=len, reverse=True):
        if m.startswith(key):
            return table[key]
    return None


# ---------------------------------------------------------------------------
# Cost
# ---------------------------------------------------------------------------

def _rate(entry: dict, key: str, tiered: bool) -> float:
    if tiered:
        high = entry.get(f"{key}_above_200k_tokens")
        if high is not None:
            return float(high)
    return float(entry.get(key) or 0.0)


def cost_of(entry: dict, inp: int, out: int, cw5: int, cw1: int, cr: int) -> float:
    """
    Cost of a single request in USD. LiteLLM rates are per token, not per
    million.

    Tiering: when a request's total input exceeds 200k the whole request bills
    at premium rates. Models without *_above_200k keys bill at base rates.
    """
    tiered = (inp + cw5 + cw1 + cr) > TIER_THRESHOLD

    r_in = _rate(entry, "input_cost_per_token", tiered)
    r_out = _rate(entry, "output_cost_per_token", tiered)
    r_cr = _rate(entry, "cache_read_input_token_cost", tiered)
    r_cw5 = _rate(entry, "cache_creation_input_token_cost", tiered)

    # 1h cache writes have their own field; fall back to the 5m rate.
    tiered_1h = entry.get(
        "cache_creation_input_token_cost_above_1hr_above_200k_tokens")
    base_1h = entry.get("cache_creation_input_token_cost_above_1hr")
    if tiered and tiered_1h:
        r_cw1 = float(tiered_1h)
    elif base_1h:
        r_cw1 = float(base_1h)
    else:
        r_cw1 = r_cw5

    return inp * r_in + out * r_out + cr * r_cr + cw5 * r_cw5 + cw1 * r_cw1
