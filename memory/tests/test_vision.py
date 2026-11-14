"""Slice 7: screenshots described by a stubbed vision model."""

import json
import sqlite3
import time
from pathlib import Path

import pytest

from deepmem import cli, evaluate, store
from deepmem.distill import ModelUnavailable
from deepmem.vision import describe_pending

from test_distill import CONFIG, GOOD_REPLY, T0

FRAMES = (
    "CREATE TABLE frames (id INTEGER PRIMARY KEY, ts INTEGER NOT NULL, app TEXT NOT NULL, "
    "title TEXT NOT NULL, url TEXT, text TEXT NOT NULL, image TEXT)"
)
SECRET = (Path(__file__).parents[2] / "testdata" / "redaction_secrets.txt").read_text().splitlines()[0]
REPLY = json.dumps({
    "description": "A whiteboard diagram of the billing pipeline with three boxes.",
    "text": "ingest-gateway\nledger-writer\nPostgres 16",
})


def make_db(tmp_path, shots, ts=T0):
    """One frame per (app, short accessibility text) with a screenshot file."""
    db = tmp_path / "db.sqlite"
    (tmp_path / "frames").mkdir()
    con = sqlite3.connect(db)
    con.execute(FRAMES)
    for n, (app, text) in enumerate(shots, 1):
        (tmp_path / "frames" / f"{n}.jpg").write_bytes(b"jpeg bytes")
        con.execute(
            "INSERT INTO frames (id, ts, app, title, url, text, image) VALUES (?, ?, ?, 'Board', NULL, ?, ?)",
            (n, ts + n * 1000, app, text, f"{n}.jpg"),
        )
    con.commit()
    return db, con


def test_screenshot_becomes_searchable_text_and_the_file_is_deleted(tmp_path):
    db, con = make_db(tmp_path, [("Excalidraw", "Zoom in\nZoom out")])
    assert store.search("ledger", db=db) == []  # indexed before it is described
    assert store.stats(db)["screenshots_waiting"] == 1
    seen = []

    def model(image, context, config):
        seen.append((image, context))
        return REPLY

    assert describe_pending(db, CONFIG, model) == 1
    assert seen == [(b"jpeg bytes", 'Screenshot of the window "Board" of the app "Excalidraw".')]
    text, image = con.execute("SELECT text, image FROM frames").fetchone()
    assert text.startswith("[screenshot] A whiteboard diagram") and "ledger-writer" in text
    assert text.endswith("Zoom in\nZoom out")  # accessibility text is kept
    assert image is None and not list((tmp_path / "frames").iterdir())
    [hit] = store.search("ledger writer", db=db)
    assert hit["kind"] == "screen" and hit["app"] == "Excalidraw"
    assert store.stats(db)["screenshots_waiting"] == 0
    assert describe_pending(db, CONFIG, model) == 0 and len(seen) == 1  # described once


def test_secret_in_a_screenshot_is_redacted_before_it_is_stored(tmp_path):
    db, con = make_db(tmp_path, [("Preview", "")])
    reply = json.dumps({"description": f"A slide showing the key {SECRET}.", "text": f"API key\n{SECRET}"})
    assert describe_pending(db, CONFIG, lambda *a: reply) == 1
    assert "[REDACTED" in con.execute("SELECT text FROM frames").fetchone()[0]
    store.search("slide", db=db)
    con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    assert SECRET.encode() not in db.read_bytes()


def test_keep_screenshots_moves_the_file_and_does_not_describe_it_again(tmp_path):
    db, con = make_db(tmp_path, [("Preview", "")])
    config = {**CONFIG, "keep_screenshots": True}
    assert describe_pending(db, config, lambda *a: REPLY) == 1
    assert con.execute("SELECT image FROM frames").fetchone()[0] == "kept/1.jpg"
    assert (tmp_path / "frames" / "kept" / "1.jpg").exists() and not (tmp_path / "frames" / "1.jpg").exists()
    assert describe_pending(db, config, lambda *a: pytest.fail("described twice")) == 0


def test_unusable_reply_is_retried_once_then_the_screenshot_is_dropped(tmp_path):
    db, con = make_db(tmp_path, [("Preview", "Close"), ("Preview", "")])
    replies = iter(["not json", json.dumps({"description": "", "text": "x"}), "{}", REPLY])
    logs = []
    assert describe_pending(db, CONFIG, lambda *a: next(replies), log=logs.append) == 1
    assert con.execute("SELECT text, image FROM frames ORDER BY id").fetchall()[0] == ("Close", None)
    assert "ledger-writer" in con.execute("SELECT text FROM frames WHERE id = 2").fetchone()[0]
    assert not list((tmp_path / "frames").iterdir())
    assert sum("unusable" in line for line in logs) == 3 and sum("dropped" in line for line in logs) == 1


def test_model_outage_leaves_screenshots_for_the_next_run(tmp_path):
    db, con = make_db(tmp_path, [("Preview", "")])

    def down(*args):
        raise ModelUnavailable("no server")

    with pytest.raises(ModelUnavailable):
        describe_pending(db, CONFIG, down)
    assert con.execute("SELECT image FROM frames").fetchone()[0] == "1.jpg"
    assert (tmp_path / "frames" / "1.jpg").exists()


def test_forget_deletes_screenshots_waiting_and_kept(tmp_path):
    db, con = make_db(tmp_path, [("Preview", ""), ("Preview", "")], ts=int(time.time() * 1000))
    assert describe_pending(db, {**CONFIG, "keep_screenshots": True}, lambda *a: REPLY) == 2
    (tmp_path / "frames" / "3.jpg").write_bytes(b"orphan, its row was never written")
    con.close()
    assert store.forget(3600, db=db) == 2 + 3
    assert not list((tmp_path / "frames").rglob("*.jpg"))


def test_database_from_before_screenshots_is_left_alone(tmp_path):
    db = tmp_path / "db.sqlite"
    con = sqlite3.connect(db)
    con.execute(FRAMES.replace(", image TEXT", ""))
    con.execute("INSERT INTO frames (ts, app, title, text) VALUES (1, 'Code', 'a.rs', 'fn main')")
    con.commit()
    assert describe_pending(db, CONFIG, lambda *a: pytest.fail("no screenshots")) == 0
    assert store.stats(db)["screenshots_waiting"] == 0


def test_eval_describes_fixture_screenshots_with_the_model_under_test(tmp_path):
    db, con = make_db(tmp_path, [("Excalidraw", ""), ("Excalidraw", "Zoom")])
    assert describe_pending(db, {**CONFIG, "keep_screenshots": True}, lambda *a: REPLY) == 1 + 1
    con.execute("UPDATE frames SET text = 'old description from another model' WHERE id = 1")
    con.commit()
    fixture = tmp_path / "out" / "fixture.jsonl"
    assert evaluate.export_fixture(fixture, db=db) == 2
    assert sorted(p.name for p in (tmp_path / "out" / "fixture.images").iterdir()) == ["1.jpg", "2.jpg"]
    cases = tmp_path / "cases.toml"
    cases.write_text('[[case]]\nquery = "ledger writer"\nexpect = "Postgres 16"\n\n[[case]]\nquery = "old description"\nexpect = "another"\n')

    report = evaluate.run(fixture, cases, CONFIG, model=lambda *a: GOOD_REPLY, vision_model=lambda *a: REPLY)
    assert (report["screenshots"], report["screenshots_described"]) == (2, 2)
    assert [c["outcome"] for c in report["cases"]] == ["screen", "miss"]
    assert "screenshots described      2/2" in evaluate.format_report(report)
    # The fixture and the real screenshots are untouched.
    assert len(list((tmp_path / "frames" / "kept").iterdir())) == 2


def test_distill_command_describes_screenshots_first(tmp_path, monkeypatch, capsys):
    db, con = make_db(tmp_path, [("Excalidraw", "")])
    monkeypatch.setattr(store, "DB", db)
    real = cli.vision.describe_pending
    monkeypatch.setattr(cli.vision, "describe_pending", lambda config: real(db, config, lambda *a: REPLY))
    monkeypatch.setattr(cli.distill, "run_once", lambda config: 0)
    assert cli.main(["distill", "--force"]) == 0
    assert "Described 1 screenshot(s)." in capsys.readouterr().out
    assert "ledger-writer" in con.execute("SELECT text FROM frames").fetchone()[0]
