import sqlite3

from deepmem.store import recent_events

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


# --- recall ---------------------------------------------------------------

from deepmem.store import search  # noqa: E402

FRAMES = "CREATE TABLE frames (id INTEGER PRIMARY KEY, ts INTEGER NOT NULL, app TEXT NOT NULL, title TEXT NOT NULL, url TEXT, text TEXT NOT NULL)"


def _frames_db(tmp_path, rows):
    db = tmp_path / "db.sqlite"
    con = sqlite3.connect(db)
    con.execute(FRAMES)
    con.executemany("INSERT INTO frames (ts, app, title, url, text) VALUES (?, ?, ?, ?, ?)", rows)
    con.commit()
    return db, con


def test_search_finds_phrase_with_window_and_url(tmp_path):
    db, _ = _frames_db(tmp_path, [
        (1_700_000_000_000, "Google Chrome", "Tokio docs", "https://tokio.rs/", "The quick brown fox configures the tokio runtime"),
        (1_700_000_001_000, "Terminal", "zsh", None, "cargo build finished"),
    ])

    results = search("tokio runtime", db=db)

    assert len(results) == 1
    assert results[0]["app"] == "Google Chrome"
    assert results[0]["url"] == "https://tokio.rs/"
    assert "tokio runtime" in results[0]["snippet"]
    assert "url" not in search("cargo", db=db)[0]


def test_search_returns_one_result_per_window(tmp_path):
    db, _ = _frames_db(tmp_path, [
        (1_700_000_000_000 + i, "Notes", "Plan", None, f"memory distillation draft {i}") for i in range(30)
    ] + [(1_700_000_100_000, "Safari", "Blog", None, "distillation of memory")])

    results = search("distillation", db=db)

    assert sorted(r["app"] for r in results) == ["Notes", "Safari"]


def test_search_indexes_frames_added_later(tmp_path):
    db, con = _frames_db(tmp_path, [(1, "A", "a", None, "first frame")])
    assert search("second", db=db) == []
    con.execute("INSERT INTO frames (ts, app, title, url, text) VALUES (2, 'B', 'b', NULL, 'second frame')")
    con.commit()
    assert [r["app"] for r in search("second", db=db)] == ["B"]


def test_search_tolerates_punctuation_blank_query_and_missing_tables(tmp_path):
    db, _ = _frames_db(tmp_path, [(1, "A", "a", None, 'error: "no such file" (os error 2)')])
    assert len(search('"no such file" (os', db=db)) == 1
    assert search("AND OR NOT *", db=db) == []
    assert search("   ", db=db) == []
    assert search("x", db=tmp_path / "absent.sqlite") == []
    empty = tmp_path / "empty.sqlite"
    sqlite3.connect(empty).close()
    assert search("x", db=empty) == []
