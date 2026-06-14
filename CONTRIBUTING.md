# Contributing to MemStrata

Thanks for wanting to help. This document covers the things that aren't
obvious from reading the code.

By participating in this project you agree to abide by the
[Code of Conduct](CODE_OF_CONDUCT.md).

---

## Development setup

```bash
git clone https://github.com/yadu9989/MemStrata.git
cd MemStrata
python -m venv .venv
. .venv/bin/activate            # or .venv\Scripts\activate on Windows
pip install -e ".[dev]"
pytest
```

Python 3.10+ is required. We test against 3.10, 3.11, and 3.12 in CI.

For the browser extension:

```bash
cd browser-extension
npm install
npm run build                   # produces dist/
npm test                        # vitest
```

---

## Test discipline

The Python suite currently has **491 tests** and the browser-extension
suite has its own vitest coverage. Every pull request must keep both
suites green. If you change a public API, you change the test next to
it; if you add behavior, you add a test.

We don't accept PRs that say "test fix coming in a follow-up." Test
discipline isn't a stylistic preference — it's how a local-first tool
earns the right to ask people to run it on their machine.

```bash
pytest                          # unit + integration; ~30 s on a laptop
pytest -k "not e2e"             # skip the slower e2e suite
pytest tests/ingestion/         # one subsystem at a time
```

When you touch the dashboard HTML/JS, also do a manual smoke:

```bash
python -m memstrata.cli.main daemon start    # or memstrata daemon start
# Open http://127.0.0.1:8000/dashboard and click through every tab.
```

---

## Architectural commitments

These are non-negotiable. PRs that violate them will be closed regardless
of how clever the implementation is.

1. **Localhost-only binding.** Every `uvicorn.run(...)` and
   `socket.bind(...)` in this repo hard-codes `127.0.0.1`. Do not add
   a `--host` flag, do not read host from config, do not bind `0.0.0.0`.

2. **No TLS interception, no MITM.** The browser extension and the
   daemon talk plain HTTP on loopback. They never sit between the user
   and a provider's TLS endpoint. If your feature requires intercepting
   `api.openai.com` traffic, it belongs in the Pro tier, not here.

3. **No cloud sync, no remote telemetry.** Data lives in
   `~/.memstrata/` on the user's machine. There is no "ship to our
   servers" mode. We don't run servers that accept user data.

4. **Open code never imports Pro code.** This repo has zero imports of
   `memstrata_pro`, `harness`, `billing`. CI greps for these on every
   PR; the check fails the build if a Pro import lands.

5. **Defensive everywhere.** Public surfaces (HTTP routes, MCP handlers,
   the browser extension's content scripts) must catch broad exceptions
   and degrade gracefully. A malformed provider response is not allowed
   to crash the daemon.

If you're proposing something that bumps against any of these, open
an issue first so we can talk through the design before you spend time
on a PR.

---

## Pull request process

1. **Open an issue first** for non-trivial work. "What I want to build"
   is enough — we'll respond with whether it fits or what to adjust.
2. **Branch from `main`**, name your branch `<short-area>/<short-summary>`
   (e.g., `ingestion/skip-binary-files`).
3. **Run `pytest` locally** before pushing. CI will run it too, but
   please don't burn CI minutes on broken tests.
4. **Write a clear commit message.** What changed and why. Link to the
   issue.
5. **Keep PRs small.** A PR that touches 5 files is easier to review
   than one that touches 50. Split big changes into a series.
6. **Be patient with review.** This is a small project; turnaround can
   be a few days.

---

## Issue triage

Labels we use:
- `bug` — something is broken
- `enhancement` — a feature request
- `out-of-scope` — see "What we don't accept" below
- `pro-tier` — belongs in the commercial repo, not here
- `good-first-issue` — small enough for a first contribution
- `help-wanted` — we'd take a PR for this

Response time goal: **acknowledge within 7 days, first substantive
response within 14 days.** This isn't a SLA — it's an aspiration on a
small team.

---

## What we don't accept

Some categories of contribution will be closed without ceremony.
Sometimes these come back as great ideas in the commercial Pro tier;
sometimes they're permanent "out of scope" calls. Either way, don't
spend time on them without checking first.

- **Cloud sync features.** "Sync my MemStrata data across devices via
  your backend" — no. We don't run backends.
- **Anything that binds beyond localhost.** "Let me run MemStrata on a
  shared dev server" — no. The Pro tier has thoughts on team workflows
  that don't compromise the local-first commitment.
- **MITM-style proxies.** "Add a feature that intercepts HTTPS to
  api.openai.com." That's the Pro tier's job; the threat model here
  rejects MITM.
- **Telemetry that ships user content.** "Send anonymized prompt
  snippets to help us improve" — no. We don't collect user content,
  even anonymized.
- **Decorative architecture changes.** "Rewrite in Rust", "switch to
  microservices", "use a different web framework because I like it
  better" — please don't. We optimize for boring, debuggable code on
  a small team's maintenance budget.
- **Feature requests for the Pro tier.** Token-budgeting,
  money-back-guarantee math, IDE integration — those live in the
  commercial repo. Issues asking for them here will be redirected.

---

## Pro tier interactions

The Pro tier (`memstrata-pro`) is a separate repository under a
proprietary license. It depends on this package via PyPI. If you're
working on the Open repo:

- You don't need access to the Pro repo to develop here.
- Pro-specific bug reports go to Pro's private support channel.
- "I'd like to use MemStrata for X but it would need a Pro feature"
  is fine to raise here as a discussion — we'll route it.

---

## Getting in touch

- Bugs / features: GitHub issues
- Security: `security@memstrata.dev` (see [SECURITY.md](SECURITY.md))
- Everything else: GitHub Discussions

Thanks for reading this far. The local-first commitment only stays
honest because contributors like you hold the line on it.
