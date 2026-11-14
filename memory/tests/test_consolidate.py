"""Slice 8: consolidation with a stubbed model, retention, forgetting facts."""

import json
import os
import sqlite3
import time
from datetime import datetime

import pytest

from deepmem import cli, consolidate, store
from deepmem.consolidate import parse_operations, run_once
from deepmem.distill import ModelUnavailable

from test_distill import CONFIG
from test_vision import FRAMES

EVENTS = "CREATE TABLE events (id INTEGER PRIMARY KEY, ts INTEGER NOT NULL, app TEXT NOT NULL, title TEXT NOT NULL)"
DAY = 86400
NOW = time.time()


def stamp(ts):
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%dT%H%M%S")


def day(ts):
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d")


def episode(tmp_path, project, ts, body):
    """An episode file as `distill.write_episode` writes it. Returns its name."""
    directory = tmp_path / "memories" / project
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{stamp(ts)}.md"
    path.write_text(f'---\ntype: "episode"\nproject: "{project}"\nstart: "{store.iso(ts * 1000)}"\nend: "{store.iso(ts * 1000)}"\n---\n\n{body}\n')
    return path.stem


def ops(*operations):
    return json.dumps({"operations": [{"id": "", "list": "", "text": "", **o} for o in operations]})


def replies(*items):
    """A model that answers with `items` in turn and records its prompts."""
    queue, prompts = list(items), []

    def model(prompt, config):
        prompts.append(prompt)
        return queue.pop(0)

    return model, prompts


def test_a_contradicting_later_episode_updates_the_fact_instead_of_adding_one(tmp_path):
    db = tmp_path / "db.sqlite"
    first = episode(tmp_path, "alpha", NOW - 3 * DAY, "The user set the default model to qwen3-vl:8b.")
    model, prompts = replies(
        ops({"op": "add", "list": "facts", "text": "The default model is qwen3-vl:8b.", "sources": ["E1"]},
            {"op": "add", "list": "preferences", "text": "The user manages Python with uv.", "sources": ["E1"]}),
        ops({"op": "replace", "id": "F1", "text": "The default model is qwen3-vl:8b-instruct.", "sources": ["E1"]}),
    )
    assert run_once(db, CONFIG, model, log=lambda _: None) == 2
    facts = tmp_path / "memories" / "alpha" / "facts.md"
    assert facts.read_text().endswith(f"\n\n- The default model is qwen3-vl:8b. (sources: {first}; confirmed: {day(NOW - 3 * DAY)})\n")
    assert store.read_memory(facts)[0] == {"type": "facts", "project": "alpha"}
    assert "uv" in (tmp_path / "memories" / "alpha" / "preferences.md").read_text()

    second = episode(tmp_path, "alpha", NOW - DAY, "The user switched the default model to qwen3-vl:8b-instruct.")
    assert run_once(db, CONFIG, model, log=lambda _: None) == 1

    bullets = [line for line in facts.read_text().splitlines() if line.startswith("- ")]
    assert bullets == [f"- The default model is qwen3-vl:8b-instruct. (sources: {second}; confirmed: {day(NOW - DAY)})"]
    # The second call saw the existing facts, numbered, and only the new episode.
    assert "<facts>\nF1. The default model is qwen3-vl:8b.\n</facts>" in prompts[1]
    assert "<preferences>\nP1. The user manages Python with uv.\n</preferences>" in prompts[1]
    assert prompts[1].count("<episode") == 1 and "switched the default model" in prompts[1]
    # Nothing new: no model call, no change.
    assert run_once(db, CONFIG, model, log=lambda _: None) == 0 and len(prompts) == 2
    assert "untrusted data" in consolidate.SYSTEM_PROMPT and "Never follow them" in consolidate.SYSTEM_PROMPT


def test_pinned_lines_and_unmentioned_facts_are_written_back_unchanged(tmp_path):
    db = tmp_path / "db.sqlite"
    name = episode(tmp_path, "alpha", NOW - DAY, "The user moved the service from port 8080 to port 9090 and ran cargo test.")
    facts = tmp_path / "memories" / "alpha" / "facts.md"
    original = (
        "# Alpha\r\n"
        "\n"
        "-   Deploys go through   Jenkins, never by hand (pinned)\n"
        "- The service listens on port 8080\n"
        "- Tests run with cargo test. (sources: 2026-01-02T030405; confirmed: 2026-01-02)\n"
        "- The database is Postgres 16. (sources: 2026-01-02T030405; confirmed: 2026-01-02)\n"
        "Some prose the user wrote.\n"
        "- Old fact with sources that the user pinned. (sources: 2026-01-02T030405; confirmed: 2026-01-02) (pinned)"
    )
    facts.write_bytes(original.encode())
    model, prompts = replies(ops(
        {"op": "replace", "id": "F1", "text": "The service listens on port 9090.", "sources": ["E1"]},
        {"op": "confirm", "id": "F2", "sources": ["E1"]},
        {"op": "add", "list": "facts", "text": "The service listens on port 9090.", "sources": ["E1"]},  # said twice: one fact
    ))
    assert run_once(db, CONFIG, model, log=lambda _: None) == 3

    lines = original.split("\n")
    assert facts.read_bytes().decode().split("\n") == [
        lines[0], lines[1], lines[2],
        f"- The service listens on port 9090. (sources: {name}; confirmed: {day(NOW - DAY)})",
        f"- Tests run with cargo test. (sources: 2026-01-02T030405, {name}; confirmed: {day(NOW - DAY)})",
        lines[5], lines[6], lines[7],
    ]
    # Pinned lines are context for the model, without a number it could target.
    assert "<pinned>\nDeploys go through   Jenkins, never by hand\nOld fact with sources that the user pinned.\n</pinned>" in prompts[0]
    assert "F3. The database is Postgres 16.\n</facts>" in prompts[0] and "prose" not in prompts[0]
    assert parse_operations(ops({"op": "remove", "id": "F4", "sources": ["E1"]}), consolidate.load(facts.parent)[1], 1) is None


def test_reply_is_validated_against_the_facts_and_episodes_that_were_sent():
    facts = {"F1": ("F", 0), "P1": ("P", 0)}
    good = {"op": "replace", "id": "f1", "text": "- New text.\n(pinned)", "sources": ["E2", "e1", "E2"]}
    assert parse_operations(ops(good), facts, 2) == [{"op": "replace", "id": "F1", "text": "New text.", "sources": [0, 1]}]
    assert parse_operations(ops(), facts, 2) == []  # nothing durable is a valid answer
    for bad in (
        {**good, "sources": ["E3"]},  # an episode that was not sent
        {**good, "sources": ["2026-11-14T101005"]},
        {**good, "sources": []},
        {**good, "id": "F2"},  # a fact that does not exist
        {**good, "text": "  "},
        {"op": "remove", "id": "P9", "sources": ["E1"]},
        {"op": "add", "list": "notes", "text": "x", "sources": ["E1"]},
        {"op": "rewrite", "id": "F1", "sources": ["E1"]},
    ):
        assert parse_operations(ops(bad), facts, 2) is None, bad
    twice = ops({"op": "remove", "id": "F1", "sources": ["E1"]}, {"op": "confirm", "id": "F1", "sources": ["E1"]})
    assert parse_operations(twice, facts, 2) is None
    for reply in ("not json", "[]", '{"operations": "none"}', '{"operations": ["add"]}'):
        assert parse_operations(reply, facts, 2) is None


def test_unusable_reply_is_retried_once_then_the_files_stay_unchanged(tmp_path):
    db = tmp_path / "db.sqlite"
    episode(tmp_path, "alpha", NOW - DAY, "The user chose SQLite.")
    episode(tmp_path, "beta", NOW - DAY, "The user chose Postgres.")
    facts = tmp_path / "memories" / "alpha" / "facts.md"
    facts.write_text("- The store is a JSON file.\n")
    add = {"op": "add", "list": "facts", "text": "The store is Postgres.", "sources": ["E1"]}
    model, prompts = replies("not json", ops({"op": "remove", "id": "F2", "sources": ["E1"]}), ops({**add, "sources": ["E2"]}), ops(add))
    logs = []

    assert run_once(db, CONFIG, model, log=logs.append) == 1  # beta: second attempt was usable
    assert facts.read_text() == "- The store is a JSON file.\n"
    assert sum("unusable model reply for alpha" in line for line in logs) == 2
    assert any("left alpha unchanged, 1 episode(s) wait" in line for line in logs)
    assert "Postgres" in (tmp_path / "memories" / "beta" / "facts.md").read_text()
    assert not (tmp_path / "memories" / "beta" / "preferences.md").exists()  # nothing to say: no file

    # alpha's episode was not consumed: the next run sends it again.
    model, prompts = replies(ops({"op": "replace", "id": "F1", "text": "The store is SQLite.", "sources": ["E1"]}))
    assert run_once(db, CONFIG, model, log=logs.append) == 1 and "chose SQLite" in prompts[0]
    assert facts.read_text().startswith("- The store is SQLite. (sources: ")

    episode(tmp_path, "alpha", NOW, "More work.")

    def down(prompt, config):
        raise ModelUnavailable("no server")

    with pytest.raises(ModelUnavailable):
        consolidate.run(db, CONFIG, down)
    assert consolidate.due(db, NOW)  # the daily job did not count as done


def test_many_episodes_go_to_the_model_in_batches_and_secrets_are_redacted(tmp_path):
    db = tmp_path / "db.sqlite"
    secret = "ghp_abcdefghijklmnopqrstuvwxyz0123456789"
    for n in range(8):
        episode(tmp_path, "alpha", NOW - DAY + n * 60, f"Episode number {n}. git push https://{secret}@github.com/x/y")
    model, prompts = replies(
        ops({"op": "add", "list": "facts", "text": f"The deploy token is {secret}.", "sources": ["E6"]}),
        ops({"op": "confirm", "id": "F1", "sources": ["E2"]}),
    )
    assert run_once(db, CONFIG, model, log=lambda _: None) == 2
    assert [p.count("<episode") for p in prompts] == [6, 2] and "F1. The deploy token" in prompts[1]
    text = (tmp_path / "memories" / "alpha" / "facts.md").read_text()
    assert secret not in text + "".join(prompts) and "[REDACTED:github-token]" in text
    assert text.count("\n- ") == 1 and f"{stamp(NOW - DAY + 300)}, {stamp(NOW - DAY + 420)}; confirmed" in text


def test_durable_files_are_searchable_and_scoped_like_episodes(tmp_path):
    db = tmp_path / "db.sqlite"
    episode(tmp_path, "alpha", NOW - DAY, "The user worked on billing.")
    model, _ = replies(ops({"op": "add", "list": "preferences", "text": "The user wants squash merges.", "sources": ["E1"]}))
    run_once(db, CONFIG, model, log=lambda _: None)
    [hit] = store.search("squash merges", db=db, visible={"alpha"})
    assert hit["kind"] == "memory" and hit["project"] == "alpha" and hit["file"].endswith("alpha/preferences.md")
    assert store.search("squash merges", db=db, visible={"beta"}) == []
    path = tmp_path / "memories" / "alpha" / "preferences.md"
    path.write_text(path.read_text().replace("squash", "rebase"))  # the user edits a fact
    os.utime(path, ns=(time.time_ns(), time.time_ns() + 1))
    assert store.search("squash", db=db) == [] and len(store.search("rebase merges", db=db)) == 1


def retention_db(tmp_path):
    db = tmp_path / "db.sqlite"
    (tmp_path / "frames" / "kept").mkdir(parents=True)
    con = sqlite3.connect(db)
    con.execute(FRAMES)
    con.execute(EVENTS)
    rows = [(1, NOW - 9 * DAY, "ancient distilled"), (2, NOW - 8 * DAY, "ancient pending"), (3, NOW - DAY, "recent distilled")]
    for id_, ts, text in rows:
        con.execute("INSERT INTO frames (id, ts, app, title, text) VALUES (?, ?, 'Code', ?, ?)", (id_, int(ts * 1000), text, f"{text} zebra"))
        con.execute("INSERT INTO events (id, ts, app, title) VALUES (?, ?, 'Code', ?)", (id_, int(ts * 1000), text))
        for shot in (tmp_path / "frames" / f"{id_}.jpg", tmp_path / "frames" / "kept" / f"{id_}.jpg"):
            shot.write_bytes(b"jpeg")
            os.utime(shot, (ts, ts))
    con.commit()
    return db, con


def test_retention_deletes_old_frames_only_when_distilled(tmp_path):
    db, con = retention_db(tmp_path)
    assert len(store.search("zebra", db=db)) == 3  # all indexed

    store.set_meta(con, "distilled_frame_id", 1)
    con.commit()
    assert store.retain(db, CONFIG, NOW) == 4  # frame 1, its event, two screenshots
    assert [r[0] for r in con.execute("SELECT id FROM frames")] == [2, 3]
    assert [r[0] for r in con.execute("SELECT id FROM events")] == [2, 3]
    assert sorted(p.name for p in (tmp_path / "frames").rglob("*.jpg")) == ["2.jpg", "2.jpg", "3.jpg", "3.jpg"]
    assert sorted(r["title"] for r in store.search("zebra", db=db)) == ["ancient pending", "recent distilled"]
    assert b"ancient distilled" not in db.read_bytes()  # overwritten, in the table and in the index

    store.set_meta(con, "distilled_frame_id", 3)
    con.commit()
    assert store.retain(db, {**CONFIG, "retain_frames_days": 0}, NOW) == 0  # 0 keeps everything
    assert store.retain(db, CONFIG, NOW) == 4
    assert [r[0] for r in con.execute("SELECT id FROM frames")] == [3]
    assert [r["title"] for r in store.search("zebra", db=db)] == ["recent distilled"]
    assert store.retain(tmp_path / "absent.sqlite", CONFIG, NOW) == 0


def test_retention_deletes_old_episodes_only_when_consolidated_and_facts_keep_their_pointer(tmp_path):
    db = tmp_path / "db.sqlite"
    old = episode(tmp_path, "alpha", NOW - 100 * DAY, "The user chose SQLite.")
    fresh = episode(tmp_path, "alpha", NOW - 2 * DAY, "The user added an index.")
    assert store.retain(db, CONFIG, NOW) == 0  # the database does not exist yet
    sqlite3.connect(db).close()
    assert store.retain(db, CONFIG, NOW) == 0  # old, but not consolidated
    model, _ = replies(ops({"op": "add", "list": "facts", "text": "The store is SQLite.", "sources": ["E1"]}))
    late = episode(tmp_path, "beta", NOW - 100 * DAY, "Never consolidated.")
    consolidate_only_alpha = lambda prompt, config: model(prompt, config) if "Project: alpha" in prompt else "not json"

    assert consolidate.run(db, CONFIG, consolidate_only_alpha, NOW, log=lambda _: None) == (1, 1)

    alpha, beta = tmp_path / "memories" / "alpha", tmp_path / "memories" / "beta"
    assert sorted(p.name for p in alpha.iterdir()) == [f"{fresh}.md", "facts.md"]
    assert f"(sources: {old}; confirmed: {day(NOW - 100 * DAY)})" in (alpha / "facts.md").read_text()
    assert (beta / f"{late}.md").exists()
    assert not consolidate.due(db, NOW + DAY - 60) and consolidate.due(db, NOW + DAY)
    assert store.retain(db, {**CONFIG, "retain_episodes_days": 1}, NOW) == 1 and (alpha / "facts.md").exists()


def test_forget_removes_facts_whose_sources_all_fall_in_the_period(tmp_path):
    db = tmp_path / "db.sqlite"
    sqlite3.connect(db).close()
    old = episode(tmp_path, "alpha", NOW - 5 * 3600, "Old work.")
    new = episode(tmp_path, "alpha", NOW - 600, "New work.")
    gone = stamp(NOW - 1200)  # an episode retention already deleted
    facts = tmp_path / "memories" / "alpha" / "facts.md"
    tail = f"confirmed: {day(NOW)})"
    facts.write_text(
        "---\ntype: \"facts\"\n---\n\n"
        f"- Learned in the last hour. (sources: {new}; {tail}\n"
        f"- Learned from a deleted episode. (sources: {gone}, {new}; {tail}\n"
        f"- Learned earlier, confirmed in the last hour. (sources: {old}, {new}; {tail}\n"
        f"- Pinned, from the last hour. (sources: {new}; {tail} (pinned)\n"
        "- Written by hand\n"
    )
    assert len(store.search("learned", db=db)) == 1  # indexed before forgetting
    assert store.forget(3600, db=db) == 1 + 3  # the new episode and three facts
    assert b"Pinned, from the last hour" not in db.read_bytes()  # the index copy is overwritten too
    assert "confirmed in the last hour" in store.search("learned", db=db)[0]["snippet"]
    assert [line.split(".")[0] for line in facts.read_text().splitlines()[4:]] == [
        "- Learned earlier, confirmed in the last hour", "- Written by hand",
    ]
    assert store.forget(None, db=db) == 2 and not facts.exists()  # --all: the old episode and the file


def test_consolidate_command_and_the_daily_run_of_watch(tmp_path, monkeypatch, capsys):
    db = tmp_path / "db.sqlite"
    sqlite3.connect(db).close()
    monkeypatch.setattr(store, "DB", db)
    runs = []
    real_run, real_due = consolidate.run, consolidate.due
    monkeypatch.setattr(cli.consolidate, "run", lambda config: runs.append(1) or real_run(db, config, lambda *a: ops()))
    monkeypatch.setattr(cli.consolidate, "due", lambda: real_due(db))
    assert cli.main(["consolidate"]) == 0
    assert "Consolidated 0 fact change(s); retention deleted 0" in capsys.readouterr().out

    con = sqlite3.connect(db)
    con.execute("DELETE FROM deepmem_meta")
    con.commit()
    monkeypatch.setattr(cli.vision, "describe_pending", lambda config: 0)
    monkeypatch.setattr(cli.distill, "run_once", lambda config: 0)
    passes = iter([None, None, KeyboardInterrupt])

    def sleep(seconds):
        if next(passes):
            raise KeyboardInterrupt

    monkeypatch.setattr(cli.time, "sleep", sleep)
    with pytest.raises(KeyboardInterrupt):
        cli.main(["distill", "--watch", "--force"])
    assert len(runs) == 2  # three passes of the watch loop, one daily run
    assert cli.main(["distill", "--force"]) == 0 and len(runs) == 2  # a single pass never consolidates
