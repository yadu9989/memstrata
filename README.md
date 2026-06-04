# Memory Layer Pro — Harness

Active-interception proxy that sits between your coding agent and the LLM provider.
Every request gets your project's context block injected automatically.
Every response is counted and written to the session timeline.

```
Your agent  →  localhost:8080  →  OpenAI / Anthropic / Ollama
                (harness)
                  ↕ fetch context
              memory-layer core
              (MIT, runs separately)
```

---

## Requirements

- Python 3.11+
- [Memory Layer core](https://github.com/your-org/memory-layer) running on `:8000` (MIT package, installed separately)
- `pipx` for isolated installation

---

## Installation

```bash
# 1. Install the harness
pipx install memory-layer-pro

# 2. Register as an OS autostart service (runs on every login)
memory-layer-pro install

# 3. Confirm it is running
memory-layer-pro status
```

To run in the foreground instead (useful for debugging):

```bash
memory-layer-pro start
```

The harness listens on `http://localhost:8080` by default.

---

## Configuration

On first run, create `~/.memory-layer-pro/harness.toml`:

```toml
[harness]
listen_port = 8080
log_level   = "info"   # "debug" for verbose output

[memory_layer]
core_url = "http://localhost:8000"   # where the MIT core is running
api_key  = "${MEMORY_LAYER_API_KEY}" # optional; env-var expansion supported

[provider.openai]
upstream_url = "https://api.openai.com"

[provider.anthropic]
upstream_url = "https://api.anthropic.com"

[provider.ollama]
upstream_url = "http://localhost:11434"
```

> API keys for OpenAI/Anthropic are **never** stored here. They are forwarded
> transparently from whatever your coding agent passes in the `Authorization`
> header. See [NON_FEATURES.md](NON_FEATURES.md).

---

## Agent configuration

### Aider

Point Aider's OpenAI-compatible endpoint at the harness:

```bash
aider \
  --openai-api-base http://localhost:8080/v1 \
  --openai-api-key  $OPENAI_API_KEY \
  --model gpt-4o
```

For streaming (recommended):

```bash
aider \
  --openai-api-base http://localhost:8080/v1 \
  --openai-api-key  $OPENAI_API_KEY \
  --model gpt-4o \
  --stream
```

### Claude Code

Claude Code uses the Anthropic message shape (`/v1/messages`).
Set `ANTHROPIC_BASE_URL` before starting the CLI:

```bash
export ANTHROPIC_BASE_URL=http://localhost:8080
claude
```

Or add it to your shell profile so it applies automatically:

```bash
# ~/.bashrc or ~/.zshrc
export ANTHROPIC_BASE_URL=http://localhost:8080
```

Claude Code will continue to send your Anthropic API key in the
`x-api-key` header; the harness forwards it untouched to `api.anthropic.com`.

### Cline (VS Code extension)

1. Open the Cline extension settings (gear icon → "Settings").
2. Set **API Provider** to `OpenAI Compatible`.
3. Set **API Base URL** to `http://localhost:8080/v1`.
4. Set **API Key** to your `OPENAI_API_KEY` (or your Anthropic key if routing through OpenAI-compat shim).
5. Set **Model** to `gpt-4o` (or whichever model you use).
6. Save. Cline will now route all completions through the harness.

### Cursor

In Cursor's settings → Models → "Override OpenAI Base URL":

```
http://localhost:8080/v1
```

Leave the API key field as-is; Cursor sends it in the `Authorization` header
and the harness forwards it.

### Local Ollama

```bash
# Point any OpenAI-compatible agent at:
http://localhost:8080/api/chat    # Ollama chat shape
http://localhost:8080/api/generate
```

The harness proxies to `http://localhost:11434` by default. Configure a
different Ollama host in `harness.toml` under `[provider.ollama]`.

---

## Session headers (optional)

The harness assigns a random `session_id` per agent process. To pin a session
across multiple tool invocations (e.g. in a CI workflow):

```
X-Session-ID: my-ci-run-42
X-Project-ID: my-project
```

`X-Project-ID` must match a project registered in the Memory Layer core.
Defaults to `"default"` when absent.

---

## Troubleshooting

### Connection refused on port 8080

```
Error: connect ECONNREFUSED 127.0.0.1:8080
```

**Cause**: the harness is not running.

```bash
memory-layer-pro status   # check if running
memory-layer-pro start    # start in foreground to see log output
```

If `start` fails immediately, check the log:

```bash
memory-layer-pro logs
```

### "Memory Layer core unreachable" in the harness log

```
[Memory Layer Pro] Memory Layer core unreachable at http://localhost:8000.
Start the core with: memory-layer api
```

**Cause**: the MIT core is not running.

```bash
# In a separate terminal (or as a service):
memory-layer api
```

Verify the core is reachable:

```bash
curl http://localhost:8000/health
```

If you run the core on a non-default port, update `core_url` in `harness.toml`.

### Upstream 401 — bad or missing API key

```
[Memory Layer Pro] Upstream returned 401 for https://api.openai.com/v1/chat/completions.
Memory Layer Pro does not store API keys.
Fix: check the API key in your agent's configuration, not in harness.toml.
```

**Cause**: the API key your coding agent is sending is invalid or expired.

- The harness never stores or modifies API keys (see [NON_FEATURES.md](NON_FEATURES.md)).
- Set the correct key in your agent (`OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, etc.).
- Verify it works by calling the provider directly:

```bash
curl https://api.openai.com/v1/models \
  -H "Authorization: Bearer $OPENAI_API_KEY"
```

### Request reaches the harness but context is not injected

Check the harness log for a `WARNING` or `ERROR` about the Memory Layer core.
Common causes:

| Log message | Fix |
|-------------|-----|
| `Memory Layer core unreachable` | Start the core (`memory-layer api`) |
| `core rejected the API key (HTTP 401)` | Set correct `api_key` in `harness.toml` |
| `HTTPStatusError: 5xx` | Core is running but erroring; check core logs |

If the core is healthy but context is still missing, add `log_level = "debug"`
to `harness.toml` and restart; you will see the injection decision
(`FRESH_FULL` / `SKIP` / `APPEND_DELTA`) logged for every request.

### Hash mismatch warnings in debug logs

```
InjectionMode: APPEND_DELTA (hash changed from dead0000 → abc123)
```

This is normal behaviour, not an error. The watcher detected a file change
mid-session; the harness appended a short delta message instead of
re-injecting the full block (Hard Rule 50 — APPEND_DELTA preserves the
prefix cache).

If you see FRESH_FULL unexpectedly on turn 3+ of a session, the session
may have gone idle for > 1 hour and was treated as stale. This is also
expected behaviour.

### "502 Bad Gateway" response from the harness

```json
{"error": {"message": "...", "type": "connection_error"}}
```

**Cause**: the harness could not reach the upstream provider (OpenAI, Anthropic,
or Ollama). The terminal shows:

```
[Memory Layer Pro] Could not reach upstream at https://api.openai.com/...
Check your network connection and the upstream_url in harness.toml.
```

Check:
1. Network connectivity to the provider.
2. The `upstream_url` under `[provider.<name>]` in `harness.toml`.
3. Firewall rules (corporate proxies sometimes block direct HTTPS).

---

## Uninstall

```bash
memory-layer-pro uninstall   # remove OS service
pipx uninstall memory-layer-pro
```

Configuration and session data in `~/.memory-layer-pro/` are not removed
automatically; delete that directory manually if you want a clean slate.

---

## Privacy

See [NON_FEATURES.md](NON_FEATURES.md) for a precise description of what
Memory Layer Pro does **not** do with your code, keys, or traffic.
