# Security policy

MemStrata runs on developers' workstations and reads their codebases. We
take that seriously.

This file covers how to report vulnerabilities and what architectural
commitments are themselves part of the security posture.

---

## Reporting a vulnerability

**Do not open a public GitHub issue.** That's the wrong channel for
anything that could be exploited.

Send a report to `security@memstrata.dev`. Encrypt with our public PGP
key (`docs/security-pgp.asc`) if the issue is severe enough to warrant
it; otherwise plain email is fine.

Include:
- Affected version (`memstrata --version`) and OS
- The smallest reproducer you can produce
- What you tried, what happened, and what you expected

If you'd like to be credited in the changelog, say so. If you'd prefer
to stay anonymous, say so. We honor either.

### Response SLA

- **Acknowledgment within 24 hours** of receipt, weekdays.
- **First substantive response within 7 days.** Triage, severity call,
  and timeline.
- **Coordinated disclosure.** We'll work with you on a disclosure
  schedule that gives users time to upgrade. Default embargo: 90 days
  from initial report, shorter if the issue is actively exploited.

We do not pay bug bounties at present. We do credit reporters in the
release notes if they want to be.

---

## Threat model

MemStrata's threat model is "an attacker can run code on the user's
machine but cannot trick the user into running malicious commands as
their own user." That's a deliberately narrow model. Things we
explicitly do NOT defend against:

- A malicious local administrator who has full root on the box
- An attacker who has already installed a different process listening
  on `127.0.0.1:8000`
- An attacker with read access to the user's home directory (they can
  read `~/.memstrata/core.db` directly without going through us)

We do defend against:

- Remote attackers reaching the daemon over the network (Hard
  commitment: localhost binding only — see below)
- Compromised provider TLS certs leaking traffic (we don't intercept
  TLS, so there's nothing to leak from us)
- Cross-site scripting through dashboard rendering (dashboard renders
  only data the local user produced; HTML is rendered with explicit
  escaping in the JS layer)
- A browser extension supply-chain compromise harvesting chat content
  off the user's machine via remote API (the extension talks only to
  loopback by default; remote calls are restricted to provider APIs
  the user is already using)

---

## Architectural commitments that ARE the security posture

These are not "features we hope to ship." They are properties enforced
by the code today and tested for in CI. Removing or weakening any of
them is a breaking change with a major-version bump.

### 1. Localhost-only binding

Every HTTP server in this repo binds strictly to `127.0.0.1`. The
daemon refuses to bind a non-loopback address even when explicitly
asked. There is no `--host` flag.

Verified by: `pytest tests/test_shutdown_endpoint.py` and a CI grep
for `0.0.0.0` in service code.

### 2. No TLS interception (no MITM)

The browser extension talks directly to provider APIs over TLS — its
network layer never proxies. The daemon serves plain HTTP on loopback
and does not generate or install any root certificates on the user's
machine.

There is no install-our-root-CA dance. There is no certificate-pinning
override. The Pro tier's interception harness is structurally separate
and lives in a different repository. If you're using this repo only,
no traffic to `api.anthropic.com` / `api.openai.com` / etc. flows
through any MemStrata code.

### 3. No telemetry that includes user content

The daemon's own outbound network calls are:
- OpenRouter, once per 24h, to refresh the pricing matrix (no user
  data sent — just a GET to a public endpoint)
- Bank of Canada Valet, once per 24h, to refresh the USDCAD FX rate
  (no user data sent — just a GET to a public endpoint)
- Ollama probe at `localhost:11434` (loopback)

No request body, no prompt text, no completion text, no API key, no
machine fingerprint, no IP, no IDs of any kind ever leave the user's
machine through this codebase.

### 4. API key handling

API keys for upstream providers (OpenAI, Anthropic, Google, Ollama) are
NEVER stored by this codebase in a database we control. The CLI's
optional keychain wrapper (`memstrata/config/keychain.py`) hands
the key to your OS keyring (Windows Credential Manager, macOS
Keychain, Linux secret-service) — we don't keep our own copy.

If you uninstall `memstrata`, the keychain entries you stored stay in
the OS keyring under your control. Delete them through your OS's
credential manager if you want to clear them.

### 5. Local file system access scope

The ingestion subsystem (`memstrata/layer3/ingestion/`) reads only:
- Paths explicitly registered via `memstrata register` or the cd-hook
- Files matching project-level allow rules (the watcher's per-project
  filter)
- Never `~/.ssh/`, `~/.aws/`, `~/.config/`, or other obviously
  sensitive paths — denylists are baked into
  `memstrata/layer3/ingestion/denylist.py`

If the denylist misses a category of file you care about, that's a
security issue and we treat it as one. Report it.

### 6. Pro-tier import boundary

The Open repo contains zero `import memstrata_pro`, `import harness`,
`import billing`. The Pro tier is a separate codebase under a
proprietary license. CI fails the build if a Pro import lands in this
tree. This isn't security in the cryptographic sense, but it IS the
property that lets you audit just this repo and know what's running.

---

## What we treat as a security bug vs. a behavior bug

**Security bug** (private channel, embargoed disclosure):
- The daemon accepts a request on a non-loopback interface
- Any code path leaks user content (prompt, completion, code) to a
  remote service
- The denylist misses a category of obviously sensitive file
- TLS root CA installation, certificate pinning override, or any
  MITM-style behavior

**Behavior bug** (public issue is fine):
- Dashboard renders wrong data
- A test is flaky
- The CLI prints a stack trace instead of a friendly error
- Performance regressions

When in doubt, err toward private reporting. We'd rather over-classify
than have a real issue ride a public ticket.

---

## Disclosure policy

We follow coordinated disclosure. Default timeline:
- T+0: Report received, acknowledged within 24h
- T+7d: Initial response with severity assessment
- T+30d: Fix in main branch + a security advisory drafted
- T+60d to T+90d: Coordinated public disclosure, credit to reporter
  (with their permission)

For actively-exploited issues, we'll shorten the embargo and publish
immediately. For low-severity issues, we may bundle the fix into the
next regular release without a dedicated advisory.

---

## Things this policy does NOT cover

- The Pro tier (`memstrata-pro`) has its own security policy in its
  own repository.
- Browser extension store-side issues (Chrome Web Store, Edge Add-ons,
  Firefox AMO) — report those to the store first; we'll coordinate.
- Provider security (OpenAI, Anthropic, Google) — report directly to
  the provider.

For everything else MemStrata-related: `security@memstrata.dev`.
