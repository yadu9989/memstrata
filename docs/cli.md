# Command-line interface

`memstrata` is the console script installed by `pip install memstrata`.
It's the single entry point for every operation the daemon needs:
first-run setup, project registration, ingestion, the API server, and
the shell cd-hook.

This document describes every subcommand, every flag, what each one
writes to disk, and the precise exit behaviour. Keep it in sync with
[`memstrata/cli/main.py`](../memstrata/cli/main.py) when you add a new
command.

---

## Quick reference

```
memstrata init                  # interactive 4-question onboarding
memstrata register <path>       # register a project directory
memstrata ingest <path>         # walk a project and embed source files
memstrata api                   # start the FastAPI daemon (binds 127.0.0.1)
memstrata uninit-cd-hook        # remove the shell cd-hook
```

Every subcommand exits `0` on success and a non-zero status on
failure. Errors print to `stderr`; success output prints to `stdout`.
Subcommands that have a `--quiet` flag (currently just `register`)
suppress stdout entirely when set, leaving only stderr for errors.

The CLI binds nothing remote, never phones home, and never touches
your provider API traffic. See [`hard-rules.md`](hard-rules.md) Rule 1.

---

## `memstrata init`

Interactive 4-question onboarding wizard. Runs the first time a user
installs `memstrata`. Idempotent — running it again replaces the
prior choices.

```bash
memstrata init [--enable-cd-hook]
```

### The four questions

**[1/4] Data directory.** Where should MemStrata store its database
and vectors? Default is `~/.memstrata/`. The directory is created if
it doesn't exist. You can override later via the `ML_DATA_DIR`
environment variable.

**[2/4] Base model.** Three choices:
- `[1] Local (Ollama)` — recommended; zero cost; uses your locally-installed Ollama for embeddings and inference.
- `[2] Anthropic API (Claude)` — prompts for a Claude API key, validates it against `api.anthropic.com/v1/models`, stores it in the OS keychain.
- `[3] OpenAI API (GPT-4/o-series)` — same flow against `api.openai.com/v1/models`.

API keys are NEVER written to disk by this CLI. The keychain wrapper
(`memstrata.config.keychain`) hands them to Windows Credential Manager,
macOS Keychain, or Linux secret-service. If the keychain isn't
reachable (some Linux distros without dbus), the CLI prints a warning
and continues without storing the key — you'll need to set the
provider's standard env var (`ANTHROPIC_API_KEY` / `OPENAI_API_KEY`)
yourself.

**[3/4] Shell cd-hook.** Optional but recommended. The cd-hook
registers a project automatically the first time you `cd` into a git
repository, so you don't have to remember to run `memstrata register`
by hand. Pass `--enable-cd-hook` to skip the prompt and enable
directly.

**[4/4] Shell selection.** If the cd-hook is enabled, pick which
shell config to write to. The CLI auto-detects from `$SHELL` and
pre-selects:
- `zsh` → `~/.zshrc`
- `bash` → `~/.bashrc`
- `fish` → `~/.config/fish/config.fish`
- `powershell` → `$PROFILE`

Choose "Skip — I'll register projects manually" to bail without
modifying any shell file.

### What `init` writes to disk

- The data directory (`mkdir -p`)
- An OS keychain entry for the provider key (when chosen)
- A backup of the shell config (`.zshrc.ml-backup`, etc.) before
  inserting the hook block
- The hook block itself, delimited by `# >>> memstrata cd-hook >>>`
  / `# <<< memstrata cd-hook <<<` markers for idempotent rewrite

### Removing it

`memstrata uninit-cd-hook` (below) removes the shell hook. The
keychain entry and data directory stay until you delete them
manually — we don't touch user data without an explicit ask.

---

## `memstrata register <path>`

Register a project directory with the local daemon. Idempotent: re-running
on an already-registered path is a no-op.

```bash
memstrata register <path> [--quiet]
```

### Arguments

| Arg / flag | Effect |
|---|---|
| `<path>` | Required. Path to the project directory. Relative paths are resolved against the current working directory. |
| `--quiet`, `-q` | Suppress stdout. Errors still go to stderr. The shell cd-hook uses this to avoid spamming the prompt every time you `cd`. |

### Behaviour

1. Resolves `<path>` to an absolute path.
2. Checks the path exists. If not, prints to stderr and exits 1.
3. Checks for `.git/` under the path. If missing, prints a note that
   the path isn't a git repository and **exits 0 without registering**
   (the cd-hook calls this for every `cd`, and we don't want to register
   `~/` or similar).
4. Otherwise: registers the path with the daemon and prints
   `Registered: <path>` (suppressed under `--quiet`).

Registration is local-only — the daemon writes a row associating the
project's basename with its absolute path. No network call. Removing
the registration is currently a manual SQL operation; a `memstrata
unregister` subcommand is on the roadmap.

### Exit codes

- `0` — success, or path-is-not-a-git-repo (intentional no-op)
- `1` — path doesn't exist

The cd-hook uses `--quiet` so a non-existent directory doesn't blow
up the prompt; it just silently fails.

---

## `memstrata ingest <path>`

Walk a project tree, chunk every source file via tree-sitter, embed
the chunks against Ollama, and write the vectors into the local
SQLite store. This is the bulk-ingest path; the file watcher
(running inside the daemon) handles incremental updates afterward.

```bash
memstrata ingest <path> [--project-id ID] [--no-embed]
```

### Arguments

| Arg / flag | Effect |
|---|---|
| `<path>` | Required. Project root to ingest. `.` for the cwd. |
| `--project-id ID` | Override the project_id stored against each chunk. Defaults to the basename of `<path>`. |
| `--no-embed` | Skip the Ollama embedding step. Stores text chunks only. Use when Ollama is offline; you can re-run later without `--no-embed` to backfill the vectors. |

### Behaviour

1. Initializes the local SQLite database if it doesn't exist.
2. Walks the project tree, respecting the denylist (`memstrata/layer3/ingestion/denylist.py`) — `.git/`, `node_modules/`, `__pycache__/`, build outputs, etc.
3. For each file with a recognized tree-sitter grammar (Python, JavaScript, TypeScript, more languages added over time):
   - Parses the AST
   - Splits into chunks at function/class boundaries
   - Computes a content hash for incremental updates
4. For each chunk:
   - If `--no-embed`: stores the chunk text only
   - Otherwise: sends the chunk to Ollama for embedding via `nomic-embed-text`, writes the vector to the `sqlite-vec` virtual table
5. Prints progress to stdout. Errors per-file are logged but don't
   abort the run; one bad file shouldn't waste the whole ingest.

### Output

Per-run summary lines like:

```
Ingested: 1,247 files / 8,394 chunks
Embedded: 8,221 chunks (173 skipped — see logs)
Elapsed: 2m 14s
```

If you run with `--no-embed` followed later by a normal run, the
second run picks up only the chunks that need embedding (idempotent
via content hash).

### Failure modes

| Symptom | Cause | Fix |
|---|---|---|
| `ConnectionRefusedError: localhost:11434` | Ollama not running | Start Ollama, or run with `--no-embed` to defer |
| Tree-sitter grammar missing for a file type | Grammar not installed | Skip silent; file is logged but not ingested. Install the grammar package and re-run. |
| `OperationalError: database is locked` | Concurrent ingest + daemon | Stop the daemon first, or wait for it to release the connection |

---

## `memstrata api`

Start the FastAPI server that hosts the dashboard, the MCP endpoint,
the telemetry routes, and the background workers.

```bash
memstrata api [--port PORT]
```

### Arguments

| Arg / flag | Effect |
|---|---|
| `--port PORT` | Port to bind on `127.0.0.1`. Default `8000`. |

There is intentionally **no `--host` flag**. The server binds strictly
to `127.0.0.1` — see [`hard-rules.md`](hard-rules.md) Rule 1.

### Behaviour

1. Imports the FastAPI app from `memstrata.layer3.api_server`.
2. Runs uvicorn with `host="127.0.0.1"`, `port=<port>`, default log level.
3. Stays in the foreground; Ctrl-C shuts the daemon down cleanly via
   the lifespan context.

For "run as a service" patterns (systemd unit, launchd plist, Windows
service), see the [Pro tier](https://memstrata.dev). The Open repo
intentionally doesn't bundle service-installer scaffolding — the
running pattern is "start it from your terminal or a tmux pane."

### Once running

- Dashboard: `http://127.0.0.1:8000/dashboard`
- MCP endpoint: `http://127.0.0.1:8000/mcp` (see [`mcp-server.md`](mcp-server.md))
- Telemetry POST endpoint: `http://127.0.0.1:8000/telemetry/session` (called by the browser extension)
- API endpoints under `/api/dashboard/*`, `/context/*`, `/baseline/status`

### Background workers

When the lifespan starts, three background asyncio tasks come up:
- **Ollama health probe** — polls `localhost:11434` every 30s while
  not-ready, then 5min once ready.
- **OpenRouter pricing sync** — refreshes `provider_pricing` table
  every 24h (skipped if the network is down).
- **Embedding worker** — drains the embedding queue and writes
  vectors to `sqlite-vec`.

Each catches every exception inside its loop body — Rule 7 of
[`hard-rules.md`](hard-rules.md).

---

## `memstrata uninit-cd-hook`

Remove the shell cd-hook block from your shell config file. Inverse
of the `init` flow's step 3/4.

```bash
memstrata uninit-cd-hook [--shell {bash,zsh,fish,powershell}]
```

### Arguments

| Arg / flag | Effect |
|---|---|
| `--shell SHELL` | Which shell config to clean. Auto-detected from `$SHELL` if omitted. |

### Behaviour

1. Auto-detects (or accepts) the shell.
2. Locates the corresponding rc file (`.zshrc`, `.bashrc`, etc.).
3. Strips the block delimited by `# >>> memstrata cd-hook >>>` /
   `# <<< memstrata cd-hook <<<`.
4. Prints `Removed memstrata cd-hook from <path>`.

If the markers aren't present (no hook installed), the command exits
0 with no changes — idempotent.

### What's left after `uninit-cd-hook`

Just the hook lines are removed. Your other shell config stays
untouched. Your registered projects remain in the database. To wipe
the database entirely:

```bash
rm -rf ~/.memstrata/
```

---

## The shell cd-hook (what `init` writes)

The hook block looks like this (zsh shown — bash/fish/powershell are
shaped similarly):

```bash
# >>> memstrata cd-hook >>>
ml_cd_hook() {
    if [ -d ".git" ] && command -v memstrata >/dev/null 2>&1; then
        (memstrata register "$PWD" --quiet >/dev/null 2>&1 &)
    fi
}
typeset -gaU chpwd_functions
chpwd_functions+=(ml_cd_hook)
# <<< memstrata cd-hook <<<
```

What it does:
- Fires on every directory change (zsh `chpwd_functions`; bash uses
  `PROMPT_COMMAND`; fish uses `function --on-variable PWD`).
- Only acts when the new directory contains a `.git/` (skipping
  non-repo dirs avoids spurious registrations).
- Runs `memstrata register` in the background with `--quiet` so the
  prompt doesn't pause.
- Silently drops any output. If `memstrata` isn't on PATH (uninstalled,
  pipx isn't loaded, etc.), the hook is a no-op.

Idempotent install: `init` rewrites the block between the markers
every time, so re-running `init` won't pile up duplicate copies.

---

## Environment variables the CLI respects

| Var | Effect |
|---|---|
| `ML_DATA_DIR` | Override the data directory. Default `~/.memstrata/`. |
| `ML_DB_PATH` | Override the database file path specifically. Takes precedence over `ML_DATA_DIR`. Mostly used by the test suite. |
| `ANTHROPIC_API_KEY` | Used as the Anthropic key when the keychain doesn't have one. |
| `OPENAI_API_KEY` | Same for OpenAI. |

The legacy `ML_*` env-var naming pre-dates the rename to `memstrata`.
We kept the names for backward compatibility with users coming from
older builds; a `MEMSTRATA_*` alias may land in a future release.

---

## Exit codes

| Code | Meaning |
|---|---|
| `0` | Success (or intentional no-op like "this isn't a git repo, skipping") |
| `1` | Caller-visible error: bad arguments, path missing, dependency not installed |
| `2` | Argparse-level error (typically wrong subcommand or missing positional arg) |

The CLI never raises an uncaught exception out to the user — Rule 5
of [`hard-rules.md`](hard-rules.md). If you see a Python traceback,
that's a bug; file it.

---

## See also

- [`mcp-server.md`](mcp-server.md) — once the daemon is running, how to
  point Claude Desktop / Cursor at it
- [`browser-extension.md`](browser-extension.md) — the other half of
  the data ingestion story
- [`hard-rules.md`](hard-rules.md) — the architectural commitments
  that govern what the CLI is and isn't allowed to do
