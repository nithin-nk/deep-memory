import sqlite3

from deepmem.server import recent_events

# Same schema deepmem-capture creates (capture/src/main.rs).
SCHEMA = "CREATE TABLE events (id INTEGER PRIMARY KEY, ts INTEGER NOT NULL, app TEXT NOT NULL, title TEXT NOT NULL)"


def test_recent_events_newest_first_and_limited(tmp_path):
    db = tmp_path / "db.sqlite"
    con = sqlite3.connect(db)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute(SCHEMA)
    con.executemany(
        "INSERT INTO events (ts, app, title) VALUES (?, ?, ?)",
        [(1_700_000_000_000 + i, f"App{i}", f"Window {i}") for i in range(25)],
    )
    con.commit()

    events = recent_events(db)

    assert len(events) == 20
    assert events[0]["app"] == "App24"
    assert events[-1]["app"] == "App5"
    assert set(events[0]) == {"time", "app", "title"}
    con.close()


def test_missing_database_or_table_returns_empty(tmp_path):
    assert recent_events(tmp_path / "absent.sqlite") == []
    empty = tmp_path / "empty.sqlite"
    sqlite3.connect(empty).close()
    assert recent_events(empty) == []
