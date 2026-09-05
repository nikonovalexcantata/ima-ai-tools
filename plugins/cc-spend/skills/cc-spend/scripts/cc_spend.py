#!/usr/bin/env python3
"""
cc-spend - API-equivalent cost of Claude Code usage for a calendar month,
computed from local session transcripts. Standard library only.

All dates are UTC, matching the transcript timestamps, so no timezone
conversion happens and two machines in different zones agree on the numbers.

Output is deterministic: given the same transcripts and the same price table,
two runs on the same day produce identical bytes. Whole days are used rather
than fractional elapsed time, and the price table is identified by a content
hash instead of a cache age.
"""

from __future__ import annotations

import argparse
import calendar
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pricing

CACHE_DIR = Path.home() / ".cache" / "cc-spend"
DEFAULT_RETENTION_DAYS = 30  # Claude Code's cleanupPeriodDays default


# ---------------------------------------------------------------------------
# Period
# ---------------------------------------------------------------------------

def parse_month(raw: str) -> str:
    try:
        datetime.strptime(raw, "%Y-%m")
    except ValueError:
        raise argparse.ArgumentTypeError(f"expected YYYY-MM, got {raw!r}")
    return raw


def month_bounds(today: datetime, month: str | None) -> tuple[datetime, datetime, int, int]:
    """
    Return (start, end, days_counted, days_in_month) in UTC.

    Everything is UTC on purpose. Transcript timestamps are already UTC, so no
    conversion happens anywhere and there is no DST edge to get wrong. It also
    means two machines in different timezones agree on which day a request
    belongs to, which local time would not guarantee.

    The end is a midnight boundary, never "now". A partially elapsed day would
    make days_counted fractional and the output would change on every run even
    with no new activity.
    """
    year, mon = (int(x) for x in month.split("-")) if month else (today.year, today.month)

    start = datetime(year, mon, 1, tzinfo=timezone.utc)
    days_in_month = calendar.monthrange(year, mon)[1]

    if (year, mon) == (today.year, today.month):
        end = datetime(today.year, today.month, today.day,
                       tzinfo=timezone.utc) + timedelta(days=1)
        days_counted = today.day
    else:
        end = start + timedelta(days=days_in_month)
        days_counted = days_in_month

    return start, end, days_counted, days_in_month


# ---------------------------------------------------------------------------
# Transcripts
# ---------------------------------------------------------------------------

def config_dirs() -> list[Path]:
    """Where Claude Code keeps settings and sessions. CLAUDE_CONFIG_DIR wins."""
    env = os.environ.get("CLAUDE_CONFIG_DIR")
    if env:
        return [Path(p).expanduser() for p in env.split(os.pathsep)]
    return [Path.home() / ".claude", Path.home() / ".config" / "claude"]


def transcript_dirs() -> list[Path]:
    return [d / "projects" for d in config_dirs()]


def retention_days() -> int:
    """
    Claude Code deletes transcripts older than cleanupPeriodDays. A period that
    starts before that horizon is incomplete and the report must say so.
    """
    for d in config_dirs():
        try:
            with open(d / "settings.json", encoding="utf-8") as fh:
                value = json.load(fh).get("cleanupPeriodDays")
            if value is not None:
                return int(value)
        except (OSError, json.JSONDecodeError, TypeError, ValueError):
            continue
    return DEFAULT_RETENTION_DAYS


def parse_ts(raw: str) -> datetime | None:
    """ISO 8601 from the transcript -> UTC aware datetime."""
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def dedup_key(rec: dict, msg: dict) -> str | None:
    """
    The same assistant reply is physically written to several files after
    /resume, conversation branching and subagent runs. Without deduplication
    the total is inflated several-fold.
    """
    mid = msg.get("id")
    rid = rec.get("requestId") or rec.get("request_id")
    if mid and rid:
        return f"{mid}:{rid}"
    return mid or rec.get("uuid")


def split_cache_writes(usage: dict) -> tuple[int, int, int]:
    """Return (total, 5m, 1h). 1h writes cost 2x base input against 1.25x."""
    total = int(usage.get("cache_creation_input_tokens") or 0)
    detail = usage.get("cache_creation")
    if isinstance(detail, dict):
        cw5 = int(detail.get("ephemeral_5m_input_tokens") or 0)
        cw1 = int(detail.get("ephemeral_1h_input_tokens") or 0)
        if cw5 or cw1:
            return cw5 + cw1, cw5, cw1
    return total, total, 0


def session_of(path: Path) -> tuple[Path, bool]:
    """
    A session is <project>/<id>.jsonl; its subagents live in
    <project>/<id>/subagents/*.jsonl. Return (session file, is_subagent).
    """
    if path.parent.name == "subagents":
        sid_dir = path.parent.parent
        return sid_dir.with_suffix(".jsonl"), True
    return path, False


def new_session(path: Path) -> dict:
    return {
        "file": str(path),
        "started": None,
        "calls": 0,
        "subagent_calls": 0,
        "cost": 0.0,
        "models": set(),
        "start_context": 0,
        "max_context": 0,
        "context_sum": 0,
        "misses": 0,
        "miss_cost": 0.0,
        "compactions": 0,
    }


def collect(start: datetime, end: datetime, table: dict) -> dict:
    seen: set[str] = set()
    per_model: dict[str, dict] = {}
    sessions: dict[str, dict] = {}
    unknown: set[str] = set()
    files = counted = duplicates = 0
    searched: list[str] = []
    found: list[str] = []
    ttl_1h = False

    for base in transcript_dirs():
        searched.append(str(base))
        if not base.is_dir():
            continue
        found.append(str(base))

        for path in sorted(base.rglob("*.jsonl")):
            files += 1
            session_file, is_subagent = session_of(path)
            session = sessions.setdefault(str(session_file), new_session(session_file))
            last_ts: datetime | None = None
            try:
                fh = open(path, encoding="utf-8", errors="replace")
            except OSError:
                continue
            with fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if not isinstance(rec, dict):
                        continue

                    ts = parse_ts(rec.get("timestamp", ""))
                    if ts is None or not (start <= ts < end):
                        continue

                    if rec.get("subtype") == "compact_boundary" and not is_subagent:
                        session["compactions"] += 1
                        continue

                    msg = rec.get("message")
                    if not isinstance(msg, dict):
                        continue
                    usage = msg.get("usage")
                    if not isinstance(usage, dict):
                        continue

                    key = dedup_key(rec, msg)
                    if key:
                        if key in seen:
                            duplicates += 1
                            continue
                        seen.add(key)

                    model = msg.get("model") or rec.get("model") or ""
                    entry = pricing.match(model, table)
                    if entry is None:
                        if model and not model.strip().startswith("<"):
                            unknown.add(model)
                        continue

                    inp = int(usage.get("input_tokens") or 0)
                    out = int(usage.get("output_tokens") or 0)
                    cr = int(usage.get("cache_read_input_tokens") or 0)
                    cw_total, cw5, cw1 = split_cache_writes(usage)
                    cost = pricing.cost_of(entry, inp, out, cw5, cw1, cr)
                    context = inp + cr + cw_total
                    ttl_1h = ttl_1h or cw1 > 0

                    bucket = per_model.setdefault(
                        model,
                        {"cost": 0.0, "input": 0, "output": 0,
                         "cache_write": 0, "cache_read": 0, "messages": 0})
                    bucket["cost"] += cost
                    bucket["input"] += inp
                    bucket["output"] += out
                    bucket["cache_write"] += cw_total
                    bucket["cache_read"] += cr
                    bucket["messages"] += 1
                    counted += 1

                    session["cost"] += cost
                    session["models"].add(model)
                    if is_subagent:
                        session["subagent_calls"] += 1
                    else:
                        session["calls"] += 1
                        session["max_context"] = max(session["max_context"], context)
                        session["context_sum"] += context
                        if session["started"] is None:
                            # First call of a session: system prompt, CLAUDE.md,
                            # tool and skill definitions - before the user typed
                            # anything. This is the part the user controls.
                            session["started"] = ts
                            session["start_context"] = context
                        # A call after a break longer than the cache TTL writes
                        # the whole context again at the cache-write rate.
                        elif last_ts is not None and \
                                (ts - last_ts).total_seconds() > (3600 if ttl_1h else 300):
                            session["misses"] += 1
                            session["miss_cost"] += pricing.cost_of(entry, 0, 0, cw5, cw1, 0)
                    last_ts = ts

    live = [s for s in sessions.values() if s["calls"] or s["subagent_calls"]]
    return {
        "per_model": per_model,
        "sessions": live,
        "ttl_1h": ttl_1h,
        "unknown_models": sorted(unknown),
        "stats": {"files": files, "counted": counted, "duplicates": duplicates},
        "searched": searched,
        "found_dirs": found,
    }


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------

def build_report(data: dict, start: datetime, end: datetime, days_counted: int,
                 days_in_month: int, source: str, fp: str,
                 retention: int, today: datetime) -> dict:
    per_model = data["per_model"]
    total = sum(b["cost"] for b in per_model.values())

    # Ties are broken by model name / file so the ordering never flips.
    ordered = sorted(per_model.items(), key=lambda kv: (-kv[1]["cost"], kv[0]))
    sessions = sorted(data["sessions"], key=lambda s: (-s["cost"], s["file"]))
    main = [s for s in sessions if s["calls"]]
    main_calls = sum(s["calls"] for s in main)

    miss_cost = sum(s["miss_cost"] for s in main)
    cache = {
        "ttl_hours": 1 if data["ttl_1h"] else 5 / 60,
        "sessions": len(main),
        "cache_misses": sum(s["misses"] for s in main),
        "miss_cost_usd": round(miss_cost, 2),
        "miss_share": round(miss_cost / total, 3) if total else 0.0,
        "context_avg": sum(s["context_sum"] for s in main) // main_calls if main_calls else 0,
        "context_max": max((s["max_context"] for s in main), default=0),
        "compactions": sum(s["compactions"] for s in main),
        "sessions_with_subagents": sum(1 for s in main if s["subagent_calls"]),
    }

    return {
        "period": {
            "from": start.strftime("%Y-%m-%d"),
            "to": (end - timedelta(days=1)).strftime("%Y-%m-%d"),
            "days_counted": days_counted,
            "days_in_month": days_in_month,
        },
        "total_cost_usd": round(total, 2),
        "pricing": {"source": source, "fingerprint": fp},
        "retention": {
            "days": retention,
            "incomplete": start < today - timedelta(days=retention),
        },
        "models": {m: {**b, "cost": round(b["cost"], 2)} for m, b in ordered},
        "cache": cache,
        "sessions": [
            {
                **s,
                "started": s["started"].strftime("%Y-%m-%d %H:%M") if s["started"] else "",
                "cost": round(s["cost"], 2),
                "miss_cost": round(s["miss_cost"], 2),
                "models": sorted(s["models"]),
            }
            for s in sessions
        ],
        "unknown_models": data["unknown_models"],
        "stats": {**data["stats"], "main_calls": main_calls},
        "searched": data["searched"],
        "found_dirs": data["found_dirs"],
    }


def ttl_label(c: dict) -> str:
    return "1 hour" if c["ttl_hours"] == 1 else "5 minutes"


def human_tokens(n: int) -> str:
    for unit, div in (("B", 1_000_000_000), ("M", 1_000_000), ("k", 1_000)):
        if n >= div:
            return f"{n / div:.1f}{unit}"
    return str(n)


def render_text(r: dict) -> str:
    p = r["period"]
    out = [
        "Claude Code - API-equivalent spend",
        f"Period: {p['from']} to {p['to']} UTC ({p['days_counted']} of {p['days_in_month']} days)",
        f"Spent: ${r['total_cost_usd']:,.2f}",
    ]

    if r["models"]:
        total = r["total_cost_usd"] or 1
        out += [
            "",
            "| Model | Cost | Share | Calls | Input | Output | Cache write | Cache read |",
            "|---|---:|---:|---:|---:|---:|---:|---:|",
        ]
        for name, b in r["models"].items():
            out.append(
                f"| {name} | ${b['cost']:,.2f} | {100 * b['cost'] / total:.1f}% | "
                f"{b['messages']:,} | {human_tokens(b['input'])} | {human_tokens(b['output'])} | "
                f"{human_tokens(b['cache_write'])} | {human_tokens(b['cache_read'])} |"
            )
        c = r["cache"]
        out += [
            "",
            "Cache analysis",
            "",
            "| Sessions | Cache misses | Miss cost | Avg context | Max context |",
            "|---:|---:|---:|---:|---:|",
            f"| {c['sessions']} | {c['cache_misses']} | "
            f"${c['miss_cost_usd']:,.2f} ({100 * c['miss_share']:.0f}%) | "
            f"{human_tokens(c['context_avg'])} | {human_tokens(c['context_max'])} |",
        ]
    elif not r["found_dirs"]:
        out += [
            "",
            "No Claude Code transcript directory found. Looked in:",
            *[f"  {d}" for d in r["searched"]],
            "Set CLAUDE_CONFIG_DIR if Claude Code lives elsewhere.",
        ]
    else:
        out += ["", "Transcripts found, but no usage in this period."]

    warnings = []
    ret = r["retention"]
    if ret["incomplete"]:
        warnings.append(
            f"WARNING: Claude Code deletes transcripts after {ret['days']} days "
            "(cleanupPeriodDays); this period starts earlier and is incomplete."
        )
    if r["unknown_models"]:
        warnings.append(
            "WARNING: no price for " + ", ".join(r["unknown_models"])
            + " - excluded from the total. Add them to "
            "~/.config/cc-spend/pricing_overrides.json."
        )
    if warnings:
        out += ["", *warnings]  # a blank line ends the markdown table above
    return "\n".join(out)


def render_facts(r: dict) -> str:
    """
    One row per session, most expensive first, with the transcript path so a
    hypothesis can be checked in the source before it becomes a tip.
    """
    c = r["cache"]
    out = [
        f"Sessions {r['period']['from']} to {r['period']['to']} UTC, "
        f"cache TTL {ttl_label(c)}, ${r['total_cost_usd']:,.2f} total",
        "",
        "| Started (UTC) | Cost | Calls | Subagent calls | Start context | Max context | "
        "Cache misses | Miss cost | Compactions | Models | File |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---|---|",
    ]
    for s in r["sessions"]:
        out.append(
            f"| {s['started']} | ${s['cost']:,.2f} | {s['calls']} | {s['subagent_calls']} | "
            f"{human_tokens(s['start_context'])} | {human_tokens(s['max_context'])} | "
            f"{s['misses']} | ${s['miss_cost']:,.2f} | {s['compactions']} | "
            f"{', '.join(s['models'])} | {s['file']} |"
        )
    return "\n".join(out)


def render(report: dict, fmt: str) -> str:
    if fmt == "json":
        return json.dumps(report, indent=2, sort_keys=True)
    if fmt == "facts":
        return render_facts(report)
    if fmt == "statusline":
        return f"${report['total_cost_usd']:,.0f}"
    return render_text(report)


# ---------------------------------------------------------------------------
# Result cache (for statusline use)
# ---------------------------------------------------------------------------

def cache_path(month: str) -> Path:
    """Keyed by period: without this, --month 2026-07 would poison the cache
    that a plain run then reads back as the current month."""
    return CACHE_DIR / f"result-{month}.json"


def read_cache(path: Path, ttl: int) -> dict | None:
    if ttl <= 0:
        return None
    try:
        if time.time() - path.stat().st_mtime > ttl:
            return None
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, json.JSONDecodeError):
        return None


def write_cache(path: Path, report: dict) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(report, fh)
        tmp.replace(path)
    except OSError:
        pass


# ---------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(
        description="API-equivalent Claude Code spend for a calendar month.")
    ap.add_argument("--month", type=parse_month,
                    help="YYYY-MM; defaults to the current month")
    ap.add_argument("--format", choices=("text", "facts", "json", "statusline"),
                    default="text")
    ap.add_argument("--refresh-pricing", action="store_true",
                    help="force a re-download of the price table")
    ap.add_argument("--cache-ttl", type=int, default=0, metavar="SECONDS",
                    help="serve a cached result if it is younger than this")
    args = ap.parse_args()

    today = datetime.now(timezone.utc)
    start, end, days_counted, days_in_month = month_bounds(today, args.month)
    month_key = start.strftime("%Y-%m")
    path = cache_path(month_key)

    cached = None if args.refresh_pricing else read_cache(path, args.cache_ttl)
    if cached:
        print(render(cached, args.format))
        return 0

    try:
        table, source = pricing.load(refresh=args.refresh_pricing)
    except pricing.PricingError as exc:
        print(f"pricing error: {exc}", file=sys.stderr)
        return 1

    data = collect(start, end, {**table, **pricing.load_overrides()})
    report = build_report(data, start, end, days_counted, days_in_month,
                          source, pricing.fingerprint(table),
                          retention_days(), today)

    if args.cache_ttl:
        write_cache(path, report)

    print(render(report, args.format))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except BrokenPipeError:  # plain `| head`, not worth a traceback
        os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
        sys.exit(0)
    except KeyboardInterrupt:
        sys.exit(130)
