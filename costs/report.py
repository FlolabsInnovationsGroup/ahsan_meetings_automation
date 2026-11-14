"""The cost report for the last N days."""

from __future__ import annotations

from datetime import date, timedelta

from . import db
from .record import sync_uploads

DAYS_PER_MONTH = 30.4375  # fixed monthly costs are shared out per day on this


def report(days: int, today: date | None = None) -> dict:
    """Costs over the last `days` days (1..3650); today counts as day 1.

    Variable costs are AI and tools; fixed costs (the server) are pro-rated by
    days / 30.4375. The projection is a 30-day month at this range's daily
    variable average, plus the fixed monthly costs."""
    if not isinstance(days, int) or not 1 <= days <= 3650:
        raise ValueError("Days must be a whole number from 1 to 3650.")
    sync_uploads()
    end = today or date.fromisoformat(db.local_day())
    start = end - timedelta(days=days - 1)
    with db.connect() as con:
        events = [dict(r) for r in con.execute(
            "SELECT * FROM events WHERE day >= ? AND day <= ?", (start.isoformat(), end.isoformat()))]
        prices = db.load_prices(con)
        fixed_monthly = sum(r["usd_per_month"] for r in con.execute(
            "SELECT usd_per_month FROM fixed_costs WHERE active=1"))
        limits = {r["key"]: r["value"] for r in con.execute("SELECT * FROM settings")}
        first_day = con.execute("SELECT MIN(day) FROM events").fetchone()[0]

    per_day = {(start + timedelta(days=i)).isoformat(): {"day": (start + timedelta(days=i)).isoformat(),
                                                         "usd": 0.0, "uploads": 0} for i in range(days)}
    ai_usd = tools_usd = 0.0
    tokens = {"calls": 0, "input_tokens": 0, "output_tokens": 0, "cache_read_tokens": 0, "cache_write_tokens": 0}
    uploads = shorts = compute_s = 0
    by_model: dict[tuple[str, str], dict] = {}
    meetings: set[str] = set()
    unpriced: set[str] = set()

    for e in events:
        row = per_day[e["day"]]
        kind = e["kind"]
        if kind == "ai":
            usd = e["usd"]
            if usd is None:
                usd = db.usd_for(prices, e["model"], calls=e["calls"], input_tokens=e["input_tokens"],
                                 output_tokens=e["output_tokens"], cache_read=e["cache_read_tokens"],
                                 cache_write=e["cache_write_tokens"])
            ai_usd += usd or 0.0
            row["usd"] += usd or 0.0
            for k in tokens:
                tokens[k] += e[k]
            slot = by_model.setdefault((e["provider"], e["model"]), {
                "provider": e["provider"], "model": e["model"], "usd": 0.0, "calls": 0, "input_tokens": 0,
                "output_tokens": 0, "meetings": 0, "price_known": db.price_for(prices, e["model"]) is not None})
            slot["usd"] += usd or 0.0
            slot["calls"] += e["calls"]
            slot["input_tokens"] += e["input_tokens"]
            slot["output_tokens"] += e["output_tokens"]
            slot["meetings"] += 1
            if not slot["price_known"] and e["calls"]:
                unpriced.add(e["model"] or "(unknown model)")
            if e["job_id"]:
                meetings.add(e["job_id"])
        elif kind == "tool":
            tools_usd += e["usd"] or 0.0
            row["usd"] += e["usd"] or 0.0
        elif kind == "upload":
            uploads += 1
            shorts += 1 if e["note"] == "short" else 0
            row["uploads"] += 1
        elif kind == "compute":
            # time is counted, but a compute row alone doesn't make a meeting:
            # empty, failed and cancelled recordings have one too
            compute_s += e["seconds"]

    for row in per_day.values():
        row["usd"] = round(row["usd"], 4)
    variable = ai_usd + tools_usd
    fixed_share = fixed_monthly * days / DAYS_PER_MONTH
    # The meter only has data since it started; averaging over days before that
    # would make the pace look far too low, so average over the days with data.
    effective_days = days
    if first_day:
        effective_days = max(1, min(days, (end - max(start, date.fromisoformat(first_day))).days + 1))
    fixed_effective = fixed_monthly * effective_days / DAYS_PER_MONTH
    busiest = max(per_day.values(), key=lambda r: (r["uploads"], r["day"]))
    count = len(meetings)
    return {
        "days": days,
        "start": start.isoformat(),
        "end": end.isoformat(),
        "totals": {
            "ai_usd": round(ai_usd, 4),
            "tools_usd": round(tools_usd, 4),
            "fixed_usd": round(fixed_share, 4),
            "total_usd": round(variable + fixed_share, 4),
        },
        "ai": {k: v for k, v in tokens.items()},
        "uploads": {
            "count": uploads, "shorts": shorts, "full": uploads - shorts,
            "busiest_day": busiest["day"] if busiest["uploads"] else None,
            "busiest_day_uploads": busiest["uploads"],
            "limit_per_day": int(limits.get("youtube_uploads_per_day", 100)),
            "quota_units_per_day": int(limits.get("youtube_quota_units_per_day", 10000)),
        },
        "compute_hours": round(compute_s / 3600, 2),
        "meetings": {
            "count": count,
            "avg_usd": round(variable / count, 4) if count else None,
            "avg_usd_with_fixed": round((variable + fixed_effective) / count, 4) if count else None,
        },
        "by_model": sorted(({**m, "usd": round(m["usd"], 4)} for m in by_model.values()),
                           key=lambda m: -m["usd"]),
        "per_day": list(per_day.values()),
        "projection": {
            "based_on_days": effective_days,
            "month_usd": round(variable / effective_days * 30 + fixed_monthly, 2),
            "variable_usd": round(variable / effective_days * 30, 2),
            "fixed_usd": round(fixed_monthly, 2),
        },
        "unpriced_models": sorted(unpriced),
    }
