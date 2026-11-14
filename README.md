# deep-memory

Local, open-source memory for coding agents. A Rust daemon records what you do
on your computer, a Python service turns it into memories, and an MCP server
gives coding agents access. Nothing leaves your machine.

Status: Slice 4 of [tasks/todo.md](tasks/todo.md), plus the desktop window.
Records the focused window's text, distills work sessions into Markdown
memories with a local model, and lets agents search both. A window in the
menu bar app shows the memories and the settings. macOS only.

## Requirements

- macOS
- [Rust](https://rustup.rs) (for the capture daemon)
- [uv](https://docs.astral.sh/uv/) (for the Python side)
- [Ollama](https://ollama.com) with a model pulled, default `qwen3-vl:8b`
  (for distillation; capture and search work without it)

## Run

1. Start the capture daemon:

   ```bash
   cd capture && cargo run --release
   ```

   Without Accessibility permission it records app names only and prints a
   warning. For window titles and screen text, enable your terminal under
   System Settings > Privacy & Security > Accessibility, then run it again.
   Data goes to `~/.deepmem/db.sqlite` (override with `DEEPMEM_DB`). Set
   `DEEPMEM_DEBUG=1` to print one line per stored frame (sizes only).

2. Register the MCP server with Claude Code (use the absolute path to this
   repository's `memory` directory):

   ```bash
   claude mcp add deepmem -- uv run --directory /absolute/path/to/deep-memory/memory deepmem-mcp
   ```

3. Ask the agent:
   - "what was I just doing?" It calls `current_context` and gets the last
     20 focus changes.
   - "find the page I read about X". It calls `recall` and gets matching
     snippets with app, window title, URL and time.

## How capture works

The daemon checks the focused window every 250 ms and reads its text from the
accessibility tree when focus changes, when keyboard or mouse input pauses for
0.7 s, and at least every 5 s while you are active. A frame is stored only if
its text differs from the previous one. Chromium and Electron apps are asked to
enable their accessibility tree, otherwise they expose no page content.

## Memories

`deepmem distill` turns finished work sessions into episodes: short Markdown
notes saying what you did, what you decided and what is still open.

```bash
ollama pull qwen3-vl:8b
uv run --directory /absolute/path/to/deep-memory/memory deepmem distill
```

- A session ends after 5 minutes without captures, or after 30 minutes.
- Episodes are plain files in `~/.deepmem/memories/`. Edit or delete them
  freely; the search index follows the files.
- `recall` returns matching memories first, then raw screen text.
- The model runs only on mains power or while you are away; `--force`
  overrides, `--watch` repeats every 5 minutes.
- Captured text is sent only to an Ollama server on this machine. Any other
  `ollama_url` is refused.

Settings in `~/.deepmem/config.toml` (defaults shown):

```toml
model = "qwen3-vl:8b"
ollama_url = "http://localhost:11434"
num_ctx = 16384
session_gap_minutes = 5
session_max_minutes = 30
distill_on_battery = false
```

## The window

Click the menu bar item (`◉`) and choose **Open deep-memory…**, or start the
daemon with `--window`.

- **Memories:** read, search and delete memories. "Show in Finder" selects
  the file so you can edit it in your own editor.
- **Blocked sites:** websites and apps that are never recorded.
- **Settings:** the values of `~/.deepmem/config.toml`, plus capture state.

Changes are written to `config.toml` (your comments are kept) and apply at
once. The window is part of the daemon and talks to it directly: no network
port is opened.

## Privacy controls

- **Menu bar:** the daemon shows `◉` while recording and `⏸` while paused.
  Its menu has Pause/Resume, Open deep-memory… and Quit.
- **Pause:** press Ctrl+Option+Cmd+P, use the menu bar item, or run
  `deepmem pause` / `deepmem resume`. A notification confirms the change. While paused nothing is
  recorded, and a pause survives a daemon restart.
- **Never recorded:** password fields, private browsing windows (recognised
  by the marker in the window title), and password managers. Add your own
  apps and websites in the window, or in `~/.deepmem/config.toml`. No
  restart is needed:

  ```toml
  denylist = ["Signal", "Messages"]
  url_blocklist = ["bank.com", "example.com/private"]
  ```

  A blocked site covers its subdomains; with a path, only that part of the
  site. Nothing is stored for a blocked page: no text, title or address.
  Blocking a site does not delete what was recorded from it earlier.

- **Secrets:** API keys, tokens, private keys, passwords in `name = value`
  form and card numbers are replaced with `[REDACTED:kind]` by the daemon
  before anything is written to disk, and again before text is indexed or
  returned to an agent.
- **Forget:** `deepmem forget --last 1h` or `deepmem forget --all` deletes
  captured data, overwrites it in the database file, and removes the
  memories distilled from that period.
- **Status:** `deepmem status`.

Run the CLI with `uv run --directory /absolute/path/to/deep-memory/memory deepmem <command>`.

Known limits: secret detection is pattern based, so an unlabelled random
string is not caught; the database is not encrypted yet (Slice 10). A
browser may not report a page's address on the first read after it gets
focus. If the page then turns out to be blocked, what was stored in that
moment is deleted again, but a search made in the same second can keep a
copy in the search index until the next `deepmem forget`.

## Test

```bash
cd memory && uv run pytest
```

```bash
cd capture && cargo test
```

## Layout

- `capture/`: Rust daemon and its window. Writes raw tables to the SQLite
  file, reads and deletes memory files, edits `config.toml`.
- `memory/`: Python package. Reads raw tables, owns the search index, serves MCP.
- `testdata/`: redaction fixtures shared by the Rust and Python tests.
- `tasks/todo.md`: slice plan.
