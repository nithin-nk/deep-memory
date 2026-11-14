# deep-memory: vertical slice plan

Local, open-source memory for coding agents. A Rust daemon captures what the
user does, a Python service distills it into editable memories, and an MCP
server gives coding agents scoped access. Nothing leaves the machine unless the
user configures it.

## Assumptions (unconfirmed, change before Slice 1 if wrong)

- macOS only for now.
- Local models through Ollama by default. Optional user-supplied API key is
  Slice 11, not earlier.
- Default model is one multimodal model for both vision and distillation
  (candidate: Qwen3-VL 8B; verify the exact tag at ollama.com/library).
  Model name is a config value.

## Fixed decisions (every slice follows these)

- **Layout:** `capture/` is a Rust crate (binary `deepmem-capture`).
  `memory/` is a Python package managed with `uv` (CLI `deepmem`).
- **Contract between Rust and Python:** one SQLite file,
  `~/.deepmem/db.sqlite`, WAL mode. Rust is the only inserter into raw tables.
  Python reads raw tables, owns everything else, and deletes raw rows only
  for `deepmem forget` and retention. No IPC, no HTTP between them. Pause
  state is a flag file, `~/.deepmem/paused`, that both sides honour.
- **Memories are Markdown files** under `~/.deepmem/memories/<project>/`.
  The files are the source of truth; SQLite FTS5 is only an index over them.
- **Config:** one file, `~/.deepmem/config.toml`.
- **No vector database, no audio, no Windows/Linux** until a slice says so.
- **Each slice leaves one runnable check** (a test or a demo command) and
  updates `README.md` with how to run it.

## How to run a slice

One coding agent session per slice, in order. Give the agent this file and the
slice number. A slice is done only when its "Done when" check passes. Steps
marked **Human** need the user (macOS permission prompts cannot be granted by
an agent).

---

## Slice 1: Walking skeleton (window focus to MCP)

Thinnest end-to-end path, proves the Rust/SQLite/Python/MCP contract.

- [x] Rust: daemon records app name, window title, timestamp on every
      focus change into table `events`.
- [x] Python: `uv` project, MCP server (stdio) with one tool
      `current_context()` returning the last 20 events.
- [x] README: how to start the daemon and register the MCP server in
      Claude Code.
- **Human:** grant Accessibility permission to the daemon.
- **Done when:** switch between three apps, ask Claude Code "what was I just
  doing?", answer names those apps.
- Out of scope: screen text, models, redaction.

## Slice 2: Screen text capture and keyword recall

- [x] Rust: on focus change, click, scroll stop, typing pause (debounced),
      and a 5-second idle fallback, walk the accessibility tree of the focused
      window and store its text in table `frames`. Skip a frame if its text
      hash equals the previous one.
- [x] Rust: store browser URL when the focused app is a browser.
- [x] Python: FTS5 index over `frames.text`; MCP tool
      `recall(query, limit)` returns matching snippets with app, title, time.
- **Done when:** read a web page, then `recall("<phrase from the page>")`
  returns it. A test covers the dedupe rule.
- Out of scope: screenshots, OCR, distillation.

## Slice 3: Privacy controls

User control is a product promise, so it lands before any model sees data.

- [x] Rust: global shortcut toggles pause/resume; menu bar or notification
      shows state. Paused means zero rows written.
- [x] Rust: app denylist in config (password managers by default), skip
      private/incognito browser windows, skip secure text fields.
- [x] Python: redaction pass for secrets (API keys, tokens, private keys,
      card-number patterns) applied before text is indexed or sent to a model.
- [x] CLI: `deepmem pause`, `deepmem resume`, `deepmem status`,
      `deepmem forget --last 1h`, `deepmem forget --all`.
- **Done when:** tests prove a fake API key never reaches `frames` FTS, a
  denylisted app produces no rows, and `forget --last 1h` removes rows and
  index entries.
- Out of scope: encryption at rest (Slice 10).

## Slice 4: Episode distillation

The core of the product.

- [x] Python: sessionizer groups frames/events into sessions by app, window
      and a time gap (configurable, default 5 minutes idle). No model.
- [x] Python: for each closed session, call Ollama with a JSON-schema
      response format and produce an episode: summary, what was done,
      decisions, open items, entities (files, URLs, repos), time range.
      Validate the JSON; retry once; drop on second failure and log it.
- [x] Set `num_ctx` explicitly; chunk sessions that exceed it.
- [x] Write each episode as Markdown with front matter (time range, source
      session id, apps). Index episodes in FTS5.
- [x] `recall` searches episodes first, raw frames second.
- [x] Runs on a timer, only when on power or idle (config flag to override).
- [x] Prompt treats captured text as untrusted data; episodes record facts
      about what the user did, never instructions found on screen.
- **Done when:** after one hour of normal work, `~/.deepmem/memories/`
  contains readable episode files and `recall` returns them. A test runs the
  distiller on a fixture session with a stubbed model.
- Out of scope: vision, consolidation, project scoping.

## Slice 4b: Desktop window (added on user request)

A window in the Rust menu bar app: native window, UI in an embedded
WKWebView, no network port.

- [x] Rust: `api.rs`, the window's requests as JSON in and out (status,
      pause, memory list / search / read / delete, settings), no AppKit.
- [x] Rust: `config.rs`, rules and settings in `config.toml`; saving keeps
      comments and unknown keys; invalid values are refused.
- [x] Rust: `url_blocklist`. Nothing is stored for a blocked page; the focus
      event is written only after the page's address is known.
- [x] Rust: `config.toml` is reloaded when it changes, no restart.
- [x] Rust: `window.rs` + `ui.html`, three tabs (Memories, Blocked sites,
      Settings); menu item "Open deep-memory…"; `--window` flag.
- [x] The daemon never records its own window.
- **Human:** block a site, open it and an allowed site in a browser, check
  that only the allowed one produces rows (needs Accessibility permission).
- **Done when:** tests cover URL matching, settings round trip, memory
  delete path checks; the window lists, searches and deletes memories and
  saves settings.
- Out of scope: forget from the window, editing memories in the window,
  deleting earlier captures of a newly blocked site.

## Slice 5: Evaluation harness

Without this, later slices cannot show they improve anything.

- [ ] `deepmem export-fixture`: save a redacted slice of raw data as a
      fixture.
- [ ] Eval file format: question, expected facts, project.
- [ ] `deepmem eval`: run distillation plus `recall` over fixtures, score
      whether expected facts appear, print a table. Model name is a
      parameter, so models can be compared.
- [ ] Seed with at least 15 questions written by the user from real work.
- **Human:** write the seed questions.
- **Done when:** `deepmem eval --model <a>` and `--model <b>` print
  comparable scores.

## Slice 6: Project scoping

- [ ] Assign each session to a project: git repository root derived from
      editor window title, terminal working directory, or matching URL
      (repository host). Unmatched sessions go to `personal`.
- [ ] Memories are written under `memories/<project>/`.
- [ ] MCP: server resolves the calling agent's project from its working
      directory. `recall` and `current_context` return only that project's
      memories by default. `personal` is never returned unless the user
      allows it in config.
- **Done when:** a test shows an agent in repo A cannot retrieve an episode
  from repo B or from `personal`.

## Slice 7: Vision fallback

- [ ] Rust: when the accessibility tree returns empty or near-empty text,
      capture a screenshot of the focused window (JPEG, downscaled) to
      `~/.deepmem/frames/`.
- [ ] Python: send those frames to the vision model for a description plus
      visible text; store the result as the frame's text. Batch under the
      same power/idle rule as Slice 4.
- [ ] Denylist, pause and redaction from Slice 3 apply to screenshots.
      Delete the JPEG after it is described (config flag to keep).
- **Human:** grant Screen Recording permission.
- **Done when:** a canvas app or image-only window produces a frame with
  useful text, and `deepmem eval` includes at least three vision questions.
- Out of scope: vision on frames that already have accessibility text.

## Slice 8: Consolidation and retention

- [ ] Nightly job merges episodes into durable per-project files:
      `facts.md` (project state, decisions), `preferences.md` (how the user
      works). The model receives the existing file plus new episodes and
      returns the full updated file, so stale facts are replaced, not
      appended.
- [ ] Every fact keeps a pointer to its source episodes and a last-confirmed
      date.
- [ ] Retention: delete raw frames after N days (default 7), episodes after
      M days (default 90). Durable files stay.
- [ ] User edits to Markdown files are respected: reindex on file change;
      the consolidator never overwrites a line the user marked as pinned.
- **Done when:** a fixture with a contradicting later episode yields one
  updated fact, not two; a test covers retention and pinning.

## Slice 9: Structured sources

- [ ] Ingest git activity (commits, branch switches) for repositories the
      user has been seen working in.
- [ ] Ingest shell history (zsh) with timestamps, through the same redaction
      pass.
- [ ] Sessionizer merges these into sessions by time and project.
- **Done when:** an episode from a coding session names the commits made
  during it, and eval scores do not drop.

## Slice 10: Agent integration and install

- [ ] Claude Code session-start hook that injects a short
      `current_context()` summary for the current project, so memory is used
      without the agent deciding to ask.
- [ ] `deepmem install`: launchd agents for the daemon and the distiller,
      MCP registration snippet, permission checklist. `deepmem uninstall`
      removes everything, with an option to keep or delete data.
- [ ] Encryption at rest for the database and memory directory, key in the
      macOS Keychain.
- **Done when:** on a clean user account, `deepmem install` plus the
  permission grants gives a working system after reboot.

## Slice 11: Optional remote model

- [ ] Config option for an OpenAI-compatible endpoint and user-supplied key
      for distillation only. Off by default. `deepmem status` shows clearly
      when data can leave the machine.
- **Done when:** `deepmem eval` runs against a remote model and the default
  install makes zero network calls (test asserts this).

---

## Later, not planned

Audio capture, Windows/Linux, vector search, exposing memory to non-coding
agents. Add when a real need shows up.

## Review

(Fill in after each slice: what shipped, what changed from the plan, eval
scores.)

### Slice 1 (done, except the in-agent demo)

- Python MCP server written and verified: 2 tests pass, `current_context`
  returns events over stdio from a sample database. Uses the `mcp` 2.x API
  (`MCPServer`, not `FastMCP`).
- Rust daemon compiles clean (cargo 1.99.1, installed with Homebrew). Run
  from the agent's shell it exits with the Accessibility permission message,
  as designed. Capture itself is **not yet observed working**: needs the
  human permission grant.
- Change from plan: the daemon polls focus every 500 ms instead of using
  notifications. It also records title changes inside one app (for example
  browser tab switches). Slice 2 replaces polling with AXObserver.
- First real run wrote zero rows: `AXFocusedApplication` on the system-wide
  element returned nothing. Fixed by taking the frontmost app from
  `NSWorkspace` (run loop pumped each poll) and asking only that app for its
  focused window title. Missing permission is now a warning; app names are
  still recorded. Verified without permission: one row with the app name,
  title empty, `AXError -25211` logged.
- Verified with permission granted: rows for Terminal, Google Chrome and
  Claude with full window titles; `current_context` over MCP stdio returns
  the same rows from the real database.
- Still open: register the MCP server in Claude Code and ask "what was I
  just doing?" in a new session (the tool call itself is already verified).

### Slice 2 (done)

- Verified: daemon builds with no warnings; Rust dedupe test passes; 6 Python
  tests pass (search, one result per window, incremental indexing, hostile
  query input, missing tables). Daemon starts without permission and creates
  both tables.
- Verified on real windows with permission: frames stored for Google Chrome
  (14.8 KB with URL), VS Code (Electron, with URL), Claude and Terminal.
  "Done when" met: `recall("walking skeleton")` returned the GitHub page that
  had been read, with URL and snippet; a nonsense query returned nothing.
- Observed, to handle in Slice 3: the daemon captures its own Terminal
  window, so its debug output creates a new frame every few seconds. The
  first frame of a Chromium window is small and has no URL, because the app
  builds its accessibility tree only after the first request.
- Changes from plan:
  - Triggers are inferred, not hooked. The daemon polls every 250 ms and uses
    the system idle timer to detect "input paused". This avoids an event tap,
    which would need the Input Monitoring permission. It cannot tell click,
    scroll and typing apart; all three mean "capture after 0.7 s of quiet".
  - URL comes from the `AXURL` of the first web area in the window, so it
    works for any browser and also for Electron apps, with no browser list.
  - The FTS index stores its own copy of the text (roughly doubles text
    storage) so Slice 3 can index redacted text.
  - Password fields are skipped already, ahead of Slice 3.
- Caps per frame: 5,000 nodes, depth 40, 50 KB text, 1.5 s. Long documents
  are truncated.
- The daemon sets `AXManualAccessibility` and `AXEnhancedUserInterface` on
  the focused app. Known risk: the second can slow window animations when a
  third-party window manager moves the app.
- Sessions in the Claude desktop app cannot edit the main checkout; work
  happens in the session worktree and is merged to `main` afterwards.

### Slice 3 (done; private-window detection unconfirmed in real browsers)

- Verified by tests: 2 Rust (dedupe; denylist and private-window rules),
  11 Python. Fake secrets of ten kinds never appear in the index, in `recall`
  results or in `current_context`. `forget --last 1h` removes raw rows and
  index entries, and the deleted text is no longer present in the database
  file bytes. CLI pause/resume/forget confirmation covered.
- Verified by running the daemon in a temporary home (no Accessibility
  permission): shortcut registration succeeds; `deepmem pause` stops rows,
  `deepmem resume` restarts them; a denylisted frontmost app produces no
  rows; a broken `config.toml` exits with code 1.
- Confirmed by the user: Ctrl+Option+Cmd+P pauses and resumes capture.
- Not verified: private-window detection in real browsers.
  Safari's private-window title marker is an assumption.
- Changes from plan:
  - Pause state is a flag file, so the shortcut and CLI share one source of
    truth and a pause survives restarts.
  - Shortcut is fixed (Ctrl+Option+Cmd+P), registered through Carbon hot
    keys, which need no permission. Not configurable yet.
  - State is shown by a notification sent through `osascript`, not a menu
    bar item.
  - `forget` drops and rebuilds the search index instead of deleting single
    entries, and asks for confirmation unless `--yes`.
  - Python code moved to `store.py`; `server.py` only declares MCP tools.
  - The daemon skips its own terminal window (title contains
    `deepmem-capture`).
- Gap closed (user decision: redact in Rust): the daemon now redacts
  title, URL and text before insert (`capture/src/redact.rs`), so secrets
  never reach disk. Python keeps the same rules for indexing, output and
  later sources. Both are tested against `testdata/redaction_*.txt` so the
  two rule sets cannot drift unnoticed. On first start the daemon redacts
  rows written by earlier builds once (`PRAGMA user_version = 1`) with
  secure delete and a WAL truncate. Verified on a seeded temporary database:
  2 rows cleaned, none of the ten fixture secrets left in the database files.
- Follow-up after user feedback ("cannot see the app in the menu bar,
  shortcut not working"): added a menu bar status item (`◉` recording, `⏸`
  paused) with Pause/Resume and Quit. The daemon is now an accessory
  NSApplication and pumps AppKit events each tick instead of
  `RunCurrentEventLoop`. Process-level smoke test passes; the user confirmed the menu bar item
  and the shortcut both work. Cause of the
  reported shortcut failure was not reproduced.

### Slice 4 (done; verified on real captures with a real local model)

- Verified: 20 Python tests pass (9 new): session splitting, compaction,
  episode file content, open sessions left alone, retry then drop, model
  outage leaves the session for later, secrets never in prompt or file,
  remote `ollama_url` refused, memory-first recall, edited files reindexed,
  forget removes memories.
- Not verified: output of a real model. Ollama is not installed on the
  development machine; installing it and pulling a 6.2 GB model needs the
  user's go-ahead. The "Done when" check (readable episodes after real work)
  is therefore still open, and episode quality is unknown.
- Changes from plan:
  - Sessions split on time only (idle gap, 30 minute cap), not per app or
    window: one piece of work moves between browser, editor and terminal.
    Inside a session, text is grouped per window.
  - Compaction before the model: repeated captures of a window are merged
    to unique lines, and partial lines from typing or streaming are dropped.
    Without this the real data (124 captures of one chat window, 1.2 MB)
    would not fit any context.
  - A session too large for the context is split into parts, one episode
    file per part.
  - Episodes go to `memories/_unsorted/` until Slice 6 assigns projects.
  - The memory index is rebuilt whenever any memory file changes, so user
    edits and deletions are picked up without a watcher.
  - "Timer" is `deepmem distill --watch` (sleep loop) for now; launchd in
    Slice 10.
  - `forget` also deletes memories whose time range falls in the period.
  - `loginwindow` (lock screen) added to the daemon's built-in denylist.
- Model: `qwen3-vl:8b` confirmed on ollama.com (6.2 GB). It is a thinking
  model; requests set `think: false`.
- First real runs (Ollama 0.40.16, `qwen3-vl:8b`, 100% GPU, ~40 tokens/s):
  - One real episode was produced from 15 minutes of activity in about 2
    minutes: specific and mostly accurate (commands, commit hashes, URLs),
    but its "Decisions" section listed actions, not decisions.
  - `qwen3-vl:8b` is the thinking build. It ignores `think: false` and the
    `/no_think` switch, puts 1,000+ tokens into hidden reasoning before any
    answer, and once ran past 4,000 tokens. With the reply capped at 1,500
    tokens every reply came back empty and all sessions were dropped.
  - Added: reply cap (`num_predict` 1500) and `maxItems` 10 per list.
  - Fixed by switching the default to `qwen3-vl:8b-instruct` (non-thinking
    build, 6.2 GB). Tiny prompt: valid JSON in 3 s. Real data: 30 minutes of
    activity (320 frames) became 3 episodes in 65 s.
- "Done when" met: readable episode files exist in `~/.deepmem/memories/`
  and `recall` returns them before raw screen text.
- Episode quality with the instruct model, read by hand:
  - Good: specific and mostly correct (commands, test counts, model names,
    open items such as "old daemon still running").
  - Wrong details seen: an invented branch name, a flag attributed to the
    wrong behaviour. Small-model errors; Slice 5 must measure their rate.
  - Speculative wording ("likely", "suggesting") appeared once; a stricter
    prompt rule removed it in the rerun.
  - Parts of one session are grouped by window, so they overlap in time and
    all carry the session's whole frame range.
  - Personal browsing becomes a memory like anything else. Slice 6 must keep
    it out of project-scoped recall.
- The thinking build `qwen3-vl:8b` is still on disk, unused (6.2 GB).

### Slice 4b: desktop window (done; blocklist not yet observed on a real browser)

- Verified by tests: 13 Rust (9 new): site normalising and matching
  (subdomains, path prefix, look-alike hosts), rules from `config.toml`,
  settings saved with comments and unknown keys kept, invalid values refused
  with nothing written, memory list / search / read, delete refused outside
  `memories/`, pause toggle, cleanup of rows for a blocked window.
  20 Python tests still pass; no Python change.
- Verified in a browser with a stand-in for the Rust side: the page lists,
  searches, opens and deletes memories (two-click delete), adds and removes
  sites and apps, shows the Rust side's error text, saves settings, toggles
  pause. Memory text containing HTML is shown as text.
- Verified on the real window (temporary home, debug log): the window opens
  with `--window`, the page's requests reach Rust and the replies reach the
  page (`memory_get` is only sent after the list reply arrived). While the
  window was frontmost no event row was written for it.
- Not verified: the capture loop's blocklist on a real browser. The test
  binary had no Accessibility permission, so no page address was read. Also
  not verified by me: Cmd+C / Cmd+V in the window's text fields.
- Changes from plan:
  - The newest memory (or best match) opens by itself, so the reading pane
    is not empty.
  - "Show in Finder" added, since editing stays in the user's editor.
  - `toml` crate replaced by `toml_edit` for reading too (one parser).
  - Once a window is known to show a blocked site it stays blocked until a
    later read shows another address, so a read that finds no address does
    not unblock it.
- Known gap: a page whose address is unknown on the first read can be
  stored for a moment and is then deleted (see README, Known limits).
