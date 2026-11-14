"""Slice 6: project labels and scoped recall."""

import asyncio
import json
import re

import pytest

from deepmem import projects, server, store
from deepmem.distill import run_once

from test_distill import CONFIG, T0, MIN, frame, make_db


@pytest.fixture
def home(tmp_path, monkeypatch):
    """A fake home directory with two repositories, a worktree and a loose folder."""
    monkeypatch.setenv("HOME", str(tmp_path))
    projects._repo_of_dir.cache_clear()
    for repo in ("alpha", "beta"):
        (tmp_path / repo / ".git").mkdir(parents=True)
        (tmp_path / repo / "src").mkdir()
    worktree = tmp_path / "alpha" / ".claude" / "worktrees" / "feature-x"
    worktree.mkdir(parents=True)
    (worktree / ".git").write_text(f"gitdir: {tmp_path}/alpha/.git/worktrees/feature-x\n")
    (tmp_path / "notes").mkdir()
    yield tmp_path
    projects._repo_of_dir.cache_clear()


def test_of_path_finds_the_repository(home):
    assert projects.of_path(home / "alpha") == "alpha"
    assert projects.of_path("~/alpha/src/not_yet_saved.py") == "alpha"
    assert projects.of_path(home / "alpha" / ".claude" / "worktrees" / "feature-x" / "a.py") == "alpha"
    assert projects.of_path(home / "notes" / "todo.txt") is None
    (home / ".git").mkdir()  # a home directory under version control is not a project
    projects._repo_of_dir.cache_clear()
    assert projects.of_path(home / "notes" / "todo.txt") is None


def test_of_window_uses_title_url_editor_title_then_text(home):
    known: set[str] = set()
    assert projects.of_window("nithin — ~/alpha/src — zsh", None, "", known) == "alpha"
    assert known == {"alpha"}
    # A repository URL counts only for repositories that exist on this machine.
    assert projects.of_window("alpha", "https://github.com/someone/alpha/pull/3", "", known) == "alpha"
    assert projects.of_window("x", "https://github.com/screenpipe/screenpipe", "", known) == "personal"
    assert projects.of_window("store.py — alpha", None, "", known) == "alpha"
    assert projects.of_window("store.py — beta", None, "", known) == "personal"  # beta not seen yet
    text = "edit ~/beta/src/a.py\nrun ~/beta/src/b.py\nsee ~/alpha/src/c.py"
    assert projects.of_window("Claude", None, text, known) == "beta" and "beta" in known
    assert projects.of_window("Inbox (27) - Gmail", "https://mail.google.com/mail/u/0/", "Kotak Bank maintenance", known) == "personal"


def test_visible_to_follows_config(home):
    base = {"share_personal": False, "outside_project": "none"}
    assert projects.visible_to(home / "alpha" / "src", base) == {"alpha"}
    assert projects.visible_to(home / "alpha", {**base, "share_personal": True}) == {"alpha", "personal"}
    assert projects.visible_to(home / "notes", base) == set()
    assert projects.visible_to(None, base) == set()
    assert projects.visible_to(None, {**base, "outside_project": "personal"}) == {"personal"}
    assert projects.visible_to(None, {**base, "outside_project": "all"}) is None


def model(prompt, config):
    project = re.search(r"Project: ([\w-]+)\.", prompt)
    name = project.group(1) if project else "personal"
    return json.dumps({"summary": f"The user worked on {name} zebra-{name} task.", "activities": [], "decisions": [],
                       "open_items": [], "entities": []})


def lines(prefix, n=12):
    return "\n".join(f"{prefix} {i}" for i in range(n))


@pytest.fixture
def scoped_db(home):
    data = home / "data"
    data.mkdir()
    db, con = make_db(data, [
        frame(1, 0, "Code", "auth.rs — alpha", lines("fn alpha_handler in ~/alpha/src/auth.rs line")),
        frame(2, 1, "Code", "billing.py — beta", lines("def beta_invoice in ~/beta/src/billing.py line")),
        frame(3, 2, "Google Chrome", "Inbox - Gmail", lines("Kotak Bank maintenance notice line"), "https://mail.google.com/"),
    ])
    con.execute("CREATE TABLE events (id INTEGER PRIMARY KEY, ts INTEGER NOT NULL, app TEXT NOT NULL, title TEXT NOT NULL)")
    con.executemany("INSERT INTO events (ts, app, title) VALUES (?, ?, ?)", [
        (T0, "Terminal", "zsh — ~/alpha/src"), (T0 + 1, "Terminal", "zsh — ~/beta/src"), (T0 + 2, "Google Chrome", "Inbox - Gmail"),
    ])
    con.commit()
    assert run_once(db, CONFIG, model, now=(T0 + 60 * MIN) / 1000, log=lambda _: None) == 3
    return db


def test_one_episode_per_project_in_its_own_directory(scoped_db):
    memories = scoped_db.parent / "memories"
    assert sorted(p.name for p in memories.iterdir()) == ["alpha", "beta", "personal"]
    front, body = store.read_memory(next((memories / "alpha").glob("*.md")))
    assert front["project"] == "alpha" and "zebra-alpha" in body
    assert "Kotak" not in body and "beta" not in body


def test_agent_in_one_project_cannot_read_another_or_personal(scoped_db):
    alpha = {"alpha"}
    hits = store.search("zebra", db=scoped_db, visible=alpha)
    assert [(r["kind"], r["project"]) for r in hits] == [("memory", "alpha")]
    assert [(r["kind"], r["project"]) for r in store.search("handler", db=scoped_db, visible=alpha)] == [("screen", "alpha")]
    # Neither the other project's memory or screen text, nor personal mail.
    assert store.search("beta_invoice", db=scoped_db, visible=alpha) == []
    assert store.search("Kotak", db=scoped_db, visible=alpha) == []
    assert store.search("zebra", db=scoped_db, visible=set()) == []
    # Unscoped (the CLI and eval) still sees everything.
    assert {r["project"] for r in store.search("zebra", db=scoped_db)} == {"alpha", "beta", "personal"}
    assert {r["project"] for r in store.search("zebra", db=scoped_db, visible={"alpha", "personal"})} == {"alpha", "personal"}

    assert [e["title"] for e in store.recent_events(scoped_db, visible=alpha)] == ["zsh — ~/alpha/src"]
    assert store.recent_events(scoped_db, visible=set()) == []
    assert len(store.recent_events(scoped_db)) == 3


def test_moving_a_memory_file_moves_it_to_the_other_project(scoped_db):
    memories = scoped_db.parent / "memories"
    file = next((memories / "personal").glob("*.md"))
    assert store.search("zebra-personal", db=scoped_db, visible={"alpha"}) == []
    file.rename(memories / "alpha" / file.name)
    assert len(store.search("zebra-personal", db=scoped_db, visible={"alpha"})) == 1


def test_server_scopes_by_the_callers_workspace(home, monkeypatch):
    class Roots:
        def __init__(self, uri): self.roots = [type("Root", (), {"uri": uri})()] if uri else []

    class Session:
        def __init__(self, uri=None, fail=False): self.uri, self.fail = uri, fail
        async def list_roots(self):
            if self.fail:
                raise RuntimeError("roots not supported")
            return Roots(self.uri)

    ctx = lambda **kw: type("Ctx", (), {"session": Session(**kw)})()
    monkeypatch.setattr(store, "CONFIG_FILE", home / "no-config.toml")
    monkeypatch.delenv("CLAUDE_PROJECT_DIR", raising=False)
    monkeypatch.delenv("DEEPMEM_PROJECT_DIR", raising=False)

    assert asyncio.run(server._visible(ctx(uri=f"file://{home}/alpha/src"))) == {"alpha"}
    assert asyncio.run(server._visible(ctx(uri=f"file://{home}/notes"))) == set()
    assert asyncio.run(server._visible(ctx(fail=True))) == set()  # unknown caller reads nothing
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(home / "beta"))
    assert asyncio.run(server._visible(ctx(fail=True))) == {"beta"}
