# deep-memory

Local, open-source memory for coding agents. A Rust daemon records what you do
on your computer, a Python service turns it into memories, and an MCP server
gives coding agents access. Nothing leaves your machine.

Status: Slice 1 of [tasks/todo.md](tasks/todo.md). Records app and window
focus changes; agents can ask what you were just doing. macOS only.

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
   warning. For window titles, enable your terminal under System Settings >
   Privacy & Security > Accessibility, then run it again. Data goes to `~/.deepmem/db.sqlite` (override with
   `DEEPMEM_DB`).

2. Register the MCP server with Claude Code (use the absolute path to this
   repository's `memory` directory):

   ```bash
   claude mcp add deepmem -- uv run --directory /absolute/path/to/deep-memory/memory deepmem-mcp
   ```

3. Switch between a few apps, then ask the agent "what was I just doing?".
   It calls `current_context` and gets the last 20 focus changes.

## Test

```bash
cd memory && uv run pytest
```

## Layout

- `capture/`: Rust daemon. Only writes raw tables to the SQLite file.
- `memory/`: Python package. Reads raw tables, serves MCP.
- `tasks/todo.md`: slice plan.
