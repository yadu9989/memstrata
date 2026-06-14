# Architectural rules

This is the list of rules we don't break. They aren't preferences and
they aren't aspirations; they're the floor of what makes MemStrata
worth installing. New code that violates one of these will be rejected
on review regardless of how clever the implementation is.

The numbered list below is the **public, MIT-tier subset**. Each rule
applies to all code in this repository. Some are enforced by tests in
CI; some by code review; all are enforced eventually one way or
another.

---

### Rule 1 — Localhost-only binding

Every HTTP/WebSocket server in this repository binds strictly to
`127.0.0.1`. The CLI exposes no `--host` flag. The daemon refuses to
bind a non-loopback address even when explicitly asked. The
configuration loader has no key for it; adding one is rejected on
review.

The Ollama probe targets `localhost:11434`. The MCP server's
DNS-rebinding allowlist includes only `127.0.0.1`, `localhost`, and
`0.0.0.0` loopback variants (plus the Starlette TestClient synthetic
host for the test suite).

**Why it matters.** Even one route that binds `0.0.0.0` would put a
SQLite database with the user's chat history on the LAN. The cost of
fixing that mistake after the fact is unbounded; the cost of refusing
to do it in the first place is zero.

---

### Rule 2 — No TLS interception, no MITM

The browser extension talks directly to provider TLS endpoints
(`claude.ai`, `api.openai.com`, etc.). It does not install a root CA.
It does not register an HTTP proxy. It does not run a local
certificate authority.

The daemon serves plain HTTP on loopback and HAS NO outbound TLS
endpoint that user content flows through. The only outbound HTTPS the
daemon performs is `GET` to two public read-only endpoints (OpenRouter
prices, Bank of Canada FX) with empty bodies.

There is no `https_proxy` setting in MemStrata's configuration. There
is no "trust our custom CA" prompt. There is no certificate-pinning
override.

**Why it matters.** A local proxy that terminates TLS for
`api.anthropic.com` has, by construction, full visibility into the
user's prompts, completions, and API keys. We do not want that
capability in this codebase. (The commercial Pro tier solves the same
user-visible problem — context injection — in a different way that
also doesn't terminate TLS; see
[`repository-split.md`](repository-split.md).)

---

### Rule 3 — Local storage only

All data MemStrata produces — chat turns, code chunks, vectors,
pricing, settings, telemetry — lives in `~/.memstrata/` on the user's
machine. Override with `ML_DATA_DIR` if you want a different location,
or `ML_DB_PATH` for the database specifically.

We do not operate a backend that accepts user data. There is no
"sync your MemStrata across devices" feature. There is no "share
your context with your team" feature. Both have come up in feedback;
both are off-the-table for this repository.

If your feature needs a backend, it belongs in the commercial Pro
tier, where the backend's data handling is explicitly contracted with
the user.

---

### Rule 4 — Telemetry never includes user content

The daemon's outbound network calls are limited to two public
read-only endpoints (see Rule 2). They carry no prompt text, no
completion text, no code, no API keys, no machine identifiers, no
session IDs, no user IDs, no IP addresses (beyond what the network
layer can't help). They're plain HTTP GETs to URLs that don't accept
a request body.

We do not collect anonymized snippets. We do not collect aggregate
statistics. We do not collect crash reports that include local file
paths or buffer contents. If you find a code path that violates
this, treat it as a security bug (see [`../SECURITY.md`](../SECURITY.md)).

---

### Rule 5 — Defensive on every public surface

Every HTTP route, MCP handler, content-script entrypoint, and
background-task loop must catch broad exceptions and degrade
gracefully. Specifically:

- Background tasks (Ollama health probe, OpenRouter sync, embedding
  worker) catch `Exception` inside their loop body and continue.
  Raising into the lifespan would tear the whole daemon down, which
  is unacceptable for the typical user trying to keep a session
  alive.
- HTTP routes return structured error JSON for known failure cases
  and a generic 500 for unknown ones, NEVER an uncaught traceback.
- The browser extension's content script wraps every detector call
  in a try/catch. A malformed turn must not crash the page.

The exception to "catch everything" is your test code: there, narrow
exceptions are correct.

---

### Rule 6 — Open code never imports Pro code

This repository contains zero `from memstrata_pro import …`,
zero `from harness import …`, zero `from billing import …`. CI greps
for these on every PR and fails the build on a match.

The Pro tier consumes Open via PyPI (see
[`repository-split.md`](repository-split.md)). The dependency direction
is one-way and structural — if you find a reason you'd want Open to
reach into Pro, the design is wrong; rework so the Pro-only behavior is
injected via `app.state` instead.

The api_server's `_NoOpCohortApi` is a worked example: Open defines the
contract and a safe default, Pro overrides at startup.

---

### Rule 7 — Background tasks must never raise into the lifespan

The FastAPI lifespan is what keeps the daemon alive. A background
asyncio task that propagates an exception out of its loop will
terminate uvicorn and end the user's session.

This means: every background loop has `try/except Exception` inside
the loop body. The Ollama health probe (`_ollama_polling_loop`), the
OpenRouter pricing sync (`_pricing_sync_loop`), and the embedding
worker all follow this pattern.

If you add a new background task, the same shape applies. Catching
`Exception` here is correct; narrow exception handling is wrong.

---

### Rule 8 — Large downloads require explicit user consent

Any code path that triggers a download larger than 100 MB must ask
the user before proceeding. The local-AI model picker is the worked
example: the CLI prompts before pulling Ollama models, and the
dashboard's "Set up local AI…" link opens the consent flow instead
of starting the download immediately.

This isn't a bandwidth policy; it's a trust policy. The user has to
believe MemStrata isn't going to silently pull gigabytes onto their
machine. One unexpected multi-GB download breaks that trust forever.

---

## Things the rules don't say

A few clarifications, because each has come up in review:

- **Rule 1 doesn't forbid you from running the daemon under a tunnel
  yourself.** If you want to expose your local daemon over `ssh -L`
  or Tailscale, that's your call. The rule says we don't bind a
  non-loopback address by default and we don't offer a flag to do so.

- **Rule 3 doesn't forbid integrating with cloud LLM providers.**
  Talking to `api.openai.com` to actually answer a question is fine;
  the user obviously consents to that. The rule is about MemStrata's
  own data storage, not about LLM provider traffic.

- **Rule 4's "no user content" means literally that.** A traceback
  showing `KeyError: 'project_id'` is fine to log locally — there's
  no user content in it. A traceback showing `KeyError: 'my-private-key-...'`
  is not — sanitize it before logging.

- **Rule 6 applies to runtime imports, not to documentation.** This
  doc file mentions `memstrata_pro` constantly — that's text in
  a markdown file, not a Python import. The CI grep checks `.py` and
  `.ts` only.

---

## How rules get added or changed

A new rule needs:
1. An issue describing the problem the rule prevents
2. A test that fails when the rule is violated (where mechanically
   possible)
3. A short discussion thread (PRs are fine for ratifying)

A rule change needs all of the above plus an explicit major-version
bump on the next release, because the rules are part of the contract
this codebase makes with the people who install it. We don't relax
them quietly.

---

## See also

- [`../CONTRIBUTING.md`](../CONTRIBUTING.md) — how these rules apply to your PRs
- [`../SECURITY.md`](../SECURITY.md) — the rules that are also security commitments
- [`repository-split.md`](repository-split.md) — why Pro lives elsewhere and Rule 6 exists
