"""Access to the deep-memory SQLite file: redaction, search index, queries,
and deletion.

deepmem-capture is the only process that inserts into the raw tables
(`events`, `frames`). This module reads them, owns the search index, and
deletes rows only when the user asks to forget.
"""

import os
import re
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path

HOME = Path(os.environ.get("DEEPMEM_HOME", Path.home() / ".deepmem"))
DB = Path(os.environ.get("DEEPMEM_DB", HOME / "db.sqlite"))
PAUSED_FLAG = HOME / "paused"

# The index stores redacted text. Change the name whenever redaction rules
# change, so the old index is dropped and rebuilt from `frames`.
INDEX = "frames_fts_v2"
_OLD_INDEXES = ("frames_fts",)


# --- redaction --------------------------------------------------------------

_SECRET_PATTERNS = [
    ("private-key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?(?:-----END [A-Z ]*PRIVATE KEY-----|\Z)", re.S)),
    ("aws-key", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("github-token", re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{36,}|github_pat_[A-Za-z0-9_]{22,})\b")),
    ("api-key", re.compile(r"\bsk-[A-Za-z0-9_-]{20,}")),
    ("api-key", re.compile(r"\b[sr]k_(?:live|test)_[0-9A-Za-z]{16,}\b")),
    ("api-key", re.compile(r"\bAIza[0-9A-Za-z_-]{35,}")),
    ("slack-token", re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}")),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}")),
    ("token", re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/-]{20,}=*")),
]
# `password = hunter2hunter2`, `API_KEY: "..."`, `?token=...`: keep the name, drop the value.
_ASSIGNMENT = re.compile(
    r"""(?ix) \b ( [a-z0-9_-]* (?: api[_-]?key | secret | token | passw(?:or)?d | pwd ) [a-z0-9_-]* )
        ( \s* [:=] \s* ['"]? ) ( [^\s'"&]{8,} )"""
)
_CARD = re.compile(r"\b\d(?:[ -]?\d){12,18}\b")


def _luhn_ok(digits: str) -> bool:
    total = 0
    for i, ch in enumerate(reversed(digits)):
        n = int(ch)
        if i % 2:
            n = n * 2 - 9 if n > 4 else n * 2
        total += n
    return total % 10 == 0


def redact(text: str) -> str:
    """Replace secrets (keys, tokens, passwords, card numbers) with
    `[REDACTED:kind]`. Applied before captured text is indexed, returned to an
    agent, or sent to a model."""
    # ponytail: pattern list only catches known shapes; a random-looking
    # secret with no label passes. Add entropy detection if leaks show up.
    for kind, pattern in _SECRET_PATTERNS:
        text = pattern.sub(f"[REDACTED:{kind}]", text)
    text = _ASSIGNMENT.sub(
        lambda m: m.group(0) if m.group(3).startswith("[REDACTED") else f"{m.group(1)}{m.group(2)}[REDACTED:secret]",
        text,
    )
    return _CARD.sub(
        lambda m: "[REDACTED:card]" if _luhn_ok(re.sub(r"\D", "", m.group(0))) else m.group(0), text
    )


# --- reading ----------------------------------------------------------------


def _iso(ts_ms: int) -> str:
    return datetime.fromtimestamp(ts_ms / 1000, timezone.utc).astimezone().isoformat(timespec="seconds")


def recent_events(db: Path = DB, limit: int = 20) -> list[dict]:
    """Newest-first window focus events. Empty if capture has never run."""
    if not db.exists():
        return []
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        rows = con.execute(
            "SELECT ts, app, title FROM events ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
    except sqlite3.OperationalError:  # file exists but capture has not created the table yet
        return []
    finally:
        con.close()
    return [{"time": _iso(ts), "app": app, "title": redact(title)} for ts, app, title in rows]


def sync_index(con: sqlite3.Connection) -> None:
    """Copy frames not yet indexed into the FTS5 table, redacted."""
    # ponytail: runs on every search; move to the distiller's timer (Slice 4)
    # if a first search after a long gap becomes slow.
    for old in _OLD_INDEXES:
        con.execute(f"DROP TABLE IF EXISTS {old}")
    con.execute(
        f"CREATE VIRTUAL TABLE IF NOT EXISTS {INDEX} "
        "USING fts5(text, app, title, url UNINDEXED, ts UNINDEXED)"
    )
    new_frames = con.execute(
        "SELECT id, text, app, title, url, ts FROM frames "
        f"WHERE id > (SELECT coalesce(max(rowid), 0) FROM {INDEX})"
    ).fetchall()
    con.executemany(
        f"INSERT INTO {INDEX} (rowid, text, app, title, url, ts) VALUES (?, ?, ?, ?, ?, ?)",
        [(id_, redact(text), app, redact(title), url and redact(url), ts) for id_, text, app, title, url, ts in new_frames],
    )
    con.commit()


def _match_expression(query: str) -> str:
    """Turn free text into an FTS5 query: every word must appear. Quoting each
    word keeps punctuation in user input from being read as FTS5 syntax."""
    return " ".join('"' + word.replace('"', '""') + '"' for word in query.split())


def search(query: str, limit: int = 10, db: Path = DB) -> list[dict]:
    """Best-matching captured screen text, one result per window."""
    match = _match_expression(query)
    if not match or not db.exists():
        return []
    limit = max(1, min(limit, 50))
    con = sqlite3.connect(db, timeout=5)
    try:
        sync_index(con)
        rows = con.execute(
            f"SELECT ts, app, title, url, snippet({INDEX}, 0, '', '', ' … ', 40) "
            f"FROM {INDEX} WHERE {INDEX} MATCH ? ORDER BY rank, ts DESC LIMIT ?",
            # The same window is captured many times; over-fetch, then keep one each.
            (match, limit * 10),
        ).fetchall()
    except sqlite3.OperationalError:  # capture has not created `frames` yet
        return []
    finally:
        con.close()

    results, seen = [], set()
    for ts, app, title, url, snippet in rows:
        if (app, title) in seen:
            continue
        seen.add((app, title))
        result = {"time": _iso(ts), "app": app, "title": title, "snippet": snippet}
        if url:
            result["url"] = url
        results.append(result)
        if len(results) == limit:
            break
    return results


# --- forgetting -------------------------------------------------------------


def forget(seconds: float | None, db: Path = DB) -> int:
    """Delete captured data from the last `seconds` (None = everything).
    Returns the number of raw rows deleted."""
    if not db.exists():
        return 0
    cutoff = 0 if seconds is None else int((time.time() - seconds) * 1000)
    con = sqlite3.connect(db, timeout=5)
    try:
        con.execute("PRAGMA secure_delete = ON")  # overwrite freed pages with zeros
        deleted = 0
        for table in ("events", "frames"):
            try:
                deleted += con.execute(f"DELETE FROM {table} WHERE ts >= ?", (cutoff,)).rowcount
            except sqlite3.OperationalError:  # table not created yet
                pass
        # ponytail: drop the whole index instead of deleting matching entries;
        # it is rebuilt from the remaining frames on the next search. Delete
        # by rowid if rebuilds get slow.
        for index in (INDEX, *_OLD_INDEXES):
            con.execute(f"DROP TABLE IF EXISTS {index}")
        con.commit()
        con.execute("PRAGMA wal_checkpoint(TRUNCATE)")  # the WAL still held the old pages
    finally:
        con.close()
    return deleted


def stats(db: Path = DB) -> dict:
    """Row counts and last capture time, for `deepmem status`."""
    result = {"events": 0, "frames": 0, "last_capture": None}
    if not db.exists():
        return result
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        for table in ("events", "frames"):
            try:
                count, last = con.execute(f"SELECT count(*), max(ts) FROM {table}").fetchone()
            except sqlite3.OperationalError:
                continue
            result[table] = count
            if last and (result["last_capture"] is None or last > result["last_capture"]):
                result["last_capture"] = last
    finally:
        con.close()
    if result["last_capture"]:
        result["last_capture"] = _iso(result["last_capture"])
    return result
