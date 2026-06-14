# Browser extension

The browser extension watches the AI chat interfaces you visit — Claude,
ChatGPT, Gemini, DeepSeek, Grok, Copilot, Perplexity, Mistral, Meta AI —
and captures each turn into your local MemStrata daemon as it happens.

This document describes what it captures, where the data goes, and how
to verify the privacy claims yourself.

The source is in [`browser-extension/`](../browser-extension/). It's
Manifest V3, TypeScript, bundled with esbuild. The same source builds
for Chrome, Edge, and Firefox.

---

## What it captures

For each visible turn on a supported chat surface:

- **Role** (`'user'` or `'assistant'`)
- **Text content** of the turn
- **Provider** (`'anthropic'`, `'openai'`, `'google'`, `'deepseek'`, …)
- **External session ID** — the chat-specific identifier from the URL
  (e.g. ChatGPT's `c/<uuid>`, Claude's `chat/<uuid>`)
- **Wall-clock timestamp** of capture

That payload is POSTed to `http://127.0.0.1:8000/telemetry/session`.

The extension does **not** capture:

- Your provider API keys (the extension never reads them; the keys
  live in your provider's tab, not in the extension's process)
- Cookies, auth headers, or anything the page set on the response
- Network-layer telemetry (timings, status codes, request headers)
- Page content outside the chat turn itself
- Pages on hosts not in the explicit allow list (see "Permissions"
  below)

---

## Permissions

The Manifest V3 permissions block (see
[`browser-extension/manifest.json`](../browser-extension/manifest.json)):

```json
"permissions": ["activeTab", "storage", "scripting", "alarms"],
"host_permissions": [
  "https://claude.ai/*",
  "https://chatgpt.com/*",
  "https://gemini.google.com/*",
  "https://chat.deepseek.com/*",
  "https://grok.com/*",
  "https://github.com/copilot/*",
  "https://copilot.microsoft.com/*",
  "https://*.bing.com/*",
  "https://chat.mistral.ai/*",
  "https://www.meta.ai/*",
  "https://www.perplexity.ai/*",
  "http://localhost:8000/*"
]
```

- **`activeTab`** lets the content script run on the currently-focused
  tab when you interact with it (clicking the extension icon).
- **`storage`** lets the extension persist its own settings (per-tab
  enable/disable, project mapping).
- **`scripting`** lets the service worker inject the universal content
  script into supported pages.
- **`alarms`** lets the extension run a periodic flush of any
  buffered captures.

`host_permissions` is the audit-critical list. The extension's content
script ONLY runs on the URLs explicitly listed. There is no wildcard
for "any page". The only non-provider entry is `http://localhost:8000/*`,
which is the local daemon.

Verifying this in Chrome: `chrome://extensions/?id=<extension-id>` →
"Inspect site permissions". The displayed list is the same as the
manifest.

---

## Where the data goes

Every captured turn is sent to **exactly one** URL:

```
http://127.0.0.1:8000/telemetry/session
```

No exceptions. The browser-extension's `memstrata_client.ts`
contains a single fetch URL constant. Search the source:

```bash
grep -rn "fetch(" browser-extension/src/content/shared/memstrata_client.ts
```

You'll see the localhost endpoint and nothing else.

The service worker does have an "ingestion" path that talks to
provider APIs **on your behalf** — but that path is for the prompt
augmentation feature (Pro tier), and it talks to the same provider
your tab is talking to with the API key your tab is using. The path is
inert when running against the Open-only daemon (the `FeatureGate`
check returns `false` for the augmentation feature).

---

## Verifying the privacy claims yourself

There are three independent ways to confirm the extension only talks
to loopback for captures:

### 1. Read the source

[`browser-extension/src/content/shared/memstrata_client.ts`](../browser-extension/src/content/shared/memstrata_client.ts)
is the entire HTTP layer the content script uses. It's ~150 lines.
The base URL is a single constant. The two non-localhost calls are
inert in the Open-only configuration (the augmentation flow's
provider-direct calls — Pro tier only).

### 2. Watch the network in DevTools

Open Chrome DevTools → Network tab. Filter for `Fetch/XHR`. Have a
conversation on `chatgpt.com`. You will see:

- POSTs to `http://127.0.0.1:8000/telemetry/session` — these are the
  captures
- The normal `chatgpt.com` API traffic that ChatGPT itself makes —
  the extension is NOT in that path

You will NOT see:
- Posts to any non-loopback host that the extension initiated
- Posts to a `memstrata.dev` backend (we don't run one)

### 3. Block localhost and watch nothing reach the daemon

Install the extension. Stop the MemStrata daemon. Have a conversation.
Open DevTools → Network. You'll see the POSTs to `127.0.0.1:8000`
returning ERR_CONNECTION_REFUSED. The chat session works normally
(because the extension's failure mode is "drop the capture"); nothing
reaches anywhere else.

---

## How the universal detector chain works

Most chat surfaces don't expose a stable JavaScript API for "give me
this turn." Each provider's DOM structure is different and changes
without notice. The extension handles this with a chain of detectors
that each try a different strategy:

1. **`AriaLiveDetector`** — looks for `aria-live="polite"` regions
   that wrap the assistant's response. The most reliable signal when
   the provider uses semantic HTML (Claude historically did).
2. **`SemanticAttrDetector`** — looks for explicit `data-message-*`
   attributes. Some providers add these for their own client-side
   logic; we piggy-back.
3. **`StructuralDetector`** — falls back to DOM structure analysis:
   "the assistant's response is whatever is the last child of the
   conversation root that matches a turn-shaped pattern."
4. **`VelocityDetector`** — last resort: watches for text that's
   being typed character-by-character (the streaming-response pattern)
   and identifies the containing element by its update velocity.

The chain runs in order; the first detector that returns a valid
candidate wins. A `StreamWatcher` then observes the chosen element
and emits a `turn-complete` event when the text stops growing.

When everything works, you get a single capture per assistant turn
with the full final text. When the provider redesigns their UI, the
detectors degrade gracefully — usually one or two stop matching, the
others pick up the slack. Failures are logged with a category for
telemetry hygiene work; the dashboard surfaces them under "Detector
health."

---

## Building and loading unpacked

```bash
cd browser-extension
npm install
npm run build
```

That produces `dist/`. To sideload in Chrome:

1. Navigate to `chrome://extensions/`
2. Toggle "Developer mode" on (top right)
3. Click "Load unpacked"
4. Select the `browser-extension/dist/` directory

The extension installs immediately. Open any supported chat surface;
you should see captures appear in the dashboard within ~1 second of
the first assistant turn.

Loading in Firefox or Edge is similar — see
[Firefox's about:debugging guide](https://extensionworkshop.com/documentation/develop/temporary-installation-in-firefox/)
or [Edge's developer mode](https://learn.microsoft.com/en-us/microsoft-edge/extensions-chromium/getting-started/extension-sideloading).

---

## Adding a new provider

If you want to add support for an additional chat surface:

1. Add the host pattern to `manifest.json`'s `host_permissions` AND
   `content_scripts[].matches`.
2. Add an entry to `browser-extension/src/content/config/provider_hints.json`
   with the URL pattern for the external session ID.
3. Test locally. The universal detectors will usually work without
   any per-provider code; the only required entry is the session ID
   pattern.
4. Open a PR. We'll review and ship.

We deliberately limit the host list to chat surfaces we've tested.
Adding a provider isn't gatekept — we just want to make sure the
detectors actually work there before claiming support.

---

## What we don't do

- We don't run a third-party analytics SDK. The extension imports no
  external scripts, no Google Analytics, no Sentry, no anything.
- We don't ship telemetry about your extension usage to ourselves.
  See [`hard-rules.md`](hard-rules.md) Rule 4.
- We don't read or modify cookies or auth headers for any host.
- We don't request `<all_urls>` permission. We never will.
