"""The cost meter's own database: storage_dir/state/costs.sqlite.

Why a separate SQLite file: finished jobs are deleted after a day or so, and
their AI usage went with them. Here one row per cost event outlives the job.
It is kept apart from the job files and from the posted list, so nothing else
can break it and it can be deleted to start over.

Every call opens a short connection (WAL mode) and the first use of a file
goes through one lock, so the worker thread, the sweep thread and API
requests can all use it.
"""

from __future__ import annotations

import math
import sqlite3
import threading
from contextlib import contextmanager
from datetime import UTC, datetime, tzinfo
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from core.config import get_settings
from decide.pricing import PRICES

MAX_ROWS = 50

_SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    at TEXT NOT NULL,
    day TEXT NOT NULL,
    kind TEXT NOT NULL,
    key TEXT NOT NULL UNIQUE,
    job_id TEXT NOT NULL DEFAULT '',
    meeting TEXT NOT NULL DEFAULT '',
    provider TEXT NOT NULL DEFAULT '',
    model TEXT NOT NULL DEFAULT '',
    calls INTEGER NOT NULL DEFAULT 0,
    input_tokens INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    cache_read_tokens INTEGER NOT NULL DEFAULT 0,
    cache_write_tokens INTEGER NOT NULL DEFAULT 0,
    units REAL NOT NULL DEFAULT 0,
    seconds REAL NOT NULL DEFAULT 0,
    usd REAL,
    note TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS events_day ON events(day);
CREATE TABLE IF NOT EXISTS prices (
    model TEXT PRIMARY KEY,
    input REAL NOT NULL,
    output REAL NOT NULL,
    cache_read REAL NOT NULL,
    cache_write REAL NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS fixed_costs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    usd_per_month REAL NOT NULL,
    active INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value REAL NOT NULL
);
"""

_lock = threading.RLock()
_ready: set[Path] = set()  # database files whose tables exist (a test's storage folder is new every time)


def db_path() -> Path:
    return get_settings().storage_dir / "state" / "costs.sqlite"


def now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def local_zone() -> tzinfo:
    try:
        return ZoneInfo(get_settings().local_timezone)
    except (ZoneInfoNotFoundError, ValueError, KeyError):
        return UTC


def local_day(moment: datetime | str | None = None) -> str:
    """YYYY-MM-DD of this moment (an aware datetime or an ISO string; now when
    missing or unreadable) in the team's time zone."""
    if isinstance(moment, str):
        try:
            moment = datetime.fromisoformat(moment)
        except ValueError:
            moment = None
    if moment is None:
        moment = datetime.now(UTC)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return moment.astimezone(local_zone()).date().isoformat()


@contextmanager
def connect():
    """A short connection with the tables in place (and the defaults seeded the
    first time the file is used). Commits on success, rolls back on error."""
    path = db_path()
    with _lock:
        if path not in _ready:
            path.parent.mkdir(parents=True, exist_ok=True)
            con = sqlite3.connect(path, timeout=30)
            try:
                con.execute("PRAGMA journal_mode=WAL")
                con.executescript(_SCHEMA)
                _seed(con)
                con.commit()
            finally:
                con.close()
            _ready.add(path)
    con = sqlite3.connect(path, timeout=30)
    con.row_factory = sqlite3.Row
    try:
        con.execute("PRAGMA busy_timeout=30000")
        yield con
        con.commit()
    except BaseException:
        con.rollback()
        raise
    finally:
        con.close()


def _seed(con: sqlite3.Connection) -> None:
    """Once per database (PRAGMA user_version): the built-in model prices. Seeding once, not on every start, lets the owner
    delete a row for good."""
    if con.execute("PRAGMA user_version").fetchone()[0] >= 1:
        return
    stamp = now_iso()
    for model, (inp, out, read, write) in PRICES.items():
        con.execute("INSERT OR IGNORE INTO prices VALUES (?,?,?,?,?,?)", (model, inp, out, read, write, stamp))
    con.execute("PRAGMA user_version=1")


# ------------------------------------------------------------------ prices


def load_prices(con: sqlite3.Connection) -> dict[str, tuple[float, float, float, float]]:
    return {r["model"]: (r["input"], r["output"], r["cache_read"], r["cache_write"])
            for r in con.execute("SELECT * FROM prices")}


def price_for(prices: dict[str, tuple], model: str) -> tuple | None:
    """The price row whose model id is the longest start of `model`."""
    best = None
    for prefix in prices:
        if prefix and (model or "").startswith(prefix) and (best is None or len(prefix) > len(best)):
            best = prefix
    return prices[best] if best else None


def usd_for(prices: dict[str, tuple], model: str, *, calls: int, input_tokens: int, output_tokens: int,
            cache_read: int, cache_write: int) -> float | None:
    """Dollars for these tokens (input_tokens includes the cached and
    cache-written part, which is priced on its own); None without a price."""
    price = price_for(prices, model)
    if price is None or not calls:
        return None
    inp, out, c_read, c_write = price
    plain = max(0, input_tokens - cache_read - cache_write)
    return round((plain * inp + cache_read * c_read + cache_write * c_write + output_tokens * out) / 1e6, 6)


def reprice_ai_events(con: sqlite3.Connection) -> None:
    """Work every AI event's dollars out again from its tokens, so a price the
    owner fixes or adds also fixes the past."""
    prices = load_prices(con)
    rows = con.execute("SELECT id, model, calls, input_tokens, output_tokens, cache_read_tokens, "
                       "cache_write_tokens FROM events WHERE kind='ai'").fetchall()
    for r in rows:
        usd = usd_for(prices, r["model"], calls=r["calls"], input_tokens=r["input_tokens"],
                      output_tokens=r["output_tokens"], cache_read=r["cache_read_tokens"],
                      cache_write=r["cache_write_tokens"])
        con.execute("UPDATE events SET usd=? WHERE id=?", (usd, r["id"]))


# ------------------------------------------------------------------ events


def upsert_event(con: sqlite3.Connection, *, kind: str, key: str, at: str, day: str, job_id: str = "",
                 meeting: str = "", provider: str = "", model: str = "", calls: int = 0, input_tokens: int = 0,
                 output_tokens: int = 0, cache_read_tokens: int = 0, cache_write_tokens: int = 0,
                 units: float = 0.0, seconds: float = 0.0, usd: float | None = None, note: str = "",
                 grows_by: str | None = None) -> None:
    """Add the event, or update the one with this key. When it happened (and
    so which day) stays as first recorded; the amounts follow the latest
    numbers, so recording a job again never counts it twice.

    `grows_by` ("calls" or "seconds") names the number that only ever grows
    for this event (a job's usage is added to by every later step): a row is
    replaced only by one with at least as much of it, so an older snapshot
    recorded after a newer one cannot lower it."""
    keep_larger = f" WHERE excluded.{grows_by} >= events.{grows_by}" if grows_by in ("calls", "seconds") else ""
    con.execute(
        """INSERT INTO events (at, day, kind, key, job_id, meeting, provider, model, calls, input_tokens,
               output_tokens, cache_read_tokens, cache_write_tokens, units, seconds, usd, note)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(key) DO UPDATE SET job_id=excluded.job_id, meeting=excluded.meeting,
               provider=excluded.provider, model=excluded.model, calls=excluded.calls,
               input_tokens=excluded.input_tokens, output_tokens=excluded.output_tokens,
               cache_read_tokens=excluded.cache_read_tokens, cache_write_tokens=excluded.cache_write_tokens,
               units=excluded.units, seconds=excluded.seconds, usd=excluded.usd, note=excluded.note"""
        + keep_larger,
        (at, day, kind, key, job_id, meeting, provider, model, calls, input_tokens, output_tokens,
         cache_read_tokens, cache_write_tokens, units, seconds, usd, note))


def get_event(key: str) -> dict | None:
    with connect() as con:
        row = con.execute("SELECT * FROM events WHERE key=?", (key,)).fetchone()
        return dict(row) if row else None


def event_keys(prefix: str) -> set[str]:
    with connect() as con:
        return {r["key"] for r in con.execute("SELECT key FROM events WHERE key LIKE ?", (prefix + "%",))}


# ------------------------------------------------------------------ the editable tables


def read_config() -> dict:
    """What the side panel edits: the model prices."""
    with connect() as con:
        prices = [{"model": r["model"], "input": r["input"], "output": r["output"],
                   "cache_read": r["cache_read"], "cache_write": r["cache_write"], "updated_at": r["updated_at"]}
                  for r in con.execute("SELECT * FROM prices ORDER BY model")]
    return {"prices": prices}


def _number(value, what: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{what} must be a number.")  # noqa: TRY004 (the API answers ValueError with a 400)
    if not math.isfinite(value) or value < 0:
        raise ValueError(f"{what} can't be negative.")
    return float(value)


def _name(value, what: str) -> str:
    text = value.strip() if isinstance(value, str) else ""
    if not 1 <= len(text) <= 100:
        raise ValueError(f"{what} needs a name of 1 to 100 characters.")
    return text


def write_config(*, prices: list | None = None) -> None:
    """Replace the prices (None leaves them alone). Raises ValueError with a
    plain message, and changes nothing, when anything is off."""
    clean_prices = None
    if prices is not None:
        if not isinstance(prices, list) or len(prices) > MAX_ROWS:
            raise ValueError(f"Prices: at most {MAX_ROWS} models.")
        clean_prices, seen = [], set()
        for row in prices:
            if not isinstance(row, dict):
                raise ValueError("Each price needs a model and its four prices.")  # noqa: TRY004
            model = _name(row.get("model"), "A price")
            if model in seen:
                raise ValueError(f"The model {model} is listed twice.")
            seen.add(model)
            nums = tuple(_number(row.get(f), f"The {f.replace('_', ' ')} price of {model}")
                         for f in ("input", "output", "cache_read", "cache_write"))
            clean_prices.append((model, nums))
    stamp = now_iso()
    with connect() as con:
        if clean_prices is not None:
            old = {r["model"]: (tuple(r[f] for f in ("input", "output", "cache_read", "cache_write")), r["updated_at"])
                   for r in con.execute("SELECT * FROM prices")}
            con.execute("DELETE FROM prices")
            for model, nums in clean_prices:
                same = old.get(model)
                when = same[1] if same and same[0] == nums else stamp  # the date a price last changed
                con.execute("INSERT INTO prices VALUES (?,?,?,?,?,?)", (model, *nums, when))
            reprice_ai_events(con)
