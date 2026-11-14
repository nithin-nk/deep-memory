"""Access to deep-memory's data: configuration, redaction, search indexes,
queries, and deletion.

deepmem-capture is the only process that inserts into the raw tables
(`events`, `frames`). This module reads them, owns the search index, and
deletes rows only when the user asks to forget.
"""

import json
import os
import re
import sqlite3
import time
import tomllib
from datetime import datetime, timezone
from pathlib import Path

from deepmem import projects

HOME = Path(os.environ.get("DEEPMEM_HOME", Path.home() / ".deepmem"))
DB = Path(os.environ.get("DEEPMEM_DB", HOME / "db.sqlite"))
PAUSED_FLAG = HOME / "paused"
CONFIG_FILE = HOME / "config.toml"

CONFIG_DEFAULTS = {
    "model": "qwen3-vl:8b-instruct",  # non-thinking build; any Ollama model; a multimodal one also serves Slice 7
    "ollama_url": "http://localhost:11434",
    "num_ctx": 16384,  # Ollama's own default is far smaller and silently truncates
    "session_gap_minutes": 5,  # idle time that ends a session
    "session_max_minutes": 30,
    "distill_on_battery": False,
    "share_personal": False,  # may agents inside a project also read `personal` memories?
    "outside_project": "none",  # what an agent outside any repository reads: none | personal | all
    "keep_screenshots": False,  # keep the JPEG (in frames/kept/) after the vision model described it
}


def load_config(path: Path | None = None) -> dict:
    """Defaults overlaid with `~/.deepmem/config.toml` (shared with the daemon,
    which reads `denylist` from it)."""
    path = path or CONFIG_FILE
    config = dict(CONFIG_DEFAULTS)
    if path.exists():
        with path.open("rb") as file:
            config.update({k: v for k, v in tomllib.load(file).items() if k in CONFIG_DEFAULTS})
    return config


# The index stores redacted text. Change the name whenever redaction rules
# change, so the old index is dropped and rebuilt from `frames`.
INDEX = "frames_fts_v3"  # v3 adds the project column
_OLD_INDEXES = ("frames_fts", "frames_fts_v2")
_LABEL_BUCKET_MS = 30 * 60 * 1000
# A frame whose screenshot the vision model has not described yet. Described
# frames have `image` NULL, or `kept/<file>` with `keep_screenshots`.
PENDING_SHOT = "image IS NOT NULL AND image NOT LIKE 'kept/%'"


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


def iso(ts_ms: int) -> str:
    return datetime.fromtimestamp(ts_ms / 1000, timezone.utc).astimezone().isoformat(timespec="seconds")


def known_projects(con: sqlite3.Connection) -> set[str]:
    return set(json.loads(get_meta(con, "projects") or "[]"))


def recent_events(db: Path = DB, limit: int = 20, visible: set[str] | None = None) -> list[dict]:
    """Newest-first window focus events, limited to the `visible` projects
    (None = all). Empty if capture has never run."""
    if not db.exists():
        return []
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        # Over-fetch: events of other projects are filtered out below.
        rows = con.execute(
            "SELECT ts, app, title FROM events ORDER BY id DESC LIMIT ?", (limit if visible is None else limit * 20,)
        ).fetchall()
        known, labelled = set(), {}
        if visible is not None:
            try:  # labels the frame index already worked out for these windows
                known = known_projects(con)
                labelled = {
                    (app, title): project
                    for app, title, project in con.execute(
                        f"SELECT app, title, project FROM {INDEX} ORDER BY rowid LIMIT -1 OFFSET "
                        f"max(0, (SELECT count(*) FROM {INDEX}) - 5000)"
                    )
                }
            except sqlite3.OperationalError:  # nothing indexed yet
                pass
    except sqlite3.OperationalError:  # file exists but capture has not created the table yet
        return []
    finally:
        con.close()

    def project_of(app: str, title: str) -> str:
        return labelled.get((app, redact(title))) or projects.of_window(title, None, "", set(known))

    events = [
        {"time": iso(ts), "app": app, "title": redact(title)}
        for ts, app, title in rows
        if visible is None or project_of(app, title) in visible
    ]
    return events[:limit]


def sync_index(con: sqlite3.Connection) -> None:
    """Copy frames not yet indexed into the FTS5 table, redacted and labelled
    with their project."""
    # ponytail: runs on every search; move to the distiller's timer if a
    # first search after a long gap becomes slow.
    for old in _OLD_INDEXES:
        con.execute(f"DROP TABLE IF EXISTS {old}")
    con.execute(
        f"CREATE VIRTUAL TABLE IF NOT EXISTS {INDEX} "
        "USING fts5(text, app, title, url UNINDEXED, ts UNINDEXED, project UNINDEXED)"
    )
    new_frames = con.execute(
        "SELECT id, text, app, title, url, ts FROM frames "
        f"WHERE id > (SELECT coalesce(max(rowid), 0) FROM {INDEX})"
    ).fetchall()
    if not new_frames:
        return
    # Label a window from all its captures in a half-hour bucket, not frame by
    # frame: one capture of a chat or terminal often shows no path at all.
    buckets: dict[tuple, list] = {}
    for frame in new_frames:
        _, text, app, title, url, ts = frame
        buckets.setdefault((app, title, ts // _LABEL_BUCKET_MS), []).append(frame)
    known = known_projects(con)
    labels = {}
    for _ in range(2):  # second pass: names learned late also label early windows
        for key, frames in buckets.items():
            url = next((f[4] for f in reversed(frames) if f[4]), None)
            labels[key] = projects.of_window(key[1], url, "\n".join(f[1] for f in frames), known)
    con.executemany(
        f"INSERT INTO {INDEX} (rowid, text, app, title, url, ts, project) VALUES (?, ?, ?, ?, ?, ?, ?)",
        [
            (id_, redact(text), app, redact(title), url and redact(url), ts, labels[(app, title, ts // _LABEL_BUCKET_MS)])
            for id_, text, app, title, url, ts in new_frames
        ],
    )
    set_meta(con, "projects", json.dumps(sorted(known)))
    con.commit()


def get_meta(con: sqlite3.Connection, key: str) -> str | None:
    con.execute("CREATE TABLE IF NOT EXISTS deepmem_meta (key TEXT PRIMARY KEY, value TEXT)")
    row = con.execute("SELECT value FROM deepmem_meta WHERE key = ?", (key,)).fetchone()
    return row[0] if row else None


def set_meta(con: sqlite3.Connection, key: str, value) -> None:
    con.execute("CREATE TABLE IF NOT EXISTS deepmem_meta (key TEXT PRIMARY KEY, value TEXT)")
    con.execute("INSERT OR REPLACE INTO deepmem_meta (key, value) VALUES (?, ?)", (key, str(value)))


def read_memory(path: Path) -> tuple[dict, str]:
    """(front matter, body) of a memory file. Front matter values are JSON."""
    text = path.read_text(encoding="utf-8")
    front: dict = {}
    if text.startswith("---\n") and "\n---\n" in text[4:]:
        header, text = text[4:].split("\n---\n", 1)
        for line in header.splitlines():
            key, _, value = line.partition(": ")
            try:
                front[key] = json.loads(value)
            except json.JSONDecodeError:
                front[key] = value  # hand-edited, not JSON: keep as text
    return front, text.strip()


def sync_memory_index(con: sqlite3.Connection, memories: Path) -> None:
    """Rebuild the memory index when any Markdown file was added, edited,
    moved or removed. The files are the source of truth; users may edit them.
    A memory's project is the directory it lies in."""
    # ponytail: full rebuild on any change; index per file if this gets slow
    # (thousands of files).
    files = sorted(memories.rglob("*.md")) if memories.exists() else []
    signature = "|".join(f"{f.relative_to(memories)}:{f.stat().st_mtime_ns}" for f in files)
    con.execute("DROP TABLE IF EXISTS memories_fts")  # pre-project index
    con.execute(
        "CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts_v2 "
        "USING fts5(body, path UNINDEXED, start UNINDEXED, project UNINDEXED)"
    )
    if get_meta(con, "memories_signature_v2") == signature:
        return
    con.execute("DELETE FROM memories_fts_v2")
    for file in files:
        front, body = read_memory(file)
        con.execute(
            "INSERT INTO memories_fts_v2 (body, path, start, project) VALUES (?, ?, ?, ?)",
            (redact(body), str(file), str(front.get("start", "")), file.relative_to(memories).parts[0]),
        )
    set_meta(con, "memories_signature_v2", signature)
    con.commit()


def _match_expression(query: str) -> str:
    """Turn free text into an FTS5 query: every word must appear. Quoting each
    word keeps punctuation in user input from being read as FTS5 syntax."""
    return " ".join('"' + word.replace('"', '""') + '"' for word in query.split())


def search(query: str, limit: int = 10, db: Path = DB, visible: set[str] | None = None) -> list[dict]:
    """Distilled memories first, then raw screen text (one result per window),
    limited to the `visible` projects (None = all)."""
    match = _match_expression(query)
    if not match or not db.exists() or visible == set():
        return []
    limit = max(1, min(limit, 50))
    scope, scope_args = "", ()
    if visible is not None:
        scope = f" AND project IN ({', '.join('?' * len(visible))})"
        scope_args = tuple(sorted(visible))
    con = sqlite3.connect(db, timeout=5)
    try:
        sync_memory_index(con, db.parent / "memories")
        memory_rows = con.execute(
            "SELECT start, path, project, snippet(memories_fts_v2, 0, '', '', ' … ', 60) "
            f"FROM memories_fts_v2 WHERE memories_fts_v2 MATCH ?{scope} ORDER BY rank LIMIT ?",
            (match, *scope_args, limit),
        ).fetchall()
        try:
            sync_index(con)
            frame_rows = con.execute(
                f"SELECT ts, app, title, url, project, snippet({INDEX}, 0, '', '', ' … ', 40) "
                f"FROM {INDEX} WHERE {INDEX} MATCH ?{scope} ORDER BY rank, ts DESC LIMIT ?",
                # The same window is captured many times; over-fetch, then keep one each.
                (match, *scope_args, limit * 10),
            ).fetchall()
        except sqlite3.OperationalError:  # capture has not created `frames` yet
            frame_rows = []
    finally:
        con.close()

    results = [
        {"kind": "memory", "project": project, "time": start, "snippet": snippet, "file": path}
        for start, path, project, snippet in memory_rows
    ]
    seen = set()
    for ts, app, title, url, project, snippet in frame_rows:
        if len(results) >= limit:
            break
        if (app, title) in seen:
            continue
        seen.add((app, title))
        result = {"kind": "screen", "project": project, "time": iso(ts), "app": app, "title": title, "snippet": snippet}
        if url:
            result["url"] = url
        results.append(result)
    return results


# --- forgetting -------------------------------------------------------------


def forget(seconds: float | None, db: Path = DB) -> int:
    """Delete captured data and the memories distilled from it, for the last
    `seconds` (None = everything). Returns the number of rows and files deleted."""
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
        # Memories distilled from that period go too.
        memories = db.parent / "memories"
        for file in memories.rglob("*.md") if memories.exists() else []:
            end = read_memory(file)[0].get("end")
            try:
                expired = seconds is None or datetime.fromisoformat(str(end)).timestamp() * 1000 >= cutoff
            except ValueError:
                expired = seconds is None  # no readable date: only `--all` removes it
            if expired:
                file.unlink()
                deleted += 1
        # Screenshots, described or not. By file time, so that one whose row
        # is already gone is removed too.
        shots = db.parent / "frames"
        for file in shots.rglob("*.jpg") if shots.exists() else []:
            if file.stat().st_mtime * 1000 >= cutoff:
                file.unlink()
                deleted += 1
        # An eval fixture is a frozen copy of captures and may cover the period.
        fixture = HOME / "eval" / "fixture.jsonl"
        if db == DB and fixture.exists():
            fixture.unlink()
            deleted += 1
            for file in fixture.with_suffix(".images").glob("*.jpg"):
                file.unlink()
                deleted += 1
        # Row ids restart after a full delete; keep the distiller's cursor valid.
        try:
            newest = con.execute("SELECT coalesce(max(id), 0) FROM frames").fetchone()[0]
            set_meta(con, "distilled_frame_id", min(int(get_meta(con, "distilled_frame_id") or 0), newest))
        except sqlite3.OperationalError:
            pass
        con.commit()
        con.execute("PRAGMA wal_checkpoint(TRUNCATE)")  # the WAL still held the old pages
    finally:
        con.close()
    return deleted


def stats(db: Path = DB) -> dict:
    """Row counts and last capture time, for `deepmem status`."""
    result = {"events": 0, "frames": 0, "last_capture": None, "screenshots_waiting": 0}
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
        try:
            result["screenshots_waiting"] = con.execute(f"SELECT count(*) FROM frames WHERE {PENDING_SHOT}").fetchone()[0]
        except sqlite3.OperationalError:  # database from before the screenshot fallback
            pass
    finally:
        con.close()
    if result["last_capture"]:
        result["last_capture"] = iso(result["last_capture"])
    return result
