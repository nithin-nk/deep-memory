# deep-memory

Local, open-source memory for coding agents. A Rust daemon records what you do
on your computer, a Python service turns it into memories, and an MCP server
gives coding agents access. Nothing leaves your machine.

Status: Slice 7 of [tasks/todo.md](tasks/todo.md). Records the focused
window's text (from a screenshot when the window exposes none), distills work
sessions into per-project Markdown memories with a local model, and lets each
agent search only its own project. macOS only.

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

## Windows without text (screenshots)

Canvas apps, image viewers, video and some games expose no text to the
accessibility tree. When a window's text (not counting labels of toolbars,
menus and buttons) stays under 200 characters for two captures in a row, the daemon saves a JPEG of that window (longest side at
most 1600 px) to `~/.deepmem/frames/`, at most once a minute per window and
only when the picture changed.

This needs a second permission: System Settings > Privacy & Security >
Screen & System Audio Recording, for the terminal that runs the daemon, then
restart the daemon. Without it nothing changes: no screenshots are taken.
macOS also shows an alert ("requesting to bypass the system private window
picker") at the first screenshot and again from time to time; choose Allow.

`deepmem distill` first has the local vision model describe each waiting
screenshot (what it shows, plus the text visible in it). The result becomes
the frame's text, so it is searched and distilled like any other frame.
`deepmem status` shows how many screenshots are waiting.

- Pause, the app denylist and the private-window rule apply before a
  screenshot is taken, and the picture is of the focused window only, not of
  the screen.
- A picture cannot be redacted, so it stays on disk only until it is
  described; the description goes through the same secret redaction as all
  captured text. `deepmem forget` deletes screenshots too.
- Windows that already have text are never screenshotted.

```toml
screenshots = true            # false: never take screenshots
screenshot_below_chars = 200  # a window with less text than this counts as "no text"
keep_screenshots = false      # true: keep described JPEGs in ~/.deepmem/frames/kept/
```

## Memories

`deepmem distill` turns finished work sessions into episodes: short Markdown
notes saying what you did, what you decided and what is still open.

```bash
ollama pull qwen3-vl:8b
uv run --directory /absolute/path/to/deep-memory/memory deepmem distill
```

- A session ends after 5 minutes without captures, or after 30 minutes.
  A run that finds only an open session says when it will close.
- Episodes are plain files in `~/.deepmem/memories/<project>/`. Edit, move
  or delete them freely; the search index follows the files.
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

## Projects and who can read what

A project is a git repository on this machine, named by its directory.
Each captured window is labelled from the file paths it shows, its
repository URL (GitHub, GitLab, Bitbucket) or an editor title such as
`store.py — deep-memory`. Windows with no such sign are `personal`.

- One session produces one episode per project it touched, stored in
  `~/.deepmem/memories/<project>/`. Move a file to another directory to
  change its project.
- An agent sees only the project of its workspace root: memories, raw
  screen text and recent activity. Not other projects, not `personal`.
- An agent outside any repository sees nothing.

Change the defaults in `~/.deepmem/config.toml`:

```toml
share_personal = false      # true: agents in a project also read `personal`
outside_project = "none"    # or "personal", or "all"
```

The server asks the client for its workspace root (MCP roots). For a client
without that, set `DEEPMEM_PROJECT_DIR` in the server's environment. The
`deepmem` CLI is not scoped: it is you, not an agent.

## Evaluation

Compare models, prompts and settings on the same input.

```bash
deepmem export-fixture            # freeze captured frames (add --last 2h to limit)
cp eval/cases.example.toml ~/.deepmem/eval/cases.toml   # then write your own cases
deepmem eval                      # configured model
deepmem eval --model qwen3-vl:4b  # any other pulled model
```

Vision is measured the same way. The repository holds three picture-only
windows (a diagram, a chart, a board of notes) and three questions whose
answers are visible only in those pictures:

```bash
deepmem eval --fixture eval/vision.fixture.jsonl --cases eval/vision.cases.toml
```

`export-fixture` also copies screenshots that are still on disk (waiting, or
kept with `keep_screenshots = true`) to `fixture.images/`, and `eval` has the
model under test describe them again.

A case is a `recall` query plus the facts a good answer must contain, and
optionally a `project` to search as an agent in that project would. The
report shows how many cases are answered from memories, how many only from
raw screen text, how many facts survived distillation at all, time per
session, dropped model replies, and entities the model named that never
appeared in the captures. Distillation runs in a temporary directory; your
real memories are not touched.

The fixture (`~/.deepmem/eval/fixture.jsonl`) holds captured screen text.
Keep it out of repositories. `deepmem forget` deletes it.

## Privacy controls

- **Menu bar:** the daemon shows `◉` while recording and `⏸` while paused.
  Its menu has Pause/Resume and Quit.
- **Pause:** press Ctrl+Option+Cmd+P, use the menu bar item, or run
  `deepmem pause` / `deepmem resume`. A notification confirms the change. While paused nothing is
  recorded, and a pause survives a daemon restart.
- **Never recorded:** password fields, private browsing windows (recognised
  by the marker in the window title), and password managers. Add your own
  apps in `~/.deepmem/config.toml`, then restart the daemon:

  ```toml
  denylist = ["Signal", "Messages"]
  ```

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
string is not caught; the database is not encrypted yet (Slice 10).

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
- `eval/`: example evaluation cases.
- `testdata/`: redaction fixtures shared by the Rust and Python tests.
- `tasks/todo.md`: slice plan.
