"""Slice 3: redaction, forgetting, pause."""

import sqlite3
import time

import pytest

from deepmem import cli, store
from deepmem.store import INDEX, forget, redact, search

FRAMES = "CREATE TABLE frames (id INTEGER PRIMARY KEY, ts INTEGER NOT NULL, app TEXT NOT NULL, title TEXT NOT NULL, url TEXT, text TEXT NOT NULL)"
EVENTS = "CREATE TABLE events (id INTEGER PRIMARY KEY, ts INTEGER NOT NULL, app TEXT NOT NULL, title TEXT NOT NULL)"

# Fake values with the shape of real secrets.
SECRETS = [
    "sk-proj-abcdefghijklmnopqrstuvwxyz0123456789",
    "AKIAIOSFODNN7EXAMPLE",
    "ghp_abcdefghijklmnopqrstuvwxyz0123456789",
    "xoxb-123456789012-abcdefghijkl",
    "AIzaSyA1234567890abcdefghijklmnopqrstuvw",
    "sk_live_abcdefghijklmnop1234",
    "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjgNryP4J3jVmNHl0w5N",
    "4111 1111 1111 1111",
    "hunter2hunter2",
    "MIIEvQIBADANBgkqhkiG9w0BAQEFAASC",
]
SCREEN = f"""export OPENAI_API_KEY={SECRETS[0]}
aws_access_key_id = {SECRETS[1]}
git remote set-url origin https://{SECRETS[2]}@github.com/x/y
slack {SECRETS[3]} google {SECRETS[4]} stripe {SECRETS[5]}
Authorization: Bearer {SECRETS[6]}
card {SECRETS[7]}
password: {SECRETS[8]}
-----BEGIN RSA PRIVATE KEY-----
{SECRETS[9]}
-----END RSA PRIVATE KEY-----
the deploy checklist is done
"""


def test_redact_removes_every_secret_and_keeps_normal_text():
    out = redact(SCREEN)
    for secret in SECRETS:
        assert secret not in out
    assert "the deploy checklist is done" in out
    assert "password: [REDACTED:secret]" in out
    # Not secrets: ordinary numbers, short values, a number failing the card checksum.
    plain = "order 1234 5678 9012 3456 took 1700000000000 ms, token count: 42"
    assert redact(plain) == plain


def test_secrets_never_reach_the_index_or_results(tmp_path):
    db = tmp_path / "db.sqlite"
    con = sqlite3.connect(db)
    con.execute(FRAMES)
    con.execute(EVENTS)
    con.execute(
        "INSERT INTO frames (ts, app, title, url, text) VALUES (1, 'Terminal', ?, ?, ?)",
        (f"zsh token={SECRETS[2]}", f"https://x.test/cb?token={SECRETS[2]}", SCREEN),
    )
    con.execute("INSERT INTO events (ts, app, title) VALUES (1, 'Terminal', ?)", (f"export API_KEY={SECRETS[0]}",))
    con.commit()

    results = search("deploy checklist", db=db)

    assert len(results) == 1
    indexed = " ".join(str(v) for row in con.execute(f"SELECT * FROM {INDEX}") for v in row)
    returned = str(results) + str(store.recent_events(db))
    for secret in SECRETS:
        assert secret not in indexed
        assert secret not in returned
    assert search(SECRETS[1], db=db) == []


def _db_with_old_and_new_rows(tmp_path):
    db = tmp_path / "db.sqlite"
    now = int(time.time() * 1000)
    con = sqlite3.connect(db)
    con.execute(FRAMES)
    con.execute(EVENTS)
    for ts, word in ((now - 3 * 3600_000, "oldest"), (now - 60_000, "newest")):
        con.execute("INSERT INTO frames (ts, app, title, url, text) VALUES (?, 'A', ?, NULL, ?)", (ts, word, f"{word} text"))
        con.execute("INSERT INTO events (ts, app, title) VALUES (?, 'A', ?)", (ts, word))
    con.commit()
    return db, con


def test_forget_last_hour_removes_rows_and_index_entries(tmp_path):
    db, con = _db_with_old_and_new_rows(tmp_path)
    assert len(search("newest", db=db)) == 1  # indexed before forgetting

    assert forget(3600, db=db) == 2

    assert [r[0] for r in con.execute("SELECT title FROM frames")] == ["oldest"]
    assert [r[0] for r in con.execute("SELECT title FROM events")] == ["oldest"]
    assert search("newest", db=db) == []
    assert len(search("oldest", db=db)) == 1
    assert b"newest text" not in db.read_bytes()  # gone from the file, not just unlinked


def test_forget_all_and_missing_database(tmp_path):
    db, con = _db_with_old_and_new_rows(tmp_path)
    assert forget(None, db=db) == 4
    assert con.execute("SELECT count(*) FROM frames").fetchone()[0] == 0
    assert forget(None, db=tmp_path / "absent.sqlite") == 0


def test_cli_pause_resume_and_forget_confirmation(tmp_path, monkeypatch, capsys):
    db, con = _db_with_old_and_new_rows(tmp_path)
    monkeypatch.setattr(store, "HOME", tmp_path / "home")
    monkeypatch.setattr(store, "PAUSED_FLAG", tmp_path / "home" / "paused")
    monkeypatch.setattr(store, "DB", db)
    monkeypatch.setattr(store.forget, "__defaults__", (db,))

    assert cli.main(["pause"]) == 0 and store.PAUSED_FLAG.exists()
    assert cli.main(["resume"]) == 0 and not store.PAUSED_FLAG.exists()
    assert cli.main(["resume"]) == 0  # resuming twice is fine

    monkeypatch.setattr("builtins.input", lambda _: "n")
    assert cli.main(["forget", "--all"]) == 1
    assert con.execute("SELECT count(*) FROM frames").fetchone()[0] == 2
    assert cli.main(["forget", "--last", "1h", "--yes"]) == 0
    assert con.execute("SELECT count(*) FROM frames").fetchone()[0] == 1

    assert cli.parse_duration("30m") == 1800 and cli.parse_duration("2d") == 172800
    with pytest.raises(SystemExit):
        cli.main(["forget", "--last", "soon"])
