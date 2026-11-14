//! Everything the window can ask for, as JSON in and JSON out. No AppKit
//! here, so it can be tested without a window.
//!
//! Request: `{"id": 1, "cmd": "memories_list", "args": {...}}`.
//! Reply: `{"id": 1, "ok": ...}` or `{"id": 1, "error": "message"}`.

use crate::config;
use rusqlite::{Connection, OpenFlags};
use serde_json::{json, Value};
use std::fs;
use std::path::{Path, PathBuf};
use std::process::Command;

pub struct Context {
    /// `~/.deepmem`: config.toml and the `paused` flag.
    pub home: PathBuf,
    pub db_path: PathBuf,
    /// Whether the daemon has the Accessibility permission.
    pub trusted: bool,
}

impl Context {
    /// Same place the Python side writes to: next to the database.
    fn memories(&self) -> PathBuf {
        self.db_path.parent().unwrap_or(Path::new(".")).join("memories")
    }
}

pub fn handle(request: &str, ctx: &Context) -> String {
    let request: Value = serde_json::from_str(request).unwrap_or(Value::Null);
    let id = &request["id"];
    let args = &request["args"];
    let result = match request["cmd"].as_str().unwrap_or("") {
        "status" => Ok(status(ctx)),
        "toggle_pause" => toggle_pause(ctx),
        "memories_list" => Ok(memories_list(ctx, args["query"].as_str().unwrap_or(""))),
        "memory_get" => memory_get(ctx, args["file"].as_str().unwrap_or("")),
        "memory_delete" => memory_delete(ctx, args["file"].as_str().unwrap_or("")),
        "memory_reveal" => memory_reveal(ctx, args["file"].as_str().unwrap_or("")),
        "config_get" => config::get(&ctx.home),
        "config_set" => match args.as_object() {
            Some(changes) => config::set(&ctx.home, changes).and_then(|_| config::get(&ctx.home)),
            None => Err("config_set needs an object".to_string()),
        },
        other => Err(format!("unknown command: {other}")),
    };
    match result {
        Ok(value) => json!({ "id": id, "ok": value }),
        Err(message) => json!({ "id": id, "error": message }),
    }
    .to_string()
}

fn status(ctx: &Context) -> Value {
    let mut counts = json!({ "events": 0, "frames": 0 });
    let mut last_capture: Option<i64> = None;
    if let Ok(db) = Connection::open_with_flags(&ctx.db_path, OpenFlags::SQLITE_OPEN_READ_ONLY) {
        for table in ["events", "frames"] {
            let row = db.query_row(&format!("SELECT count(*), max(ts) FROM {table}"), [], |r| {
                Ok((r.get::<_, i64>(0)?, r.get::<_, Option<i64>>(1)?))
            });
            if let Ok((count, last)) = row {
                counts[table] = count.into();
                last_capture = last_capture.max(last);
            }
        }
    }
    json!({
        "paused": ctx.home.join("paused").exists(),
        "accessibility": ctx.trusted,
        "events": counts["events"],
        "frames": counts["frames"],
        "last_capture_ms": last_capture,
        "memories_dir": ctx.memories(),
        "config_file": ctx.home.join("config.toml"),
    })
}

/// The flag file is the single source of truth; the capture loop sees the
/// change on its next tick and updates the menu bar.
fn toggle_pause(ctx: &Context) -> Result<Value, String> {
    let flag = ctx.home.join("paused");
    let result = if flag.exists() { fs::remove_file(&flag) } else { fs::write(&flag, "") };
    result.map_err(|err| format!("cannot change {}: {err}", flag.display()))?;
    Ok(status(ctx))
}

// --- memories ---------------------------------------------------------------

/// (front matter, body) of a memory file. Front matter values are JSON, as
/// written by the distiller; a hand-edited value that is not JSON is kept as
/// text. Same rules as `read_memory` in memory/src/deepmem/store.py.
fn parse_memory(text: &str) -> (Value, &str) {
    let mut front = json!({});
    let mut body = text;
    if let Some((header, rest)) = text.strip_prefix("---\n").and_then(|t| t.split_once("\n---\n")) {
        for line in header.lines() {
            let (key, value) = line.split_once(": ").unwrap_or((line, ""));
            front[key] = serde_json::from_str(value).unwrap_or_else(|_| value.into());
        }
        body = rest;
    }
    (front, body.trim())
}

fn markdown_files(dir: &Path, found: &mut Vec<PathBuf>) {
    for entry in fs::read_dir(dir).into_iter().flatten().flatten() {
        let path = entry.path();
        if path.is_dir() {
            markdown_files(&path, found);
        } else if path.extension().is_some_and(|ext| ext == "md") {
            found.push(path);
        }
    }
}

/// Newest first. With a query, only memories that contain every word of it.
// ponytail: reads every file on each call and matches substrings. The Python
// side owns the FTS index; query it instead if thousands of files make this slow.
fn memories_list(ctx: &Context, query: &str) -> Value {
    let root = ctx.memories();
    let words: Vec<String> = query.split_whitespace().map(str::to_lowercase).collect();
    let mut files = Vec::new();
    markdown_files(&root, &mut files);
    let mut items = Vec::new();
    for path in files {
        let Ok(text) = fs::read_to_string(&path) else { continue };
        let (front, body) = parse_memory(&text);
        let haystack = body.to_lowercase();
        if !words.iter().all(|word| haystack.contains(word)) {
            continue;
        }
        let summary = body.lines().find(|l| !l.trim().is_empty() && !l.starts_with('#')).unwrap_or("");
        items.push(json!({
            "file": path.strip_prefix(&root).unwrap_or(&path),
            "start": front["start"],
            "end": front["end"],
            "apps": front["apps"],
            "summary": summary.chars().take(240).collect::<String>(),
        }));
    }
    // ISO timestamps sort as text; files without one go last, by name.
    let key = |item: &Value| (item["start"].as_str().unwrap_or("").to_string(), item["file"].to_string());
    items.sort_by(|a, b| key(b).cmp(&key(a)));
    items.into()
}

/// The file a request names, only if it is a Markdown file inside the
/// memories directory (after resolving `..` and symlinks).
fn memory_path(ctx: &Context, file: &str) -> Result<PathBuf, String> {
    let not_found = || format!("No such memory: {file}");
    let root = ctx.memories().canonicalize().map_err(|_| not_found())?;
    let path = root.join(file).canonicalize().map_err(|_| not_found())?;
    if path.starts_with(&root) && path.is_file() && path.extension().is_some_and(|ext| ext == "md") {
        Ok(path)
    } else {
        Err(not_found())
    }
}

fn memory_get(ctx: &Context, file: &str) -> Result<Value, String> {
    let path = memory_path(ctx, file)?;
    let text = fs::read_to_string(&path).map_err(|err| format!("cannot read {file}: {err}"))?;
    let (front, body) = parse_memory(&text);
    Ok(json!({ "file": file, "path": path, "front": front, "body": body }))
}

/// Deletes the file. The Python search index drops it on its next search
/// (`sync_memory_index` rebuilds when the set of files changes).
fn memory_delete(ctx: &Context, file: &str) -> Result<Value, String> {
    let path = memory_path(ctx, file)?;
    fs::remove_file(&path).map_err(|err| format!("cannot delete {file}: {err}"))?;
    Ok(Value::Null)
}

/// Selects the file in Finder, from where the user can open it in an editor.
fn memory_reveal(ctx: &Context, file: &str) -> Result<Value, String> {
    let path = memory_path(ctx, file)?;
    let shown = Command::new("open").arg("-R").arg(&path).status();
    shown.map(|_| Value::Null).map_err(|err| format!("cannot open Finder: {err}"))
}

#[cfg(test)]
mod tests {
    use super::*;

    const EPISODE: &str = "---\ntype: \"episode\"\nstart: \"2026-11-14T09:38:44+05:30\"\n\
        end: \"2026-11-14T10:07:41+05:30\"\napps: [\"Terminal\", \"Google Chrome\"]\n---\n\n\
        The user set up the Capture daemon.\n\n## Done\n\n- Ran cargo build\n";

    fn context(name: &str) -> Context {
        let home = std::env::temp_dir().join(format!("deepmem-api-{}-{name}", std::process::id()));
        let _ = fs::remove_dir_all(&home);
        fs::create_dir_all(home.join("memories/_unsorted")).unwrap();
        fs::write(home.join("memories/_unsorted/a.md"), EPISODE).unwrap();
        fs::write(home.join("memories/_unsorted/b.md"), "Hand-written note about lunch.\n").unwrap();
        fs::write(home.join("secret.md"), "outside the memories directory").unwrap();
        Context { db_path: home.join("db.sqlite"), home, trusted: false }
    }

    fn call(ctx: &Context, cmd: &str, args: Value) -> Value {
        let reply = handle(&json!({ "id": 7, "cmd": cmd, "args": args }).to_string(), ctx);
        let reply: Value = serde_json::from_str(&reply).unwrap();
        assert_eq!(reply["id"], 7);
        reply
    }

    #[test]
    fn lists_searches_and_reads_memories() {
        let ctx = context("list");
        let all = call(&ctx, "memories_list", json!({}));
        let all = all["ok"].as_array().unwrap();
        assert_eq!(all.len(), 2);
        assert_eq!(all[0]["file"], "_unsorted/a.md"); // dated one first
        assert_eq!(all[0]["summary"], "The user set up the Capture daemon.");
        assert_eq!(all[0]["apps"], json!(["Terminal", "Google Chrome"]));

        let found = call(&ctx, "memories_list", json!({ "query": "CARGO capture" }));
        assert_eq!(found["ok"].as_array().unwrap().len(), 1);
        let none = call(&ctx, "memories_list", json!({ "query": "cargo lunch" }));
        assert_eq!(none["ok"].as_array().unwrap().len(), 0);

        let one = call(&ctx, "memory_get", json!({ "file": "_unsorted/a.md" }));
        assert_eq!(one["ok"]["front"]["start"], "2026-11-14T09:38:44+05:30");
        assert!(one["ok"]["body"].as_str().unwrap().starts_with("The user set up"));
    }

    #[test]
    fn deletes_only_files_inside_the_memories_directory() {
        let ctx = context("delete");
        for file in ["../secret.md", "/etc/hosts", "_unsorted", "missing.md", ""] {
            assert!(call(&ctx, "memory_delete", json!({ "file": file }))["error"].is_string(), "{file}");
            assert!(call(&ctx, "memory_get", json!({ "file": file }))["error"].is_string(), "{file}");
        }
        assert!(ctx.home.join("secret.md").exists());

        assert!(call(&ctx, "memory_delete", json!({ "file": "_unsorted/a.md" }))["error"].is_null());
        assert!(!ctx.home.join("memories/_unsorted/a.md").exists());
        assert_eq!(call(&ctx, "memories_list", json!({}))["ok"].as_array().unwrap().len(), 1);
    }

    #[test]
    fn status_pause_and_settings() {
        let ctx = context("status");
        let status = call(&ctx, "status", json!({}));
        assert_eq!(status["ok"]["paused"], false);
        assert_eq!(status["ok"]["frames"], 0); // no database yet

        assert_eq!(call(&ctx, "toggle_pause", json!({}))["ok"]["paused"], true);
        assert!(ctx.home.join("paused").exists());
        assert_eq!(call(&ctx, "toggle_pause", json!({}))["ok"]["paused"], false);

        let saved = call(&ctx, "config_set", json!({ "url_blocklist": ["https://bank.com/"] }));
        assert_eq!(saved["ok"]["settings"]["url_blocklist"], json!(["bank.com"]));
        assert!(call(&ctx, "config_set", json!({ "num_ctx": -1 }))["error"].is_string());
        assert!(call(&ctx, "nonsense", json!({}))["error"].is_string());
        assert!(serde_json::from_str::<Value>(&handle("not json", &ctx)).unwrap()["error"].is_string());
    }
}
