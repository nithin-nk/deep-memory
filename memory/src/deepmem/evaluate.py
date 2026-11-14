"""Measures the memory pipeline on frozen input, so that models, prompts and
chunking can be compared.

A fixture is a frozen copy of captured frames. A case is a query an agent
might send to `recall` plus the facts a good answer must contain. `run`
distills the fixture in a temporary directory (real memories are untouched)
and reports how many cases are answered, how long it took, and how many
entities the model named that never appeared in the captures.

Screenshots still on disk (waiting, or kept with `keep_screenshots`) are
copied next to the fixture, in `<fixture name>.images/`. `run` has the model
under test describe them again, so vision is measured too.
"""

import json
import re
import shutil
import sqlite3
import tempfile
import time
import tomllib
from pathlib import Path

from deepmem import distill, store, vision

EVAL_DIR = store.HOME / "eval"
FIXTURE = EVAL_DIR / "fixture.jsonl"
CASES = EVAL_DIR / "cases.toml"

_FIELDS = ("id", "ts", "app", "title", "url", "text")


def export_fixture(path: Path = FIXTURE, seconds: float | None = None, db: Path = store.DB) -> int:
    """Write captured frames (all, or the last `seconds`) to a JSON Lines
    file, and the screenshots still on disk to `<name>.images/` beside it."""
    cutoff = 0 if seconds is None else int((time.time() - seconds) * 1000)
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        has_image = any(column[1] == "image" for column in con.execute("PRAGMA table_info(frames)"))
        rows = con.execute(
            f"SELECT id, ts, app, title, url, text, {'image' if has_image else 'NULL'} FROM frames "
            "WHERE ts >= ? ORDER BY id",
            (cutoff,),
        ).fetchall()
    finally:
        con.close()
    path.parent.mkdir(parents=True, exist_ok=True)
    images = path.with_suffix(".images")
    with path.open("w", encoding="utf-8") as file:
        for *row, image in rows:
            record = dict(zip(_FIELDS, row))
            for key in ("title", "url", "text"):  # second layer; the daemon redacts already
                record[key] = record[key] and store.redact(record[key])
            shot = db.parent / "frames" / image if image else None
            if shot and shot.exists():
                images.mkdir(exist_ok=True)
                shutil.copyfile(shot, images / shot.name)
                record["image"] = shot.name
            file.write(json.dumps(record, ensure_ascii=False) + "\n")
    return len(rows)


def fixture_images(path: Path) -> dict[int, Path]:
    """Frame id -> screenshot file, for the fixture's frames that have one."""
    found = {}
    with path.open(encoding="utf-8") as file:
        for record in (json.loads(line) for line in file if line.strip()):
            shot = path.with_suffix(".images") / Path(record.get("image") or "-").name
            if shot.exists():
                found[record["id"]] = shot
    return found


def load_fixture(path: Path) -> list[distill.Frame]:
    with path.open(encoding="utf-8") as file:
        return [distill.Frame(*(json.loads(line)[key] for key in _FIELDS)) for line in file if line.strip()]


def load_cases(path: Path) -> list[dict]:
    """Cases from TOML: `[[case]]` with `query` (keywords for recall) and
    `expect` (strings that must all appear). Optional: `project` limits recall
    to that project, as for an agent working in it; `question` is a note for
    humans."""
    with path.open("rb") as file:
        cases = tomllib.load(file).get("case", [])
    for case in cases:
        if not isinstance(case.get("query"), str) or not case.get("expect"):
            raise ValueError(f"each case needs `query` and `expect`: {case}")
        case["expect"] = [case["expect"]] if isinstance(case["expect"], str) else list(case["expect"])
    return cases


_IDENTIFIER = re.compile(r"[A-Za-z0-9_./:~@#-]{4,}")


def unsupported_entities(entities: list[str], source: str) -> list[str]:
    """Entities whose concrete parts never occur in the captured text.

    An entity is checked through its identifier-like tokens (containing a
    digit or one of . / _ -): file names, hashes, URLs, branch names. Without
    such tokens, its longer plain words are checked instead.
    """
    # ponytail: substring check only. It cannot tell a wrong claim about a
    # real entity from a right one; use a judge model if that rate matters.
    source = source.lower()
    missing = []
    for entity in entities:
        tokens = [t.strip(".:,-") for t in _IDENTIFIER.findall(entity)]
        concrete = [t for t in tokens if re.search(r"[0-9._/-]", t)]
        check = concrete or [t for t in tokens if len(t) >= 5]
        if any(token.lower() not in source for token in check):
            missing.append(entity)
    return missing


def _contains_all(text: str, expected: list[str]) -> bool:
    text = text.lower()
    return all(item.lower() in text for item in expected)


def run(fixture: Path, cases_path: Path, config: dict, model=distill.call_ollama, vision_model=vision.call_vision) -> dict:
    frames = load_fixture(fixture)
    images = fixture_images(fixture)
    cases = load_cases(cases_path)
    if not frames:
        raise ValueError(f"fixture {fixture} has no frames")

    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "db.sqlite"
        con = sqlite3.connect(db)
        con.execute(
            "CREATE TABLE frames (id INTEGER PRIMARY KEY, ts INTEGER NOT NULL, app TEXT NOT NULL, "
            "title TEXT NOT NULL, url TEXT, text TEXT NOT NULL, image TEXT)"
        )
        # A frame with a screenshot starts without text: the model under test
        # has to describe it again.
        (Path(tmp) / "frames").mkdir()
        for shot in images.values():
            shutil.copyfile(shot, Path(tmp) / "frames" / shot.name)
        con.executemany(
            "INSERT INTO frames (id, ts, app, title, url, text, image) VALUES (?, ?, ?, ?, ?, ?, ?)",
            [(*f[:5], "", images[f.id].name) if f.id in images else (*f, None) for f in frames],
        )
        con.commit()

        logs: list[str] = []
        started = time.time()
        described = vision.describe_pending(db, {**config, "keep_screenshots": False}, vision_model, log=logs.append)
        frames = [distill.Frame(*row) for row in con.execute("SELECT id, ts, app, title, url, text FROM frames ORDER BY id")]
        con.close()
        # `now` far in the future: every session in the fixture counts as closed.
        episodes_written = distill.run_once(db, config, model, now=frames[-1].ts / 1000 + 86400, log=logs.append)
        seconds = time.time() - started

        files = sorted((Path(tmp) / "memories").rglob("*.md"))
        memories = [store.read_memory(file) for file in files]
        all_memory_text = "\n".join(body for _, body in memories)

        entity_total, entity_missing = 0, []
        for front, body in memories:
            first, last = (int(n) for n in str(front["frames"]).split("-"))
            source = "\n".join(f"{f.app}\n{f.title}\n{f.url or ''}\n{f.text}" for f in frames if first <= f.id <= last)
            entities = re.findall(r"^- (.+)$", body.partition("## Entities")[2], re.M)
            entity_total += len(entities)
            entity_missing += unsupported_entities(entities, source)

        results = []
        for case in cases:
            found = store.search(case["query"], 5, db=db, visible={case["project"]} if case.get("project") else None)
            memory_text = "\n".join(store.read_memory(Path(r["file"]))[1] for r in found if r["kind"] == "memory")
            screen_text = "\n".join(r["snippet"] for r in found if r["kind"] == "screen")
            if _contains_all(memory_text, case["expect"]):
                outcome = "memory"
            elif _contains_all(memory_text + "\n" + screen_text, case["expect"]):
                outcome = "screen"
            else:
                outcome = "miss"
            results.append({
                "query": case["query"],
                "expect": case["expect"],
                "outcome": outcome,
                "in_memories": _contains_all(all_memory_text, case["expect"]),
            })

    sessions = len(distill.sessionize(frames, config["session_gap_minutes"] * 60, config["session_max_minutes"] * 60))
    return {
        "model": config["model"],
        "frames": len(frames),
        "sessions": sessions,
        "episodes": episodes_written,
        "dropped": sum("dropped frames" in line for line in logs),
        "screenshots": len(images),
        "screenshots_described": described,
        "seconds": round(seconds, 1),
        "entities": entity_total,
        "entities_unsupported": entity_missing,
        "cases": results,
    }


def format_report(report: dict) -> str:
    cases = report["cases"]
    count = lambda test: sum(1 for c in cases if test(c))
    total = len(cases) or 1
    unsupported = report["entities_unsupported"]
    lines = [
        f"model                      {report['model']}",
        f"input                      {report['frames']} frames, {report['sessions']} sessions",
        f"episodes written           {report['episodes']}  (dropped replies: {report['dropped']})",
        f"screenshots described      {report['screenshots_described']}/{report['screenshots']}",
        f"time                       {report['seconds']} s  ({report['seconds'] / max(report['sessions'], 1):.1f} s per session)",
        f"answered from memories     {count(lambda c: c['outcome'] == 'memory')}/{len(cases)}  ({100 * count(lambda c: c['outcome'] == 'memory') // total}%)",
        f"answered incl. screen text {count(lambda c: c['outcome'] != 'miss')}/{len(cases)}  ({100 * count(lambda c: c['outcome'] != 'miss') // total}%)",
        f"fact kept in any memory    {count(lambda c: c['in_memories'])}/{len(cases)}  ({100 * count(lambda c: c['in_memories']) // total}%)",
        f"entities not in captures   {len(unsupported)}/{report['entities']}",
    ]
    lines += [f"    ? {entity}" for entity in unsupported[:8]]
    lines.append("")
    for case in cases:
        kept = "kept" if case["in_memories"] else "lost"
        lines.append(f"  {case['outcome']:<6} {kept:<4}  {case['query']}  ->  {', '.join(case['expect'])}")
    return "\n".join(lines)
