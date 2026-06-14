# Why two repositories?

MemStrata is split between two repositories under two different
licenses:

| Repo | License | Contents |
|---|---|---|
| `memstrata/memstrata` (this one) | MIT | Local daemon, browser extension, MCP server, dashboard, SQLite storage layer, CLI |
| `memstrata/memstrata-pro` (private) | Proprietary | Token-budgeting interception proxy ("harness"), VS Code extension, plan-tier feature gating, system tray app, packaging pipeline |

This document explains why the split exists, how the two repos relate
to each other technically, and what it means for users.

---

## Why split at all

The intuitive design is one repository with `packages/open/` and
`packages/pro/` folders and CI rules that police the boundary. We
rejected that for three reasons:

### 1. Boundary enforcement is structural, not procedural

In a monorepo, the boundary between Open and Pro is a CI rule someone
has to maintain. Every PR has to be checked. A new contributor who
hasn't read the rules can violate them by accident. The boundary holds
by hope plus tooling.

In two repos, the boundary is enforced by the file system. The Open
repo simply doesn't contain Pro code — you can't import what isn't
there. The check isn't a CI rule; it's a clone-level fact.

This matters specifically for the audit story: if you're a security
team evaluating MemStrata for use inside your organization, "audit
the code that runs on your machine" is exactly what this repo gives
you. Every line that touches your data lives in this clone. The Pro
tier — if you use it — is a separate package you install on top, and
you can audit that separately if you take a Pro subscription.

### 2. The marketing surface is materially stronger as a standalone repo

The open-source pitch is "audit what runs on your machine; the source
is on GitHub." A standalone repository at
`github.com/memstrata/memstrata` is the artifact that builds
credibility. A `packages/open/` folder inside a larger private
monorepo doesn't read as open-source even if the LICENSE file in that
folder says it is. The security team's first instinct on receiving a
"this is open source" link to a subfolder of a private repo is to
ask why they can only see the subfolder. Standalone removes the
question.

### 3. PyPI dependency forces clean interfaces

In the split design, the Pro repo consumes Open as a published PyPI
dependency. That means:
- Cross-package imports are versioned and pinned (`memstrata >= 0.6.0, < 0.7.0`)
- Breakage is detected at release time, not at deep-runtime
- The Pro repo can't reach into Open's internal modules without
  declaring the dependency
- Open's public API is visible because Pro uses it through normal
  Python imports against an installed package, not relative paths
  across a monorepo

In a monorepo, an internal cross-package import looks identical to a
public one. The discipline to keep the API clean is procedural again.

---

## How the two repos relate technically

```
              Open (MIT, this repo)              Pro (proprietary)
              ─────────────────────              ─────────────────
              memstrata/             ◀───────── memstrata-pro/
              browser-extension/                   harness/
              memstrata/layer3/                 packaging/
                api_server.py:                     extension/ (VS Code)
                  app.state.cohort_api  ◀───── injected by Pro overlay
                  app.state.dashboard_extras
                  /webhooks/stripe      ◀───── registered by Pro overlay
                  /license/*            ◀───── registered by Pro overlay

              ── PyPI ────────────────────────────────────────
                                                  ▲
              memstrata 0.6.x  ─────────────────── │
              (published from this repo's tag)     │
                                                   │
                                       memstrata-pro depends on
                                       memstrata >= 0.6.0, < 0.7.0
```

The Open daemon is fully functional standalone:

- The FastAPI `app` runs without modification
- Every Pro-specific behavior has a NoOp default in Open (see
  `_NoOpCohortApi` in `memstrata/layer3/api_server.py`)
- The dashboard renders Now + Quality tabs; the Money tab requires
  Pro

When the Pro tier is installed, `memstrata_pro.api_overlay.mount(app)`
runs at daemon startup and:

- Replaces `app.state.cohort_api` with a real implementation
- Registers `/webhooks/stripe` and `/license/*` routes
- Populates `app.state.dashboard_extras` with the Pro money-tab UI
- Applies the Pro-only SQLite schema (plan tables, Stripe customer map)

None of this requires modifying the Open code at runtime. The overlay
hooks are explicit injection points — `app.state.X` lookups with
defensive defaults — that Open declares as part of its public
contract.

---

## Versioning relationship

Both repos use semantic versioning. They release independently:

- **Open releases first.** When a feature lands in this repo, we tag
  and publish to PyPI.
- **Pro picks up Open on its own schedule.** Pro's `pyproject.toml`
  pins `memstrata >= X.Y.0, < (X+1).0.0`. A patch-level Open release
  is picked up automatically; a minor-level release requires Pro to
  explicitly upgrade its pin.
- **Breaking changes in Open bump minor version.** If we ship
  `memstrata 0.7.0` with a breaking change, Pro stays on `0.6.x`
  until they're ready to migrate. Open users get the new minor
  immediately.

The Open repo never pins to a specific Pro version. Pro is downstream;
that's the whole point of the dependency direction.

---

## What this means for users

### If you only want the open-source core

```bash
pip install memstrata
python -m memstrata.cli.main daemon start
```

That gives you:
- Local daemon on `127.0.0.1:8000`
- Chat capture (install the browser extension separately)
- MCP server at `http://127.0.0.1:8000/mcp` for Claude Desktop / Cursor
- Dashboard at `http://127.0.0.1:8000/dashboard` (Now + Quality tabs)
- SQLite storage in `~/.memstrata/core.db`
- Local-only operation, no telemetry, MIT license

You never need to touch the Pro repo. The Open repo's `pyproject.toml`
declares no Pro dependency.

### If you also want the Pro tier

`memstrata-pro` is published separately on PyPI for paying customers.
Installing it pulls `memstrata` automatically as a dependency. After
installation, the Pro overlay mounts onto the Open `app` at startup,
adding the harness, the VS Code extension, the system tray, and the
money-tab dashboard. You can stop your Pro subscription at any time
and revert to the Open-only experience by uninstalling `memstrata-pro`.

See [memstrata.dev](https://memstrata.dev) for Pro details.

---

## What this means for contributors

You don't need access to the Pro repo to contribute here. The Pro
codebase isn't required to develop, test, or release any change to
this repository.

If your contribution affects the boundary (specifically: if you're
adding a new `app.state.X` injection point that Pro will fill in),
flag it explicitly in the PR. We'll make sure the corresponding Pro
adapter is ready before the next coordinated release.

The reverse direction — "Pro needs Open to add a hook" — usually
shows up as an issue in this repo titled "expose X via app.state."
We treat those like any other API request: open discussion, evaluate
the design, ship it or push back.

---

## Why this isn't just hostile to users

Splitting code across two repos is overhead. Why bother instead of
making everything Open?

The honest answer is: the Pro tier funds maintenance of the Open
repo, the browser extension's listings, the MCP server's compatibility
with new IDE clients, the dashboard work, the build/test
infrastructure, and security response time. Splitting lets us be
specific about what's free, what's paid, and what each tier costs to
maintain — without either pretending the Open tier is fully
self-supporting or charging the Open users for it.

The boundary isn't a moat we'd love to remove if we could afford to.
It's the line that lets us keep the Open tier fully usable and
auditable for the people who want exactly that, and ship a paid
product on top for the people who want more.

If the line ever stops making sense — if the Pro features become
table-stakes for any real use of MemStrata, for example — that's
a signal we drew the line wrong. Open an issue.
