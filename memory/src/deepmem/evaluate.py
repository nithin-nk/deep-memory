"""Measures the memory pipeline on frozen input, so that models, prompts and
chunking can be compared.

A fixture is a frozen copy of captured frames. A case is a query an agent
might send to `recall` plus the facts a good answer must contain. `run`
distills the fixture in a temporary directory (real memories are untouched)
and reports how many cases are answered, how long it took, and how many
entities the model named that never appeared in the captures.
"""

import json
import re
import sqlite3
import tempfile
import time
import tomllib
from pathlib import Path

from deepmem import distill, store

EVAL_DIR = store.HOME / "eval"
FIXTURE = EVAL_DIR / "fixture.jsonl"
CASES = EVAL_DIR / "cases.toml"

_FIELDS = ("id", "ts", "app", "title", "url", "text")


def export_fixture(path: Path = FIXTURE, seconds: float | None = None, db: Path = store.DB) -> int:
    """Write captured frames (all, or the last `seconds`) to a JSON Lines file."""
    cutoff = 0 if seconds is None else int((time.time() - seconds) * 1000)
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        rows = con.execute(
            "SELECT id, ts, app, title, url, text FROM frames WHERE ts >= ? ORDER BY id", (cutoff,)
        ).fetchall()
    finally:
        con.close()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as file:
        for row in rows:
            record = dict(zip(_FIELDS, row))
            for key in ("title", "url", "text"):  # second layer; the daemon redacts already
                record[key] = record[key] and store.redact(record[key])
            file.write(json.dumps(record, ensure_ascii=False) + "\n")
    return len(rows)


def load_fixture(path: Path) -> list[distill.Frame]:
    with path.open(encoding="utf-8") as file:
        return [distill.Frame(*(json.loads(line)[key] for key in _FIELDS)) for line in file if line.strip()]


def load_cases(path: Path) -> list[dict]:
    """Cases from TOML: `[[case]]` with `query` (keywords for recall) and
    `expect` (strings that must all appear); `question` is a note for humans."""
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


def run(fixture: Path, cases_path: Path, config: dict, model=distill.call_ollama) -> dict:
    frames = load_fixture(fixture)
    cases = load_cases(cases_path)
    if not frames:
        raise ValueError(f"fixture {fixture} has no frames")

    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "db.sqlite"
        con = sqlite3.connect(db)
        con.execute(
            "CREATE TABLE frames (id INTEGER PRIMARY KEY, ts INTEGER NOT NULL, app TEXT NOT NULL, "
            "title TEXT NOT NULL, url TEXT, text TEXT NOT NULL)"
        )
        con.executemany("INSERT INTO frames (id, ts, app, title, url, text) VALUES (?, ?, ?, ?, ?, ?)", frames)
        con.commit()
        con.close()

        logs: list[str] = []
        started = time.time()
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
            found = store.search(case["query"], 5, db=db)
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
