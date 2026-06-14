# MCP server

MemStrata exposes its local store via the Model Context Protocol, the
same JSON-over-HTTP protocol Claude Desktop, Cursor, and other IDE
clients use to load context from external sources.

The MCP server is implemented in
[`memstrata/layer3/mcp_app.py`](../memstrata/layer3/mcp_app.py),
mounted at `/mcp` of the local daemon. It speaks **Streamable HTTP**
(the modern MCP transport — request/response over a single POST per
call) and runs entirely on `127.0.0.1`.

---

## What it exposes

Five tools, all read-only against the local SQLite database:

| Tool | What it does |
|---|---|
| `get_context` | Returns a token-budgeted context block for the given project, optionally scoped to a specific chat session. The same retrieval path that the daemon's `/context/*` HTTP routes use. |
| `list_chat_sessions` | Lists the chat sessions captured by the browser extension, with provider, title (when available), turn count, and last-seen timestamp. Filterable by provider. |
| `get_chat_history` | Returns the captured turns for a specific chat session, in order. Each turn carries role, content, and timestamp. |
| `search_memory` | Vector search over captured turns. Takes a query string and returns the top-K matching turns with their session metadata. |
| `get_dashboard_stats` | Summary aggregates: turn counts, injection rates, cache-hit rates, token totals. The same numbers the local dashboard renders. |

No write tools. No "delete session" tool. The MCP surface is
deliberately read-only — if you want to mutate the store, use the
HTTP API or the CLI.

---

## Starting the server

The MCP server runs as a sub-app of the main daemon. Starting the
daemon starts MCP automatically:

```bash
python -m memstrata.cli.main daemon start
# or, equivalently, if you've installed the `memstrata` console script:
memstrata daemon start
```

Once the daemon is up, MCP is at `http://127.0.0.1:8000/mcp`.

To verify it's listening:

```bash
curl -s http://127.0.0.1:8000/mcp \
  -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{}}' \
  | python -m json.tool
```

You should see the five tools above.

---

## Configuring clients

### Claude Desktop

Edit `~/Library/Application Support/Claude/claude_desktop_config.json`
(macOS) or `%APPDATA%\Claude\claude_desktop_config.json` (Windows):

```json
{
  "mcpServers": {
    "memstrata": {
      "url": "http://127.0.0.1:8000/mcp"
    }
  }
}
```

Restart Claude Desktop. The five tools appear under the tools menu.

### Cursor

Open Cursor's settings → MCP → "Add new MCP server":

- **Name:** `memstrata`
- **Type:** Streamable HTTP
- **URL:** `http://127.0.0.1:8000/mcp`

Save. Cursor will probe the server immediately; you'll see the tool
list populate.

### Anthropic CLI

```bash
claude mcp add --transport http memstrata http://127.0.0.1:8000/mcp
```

### Other clients

Anything that speaks MCP Streamable HTTP works. The MCP SDK is
provider-agnostic; we don't do anything Anthropic-specific.

---

## Security posture

The MCP server inherits Rule 1 of [`hard-rules.md`](hard-rules.md):
it binds to `127.0.0.1` only. Two additional protections apply:

1. **DNS-rebinding protection.** The MCP SDK rejects requests whose
   `Host:` header isn't an explicit loopback. We allow:
   - `localhost`, `127.0.0.1`, `0.0.0.0` (bare)
   - `localhost:*`, `127.0.0.1:*`, `0.0.0.0:*` (with any port)
   - `testserver` (the Starlette TestClient default)

   A request from a public hostname pointing at your loopback gets a
   421. This blocks the classic "malicious website rebinds DNS to
   point at your localhost" attack.

2. **No authentication.** The local daemon assumes anyone with access
   to your loopback interface is you. If you're sharing a workstation
   with another user account, the OS user separation is what's
   protecting your MemStrata data — there's no token to revoke if
   you're worried about a co-tenant.

If you need authenticated MCP for a shared-workstation use case,
file an issue. We've discussed it but haven't implemented anything;
it requires a key-management story we don't want to ship without
careful design.

---

## What the tools actually return

### `get_context`
Input: `project_id` (string), optional `chat_session_id` (string),
optional `token_budget` (integer, default 4096).

Output:
```json
{
  "context_block": "...formatted markdown context block...",
  "tokens_used": 3812,
  "tokens_budget": 4096,
  "sources": [
    {"chat_session_id": "...", "turn_count": 14},
    ...
  ]
}
```

### `list_chat_sessions`
Input: optional `provider_id` (string filter), optional `limit`
(integer).

Output:
```json
{
  "sessions": [
    {
      "id": "01HQX...",
      "provider_id": "anthropic",
      "external_session_id": "abc-123-...",
      "title": "Refactoring the API server",
      "turn_count": 47,
      "last_seen": "2026-06-13T14:22:09Z"
    },
    ...
  ]
}
```

### `get_chat_history`
Input: `chat_session_id` (string), optional `limit`, optional `offset`.

Output:
```json
{
  "session_id": "01HQX...",
  "turns": [
    {"role": "user", "text": "...", "recorded_at": "..."},
    {"role": "assistant", "text": "...", "recorded_at": "..."},
    ...
  ]
}
```

### `search_memory`
Input: `query` (string), optional `top_k` (integer, default 10),
optional `provider_id` (filter).

Output:
```json
{
  "results": [
    {
      "turn_id": 4172,
      "chat_session_id": "01HQX...",
      "provider_id": "anthropic",
      "role": "assistant",
      "text": "...",
      "score": 0.87
    },
    ...
  ]
}
```

`score` is cosine similarity in `[0, 1]`. Higher is more relevant.
Requires `sqlite-vec` to be available; without it, this tool returns
an empty result list.

### `get_dashboard_stats`
Input: optional `window` (string: `'24h'`, `'7d'`, `'30d'`, `'all'`).

Output:
```json
{
  "sessions": 124,
  "turns": 1872,
  "injected_turns": 901,
  "cache_hit_turns": 312,
  "total_input_tokens": 3294819,
  "total_output_tokens": 587302,
  "injection_rate_pct": 48,
  "cache_hit_rate_pct": 17,
  "savings_pct": 12
}
```

Same numbers the local dashboard renders. If the database is fresh
(no captures yet), every value is `0`.

---

## Troubleshooting

**"Server not found" in Claude Desktop / Cursor.** Check the daemon
is actually running: `curl http://127.0.0.1:8000/` should return
JSON with a version. If the daemon is up but the MCP endpoint
returns 404, the MCP mount is broken — check the daemon's startup
logs for an MCP initialization error.

**Tools list is empty.** The MCP SDK requires the `FastMCP` instance
to be fully initialized before the first request. Race conditions
during daemon startup can produce an empty tool list on the first
request; restart Claude Desktop / Cursor or hit the daemon's
`/` endpoint first to confirm full startup.

**Tool returns `"sqlite-vec not loadable"`.** The `sqlite-vec`
extension didn't ship with your Python install. Reinstall with
`pip install sqlite-vec>=0.1.6` to get the precompiled wheel, or
check the daemon's startup log for the exact error.

**Searches return nothing even though the dashboard shows captures.**
Embeddings are written asynchronously — there's a queue between
capture and the vector store. Wait a few seconds after a new
capture, or check `/api/dashboard/sessions` to see the per-session
embedding count.
