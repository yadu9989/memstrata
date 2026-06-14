# MemStrata

**A local-first, verification-first context engine for AI workflows.**

MemStrata sits between you and every AI tool you use. It captures conversations
from the browser, indexes your codebase locally, and serves a context block to
whichever LLM you're talking to next. Your code stays on your machine. Your
conversation history stays on your machine. No telemetry leaves the box.

This repository is the **open-source core**: the local daemon, the chat-capture
browser extension, the MCP server, and the dashboard. It is MIT-licensed and
fully usable on its own.

The commercial Pro tier (token-budgeted context injection through a proxy
harness, money-back-guaranteed savings, IDE integration) lives in a separate,
private repository and consumes this package as a PyPI dependency. See
[memstrata.dev](https://memstrata.dev) if you want the paid product.

---

## What's in this repo

| Path | What it does |
|---|---|
| `memstrata/layer3/api_server.py` | The local daemon's FastAPI app — telemetry, dashboard, MCP routing |
| `memstrata/layer3/ingestion/` | File watcher, tree-sitter chunker, opt-in lifecycle, denylists |
| `memstrata/layer3/mcp_server.py`, `mcp_app.py` | MCP server (Anthropic-spec) for Claude Desktop / Cursor / etc. |
| `memstrata/layer3/_db.py` | SQLite schema with `sqlite-vec` for local vector search |
| `memstrata/layer3/retrieval.py` | Token-budgeted context retrieval against the local store |
| `memstrata/layer3/pricing/` | Live OpenRouter price sync + bundled static fallback for offline use |
| `memstrata/layer3/ollama_health.py` | Shared Ollama reachability probe (used by the dashboard) |
| `memstrata/workers/embedding_worker.py` | Background worker that embeds new turns into the vector store |
| `memstrata/cli/` | The `memstrata` CLI: `register`, `ingest`, the cd-hook generator |
| `memstrata/config/keychain.py` | OS keyring wrapper for storing per-provider API keys |
| `browser-extension/` | Chrome / Edge / Firefox extension that captures chat turns from every major LLM front-end |
| `migrations/` | SQL migrations |
| `shared/telemetry_schema.json` | The JSON schema for telemetry events (public contract) |

---

## Quickstart

```bash
pip install memstrata
python -m memstrata.cli.main daemon start
```

The daemon binds to `127.0.0.1:8000`. Open `http://127.0.0.1:8000/dashboard`
to see what's captured.

Install the browser extension from your browser's add-on store (see the
[browser-extension/](browser-extension/) directory for build instructions
if you want to load it unpacked).

---

## Architecture commitments

These aren't aspirational — they're enforced in the code and tested for in CI:

1. **Localhost-only binding.** Every HTTP server in this repo hard-codes
   `host="127.0.0.1"`. No `0.0.0.0`, no LAN exposure, no remote access.

2. **No TLS interception.** The MCP server and the dashboard speak plain
   HTTP on loopback. The browser extension talks directly to provider
   APIs and to this daemon's loopback endpoint. There is no MITM proxy
   in the open-source stack.

3. **Local storage only.** All telemetry, all chat history, all vectors,
   all API keys live in `~/.memstrata/` (or `$ML_DATA_DIR` if set).
   Nothing is uploaded to a MemStrata-owned cloud service. There is no
   such service.

4. **Telemetry never includes user content.** The dashboard and the
   MCP server expose your data back to you. Nothing is sent off-machine.

5. **The Pro tier is structurally separate.** Pro code lives in a
   different repository under a different license. This repo has
   zero `import` statements that touch Pro code.

---

## Provider pricing (for the dashboard's savings calculator)

The dashboard's session-level savings columns compute against a price
table. By default the daemon syncs the table once per day from
[OpenRouter](https://openrouter.ai/api/v1/models). When the network is
down or OpenRouter is unreachable, the bundled
`memstrata/layer3/pricing/pricing_matrix.json` provides a static
fallback. The fallback covers the most common Claude, OpenAI, Gemini,
DeepSeek, xAI, and Mistral models. Prices in the fallback are
USD-per-million-tokens, last verified mid-2026.

---

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md). The short version: open issues
first for non-trivial changes, run `pytest` before submitting, and keep
the architectural commitments above intact.

## Security

See [SECURITY.md](SECURITY.md). Vulnerability reports go to
`security@memstrata.dev`, **not** GitHub issues.

## License

[MIT](LICENSE). See `LICENSE` for the full text.

## Related

- **Commercial Pro tier**: [memstrata.dev](https://memstrata.dev) — the
  token-budgeting interception harness, the money-back guarantee, and
  the IDE extension. Proprietary, paid, separate codebase.
- **Browser extension store listings**: shipped from the same source
  tree in this repo; see [browser-extension/README.md](browser-extension/)
  for build + sideload instructions.
