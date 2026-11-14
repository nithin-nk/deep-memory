# deep-memory

Local, open-source memory for coding agents. A Rust daemon records what you do
on your computer, a Python service turns it into memories, and an MCP server
gives coding agents access. Nothing leaves your machine.

Status: Slice 2 of [tasks/todo.md](tasks/todo.md). Records app and window
focus changes plus the text of the focused window; agents can ask what you
were just doing and search what you have seen. macOS only.

**Privacy note:** capture has no pause switch, app denylist or secret
redaction yet (Slice 3). Everything visible in the focused window is stored
in plain text in `~/.deepmem/db.sqlite`. Password fields are skipped.

## Requirements

- macOS
- [Rust](https://rustup.rs) (for the capture daemon)
- [uv](https://docs.astral.sh/uv/) (for the Python side)

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

## Test

```bash
cd memory && uv run pytest
```

```bash
cd capture && cargo test
```

## Layout

- `capture/`: Rust daemon. Only writes raw tables to the SQLite file.
- `memory/`: Python package. Reads raw tables, owns the search index, serves MCP.
- `tasks/todo.md`: slice plan.
