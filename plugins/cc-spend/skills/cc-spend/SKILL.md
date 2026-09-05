---
name: cc-spend
description: Calculates what Claude Code usage would have cost at API token rates, for the current calendar month or a named one. Parses local session transcripts, deduplicates messages and applies current model pricing from LiteLLM. Use whenever the user asks about spend, cost, usage in dollars, token consumption over a period, how much a month of Claude Code came to, or wants to compare consumption against what they pay - even if they never say "cc-spend".
---

# cc-spend

Reports the API-equivalent cost of Claude Code usage. On a subscription this
money is never charged; the figure measures consumption.

## Run

Scripts live in `scripts/` next to this file. On Windows the interpreter is
`python`, not `python3`.

```bash
python scripts/cc_spend.py                      # current month, 1st to today
python scripts/cc_spend.py --month 2026-08      # a full named month
python scripts/cc_spend.py --format facts       # one row per session, for tips
python scripts/cc_spend.py --format json        # everything, machine readable
python scripts/cc_spend.py --format statusline --cache-ttl 600
python scripts/cc_spend.py --refresh-pricing    # force price re-download
```

## Report to the user

Paste the text output verbatim as plain markdown, never inside a code fence
(tables do not render there). It has a fixed shape:

1. Header: period and total.
2. Per-model table: Model, Cost, Share, Calls, Input, Output, Cache write,
   Cache read.
3. Cache analysis table: Sessions, Cache misses, Miss cost, Avg context,
   Max context.
4. WARNING lines, only when the script prints them.

Never rename, drop, add or reorder columns. Never paraphrase, round or
recompute a number. After the output: at most one sentence per WARNING, then
at most one tip.

- `no price for <model>` - the model is excluded from the total. Fix: add its
  rates to `~/.config/cc-spend/pricing_overrides.json` (see below).
- `deletes transcripts after N days` - the period reaches past Claude Code's
  `cleanupPeriodDays`, so the figure is incomplete. Fix for future months:
  raise `cleanupPeriodDays` in `~/.claude/settings.json`.

Terms, if the user asks:

- Session: every session with calls in the period, including ones resumed
  from earlier. A session starts with an empty cache.
- Context: prompt size of one call, input + cache read + cache write.
- Cache miss: a call after a break longer than the cache TTL. The whole context
  is written to the cache again, at the cache-write rate.
- Start context (facts only): the first call of a session. System prompt,
  CLAUDE.md, tool and skill definitions, plus the first user message.

## Tips

This is not a tutor. A tip is optional help; no tip is the normal outcome.
The bar is "obvious from the facts", not "plausible".

Run `--format facts`: one row per session, most expensive first, with the
transcript path. Look for a pattern that connects a fact to something the user
controls. The user controls only:

- continue an old session after a break, or start a new one
- `/clear` between tasks; split a long task into sessions
- size of CLAUDE.md, enabled MCP servers and skills
- model and effort per task
- how much text and log output they paste
- asking for subagents on search-heavy work
- how many sessions run in parallel

Everything else is the model's behaviour, not the user's: repeated file reads,
tool call count, reasoning length. Never make a tip out of it.

Before writing a tip, check the hypothesis in the session file named in the
row: grep for timestamps, `model`, `cache_creation`, `compact_boundary`. Do not
read the file whole; transcripts run to megabytes. Not confirmed, or not
checkable: no tip. A start context far above the other sessions usually means
the session was resumed from before the period; check before advising.

Shape: two sentences, 30 words at most. The first names the user's own number,
one number only, no lists. The second offers one option with "often cheaper"
or "can help". No imperatives, no verdict, no jargon such as "cache-write
rate". The evidence you checked stays out of the text.

> Tip: 4 cache misses in one Sep 2 session cost $8.99 of its $15.85. After a
> break over an hour, a fresh session is often cheaper.

## Scope and limits

- Claude Code CLI on this machine only. claude.ai web and desktop are not
  included. For several machines, run on each and add up.
- Dates are UTC calendar days and months. Late-night activity may land in the
  neighbouring day or month.
- Public rates; negotiated discounts are not reflected.

## Pricing overrides

LiteLLM lags new releases by days to weeks. Until then the script warns and
excludes the model. To count it, add the rates in USD per token (not per
million), keyed by the model string from the transcript:

```json
{
  "claude-opus-6-20261201": {
    "input_cost_per_token": 0.000005,
    "output_cost_per_token": 0.000025,
    "cache_read_input_token_cost": 0.0000005,
    "cache_creation_input_token_cost": 0.00000625
  }
}
```

Overrides win over LiteLLM. Prices are cached 24h in `~/.cache/cc-spend/`; a
stale cache is used offline.

## Status bar

The script re-reads every transcript; never call it from a widget without
`--cache-ttl`. Let a scheduled job (cron, Task Scheduler) refresh the cache with
`--cache-ttl 1` and let the widget read it with `--cache-ttl 3600`.
