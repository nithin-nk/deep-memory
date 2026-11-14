"""Slice 5: fixture export and scoring, with a stubbed model."""

import json
import sqlite3

from deepmem import cli, evaluate, store
from deepmem.evaluate import export_fixture, load_fixture, run, unsupported_entities

from test_distill import CONFIG, FRAMES, T0, MIN, frame, make_db

REPLY = json.dumps({
    "summary": "The user fixed the token expiry check in auth.rs.",
    "activities": ["Changed the comparison to <= in auth.rs"],
    "decisions": [],
    "open_items": [],
    "entities": ["auth.rs", "https://tokio.rs/", "branch release-9", "commit deadbeef"],
})
CASES = """
[[case]]
question = "what fixed the expiry bug"
query = "token expiry"
expect = "<="

[[case]]
query = "runtime configuration"
expect = ["option 3"]

[[case]]
query = "kubernetes"
expect = ["helm"]
"""


def fixture_and_cases(tmp_path):
    db, _ = make_db(tmp_path, [
        frame(1, 0),
        frame(2, 2, "Google Chrome", "Tokio docs", "\n".join(f"Runtime configuration option {n}" for n in range(20)), "https://tokio.rs/"),
        frame(3, 60, text="\n".join(f"second session line {n}" for n in range(20))),
    ])
    fixture = tmp_path / "fixture.jsonl"
    assert export_fixture(fixture, db=db) == 3
    cases = tmp_path / "cases.toml"
    cases.write_text(CASES)
    return db, fixture, cases


def test_fixture_round_trip_keeps_frames_and_redacts(tmp_path):
    db, con = make_db(tmp_path, [frame(1, 0, text="password: hunter2hunter2\nvisible line")])
    path = tmp_path / "f.jsonl"
    export_fixture(path, db=db)
    [loaded] = load_fixture(path)
    assert loaded.id == 1 and loaded.app == "Code" and "visible line" in loaded.text
    assert "hunter2hunter2" not in path.read_text()


def test_run_scores_cases_and_flags_invented_entities(tmp_path):
    db, fixture, cases = fixture_and_cases(tmp_path)
    before = sorted(p.name for p in tmp_path.iterdir())

    report = run(fixture, cases, {**CONFIG, "model": "stub-model"}, model=lambda prompt, config: REPLY)

    assert (report["model"], report["frames"], report["sessions"], report["episodes"], report["dropped"]) == ("stub-model", 3, 2, 2, 0)
    by_query = {c["query"]: c for c in report["cases"]}
    assert by_query["token expiry"]["outcome"] == "memory" and by_query["token expiry"]["in_memories"]
    assert by_query["runtime configuration"]["outcome"] == "screen" and not by_query["runtime configuration"]["in_memories"]
    assert by_query["kubernetes"]["outcome"] == "miss"
    # Session 1 shows auth.rs and tokio.rs; session 2 shows only auth.rs. The
    # branch and the commit appear in no capture.
    assert report["entities"] == 8
    assert sorted(report["entities_unsupported"]) == [
        "branch release-9", "branch release-9", "commit deadbeef", "commit deadbeef", "https://tokio.rs/",
    ]
    assert sorted(p.name for p in tmp_path.iterdir()) == before  # worked in a temp dir only

    text = evaluate.format_report(report)
    assert "answered from memories     1/3" in text and "answered incl. screen text 2/3" in text


def test_unsupported_entities_checks_concrete_tokens():
    source = "Edited src/auth.rs and pushed commit 9bfc2dd to github.com/x/deep-memory on Google Chrome"
    assert unsupported_entities(["File: src/auth.rs", "commit 9bfc2dd", "Google Chrome", "GitHub repository: deep-memory"], source) == []
    assert unsupported_entities(["GitHub branch: slice-4", "commit 1234abc", "Firefox browser"], source) == \
        ["GitHub branch: slice-4", "commit 1234abc", "Firefox browser"]


def test_cli_eval_reports_missing_files(tmp_path, capsys):
    assert cli.main(["eval", "--fixture", str(tmp_path / "none.jsonl"), "--cases", str(tmp_path / "none.toml")]) == 1
    assert "not found" in capsys.readouterr().err
