"""The AI cost report for the last N days. Only AI token costs are counted."""

from __future__ import annotations

from datetime import date, timedelta

from . import db


def report(days: int, today: date | None = None) -> dict:
    """AI token costs over the last `days` days (1..3650); today counts as day 1.

    The projection is a 30-day month at this range's daily average, taken over
    the days that have data."""
    if not isinstance(days, int) or not 1 <= days <= 3650:
        raise ValueError("Days must be a whole number from 1 to 3650.")
    end = today or date.fromisoformat(db.local_day())
    start = end - timedelta(days=days - 1)
    with db.connect() as con:
        events = [dict(r) for r in con.execute(
            "SELECT * FROM events WHERE kind='ai' AND day >= ? AND day <= ?", (start.isoformat(), end.isoformat()))]
        prices = db.load_prices(con)
        first_day = con.execute("SELECT MIN(day) FROM events WHERE kind='ai'").fetchone()[0]

    per_day = {(start + timedelta(days=i)).isoformat(): {"day": (start + timedelta(days=i)).isoformat(),
                                                         "usd": 0.0, "calls": 0} for i in range(days)}
    ai_usd = 0.0
    tokens = {"calls": 0, "input_tokens": 0, "output_tokens": 0, "cache_read_tokens": 0, "cache_write_tokens": 0}
    by_model: dict[tuple[str, str], dict] = {}
    meetings: set[str] = set()
    unpriced: set[str] = set()

    for e in events:
        row = per_day[e["day"]]
        usd = e["usd"]
        if usd is None:
            usd = db.usd_for(prices, e["model"], calls=e["calls"], input_tokens=e["input_tokens"],
                             output_tokens=e["output_tokens"], cache_read=e["cache_read_tokens"],
                             cache_write=e["cache_write_tokens"])
        ai_usd += usd or 0.0
        row["usd"] += usd or 0.0
        row["calls"] += e["calls"]
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

    for row in per_day.values():
        row["usd"] = round(row["usd"], 4)
    # The meter only has data since it started; averaging over days before that
    # would make the pace look far too low, so average over the days with data.
    effective_days = days
    if first_day:
        effective_days = max(1, min(days, (end - max(start, date.fromisoformat(first_day))).days + 1))
    count = len(meetings)
    return {
        "range": {"start": start.isoformat(), "end": end.isoformat(), "days": days},
        "totals": {"ai_usd": round(ai_usd, 4), "total_usd": round(ai_usd, 4), **tokens},
        "per_day": list(per_day.values()),
        "by_model": sorted(({**m, "usd": round(m["usd"], 4)} for m in by_model.values()),
                           key=lambda m: -m["usd"]),
        "meetings": {"count": count, "avg_usd": round(ai_usd / count, 4) if count else None},
        "projection": {"based_on_days": effective_days, "month_usd": round(ai_usd / effective_days * 30, 2)},
        "unpriced_models": sorted(unpriced),
    }
