// All ShadowPiercer tests pass throttleMs=0 so the 50ms batch window collapses to a
// single event-loop tick — keeps test timings tight without changing production behaviour.

import { ShadowPiercer } from '../../src/content/engine/ShadowPiercer';
import { VelocityDetector } from '../../src/content/engine/detectors/VelocityDetector';
import { DetectorChain } from '../../src/content/engine/DetectorChain';
import type { Candidate, Detector } from '../../src/content/engine/types';

function sleep(ms: number): Promise<void> {
  return new Promise((r) => setTimeout(r, ms));
}

function makeShadowHost(tag: string): HTMLElement {
  const host = document.createElement(tag);
  host.attachShadow({ mode: 'open' });
  return host;
}

beforeEach(() => {
  document.body.innerHTML = '';
});

describe('ShadowPiercer', () => {

  // ── Basic attachment ──────────────────────────────────────────────────────────

  it('attaches to an existing open shadow root when start() is called', async () => {
    const host = makeShadowHost('cib-message');
    document.body.appendChild(host);

    const received: MutationRecord[][] = [];
    const piercer = new ShadowPiercer(['cib-message'], (m) => received.push(m), 0);
    piercer.start(document.body);

    host.shadowRoot!.appendChild(document.createElement('span'));
    await sleep(30); // throttle=0 → next tick is enough

    expect(received.length).toBeGreaterThan(0);
    piercer.dispose();
  });

  it('attaches to a shadow host added dynamically after start()', async () => {
    const received: MutationRecord[][] = [];
    const piercer = new ShadowPiercer(['cib-message'], (m) => received.push(m), 0);
    piercer.start(document.body);

    const host = makeShadowHost('cib-message');
    document.body.appendChild(host);
    await sleep(30); // let rootObserver callback run and pierce

    host.shadowRoot!.appendChild(document.createElement('span'));
    await sleep(30);

    expect(received.length).toBeGreaterThan(0);
    piercer.dispose();
  });

  it('does not forward light-DOM mutations when shadowRoot is null', async () => {
    const host = document.createElement('cib-message'); // no attachShadow
    document.body.appendChild(host);

    const received: MutationRecord[][] = [];
    const piercer = new ShadowPiercer(['cib-message'], (m) => received.push(m), 0);
    piercer.start(document.body);

    // Appending to host (light DOM) — not inside a shadow root.
    host.appendChild(document.createElement('span'));
    await sleep(30);

    expect(received.length).toBe(0);
    piercer.dispose(); // also cancels the polling timers queued for the null shadowRoot
  });

  it('dispose() stops forwarding shadow-root mutations', async () => {
    const host = makeShadowHost('cib-message');
    document.body.appendChild(host);

    const received: MutationRecord[][] = [];
    const piercer = new ShadowPiercer(['cib-message'], (m) => received.push(m), 0);
    piercer.start(document.body);
    await sleep(30); // let initial scan attach

    piercer.dispose();

    host.shadowRoot!.appendChild(document.createElement('span'));
    await sleep(30);

    expect(received.length).toBe(0);
  });

  // ── Async polling ─────────────────────────────────────────────────────────────

  it('polls and eventually pierces when shadowRoot becomes available asynchronously', async () => {
    // Element inserted without a shadow root — Copilot hydrates Web Components async.
    const host = document.createElement('cib-message');
    document.body.appendChild(host); // shadowRoot is null at this point

    const received: MutationRecord[][] = [];
    const piercer = new ShadowPiercer(['cib-message'], (m) => received.push(m), 0);
    piercer.start(document.body);

    // Wait long enough for 2+ poll cycles to fire and find null.
    await sleep(130); // 2 × POLL_INTERVAL_MS (50ms) + margin

    // Simulate async hydration: shadow root is now attached.
    host.attachShadow({ mode: 'open' });

    // Wait for the next poll (≤ 50ms) to pick it up and attach the observer.
    await sleep(80);

    // Mutate inside the now-accessible shadow root.
    const span = document.createElement('span');
    span.textContent = 'async streaming token';
    host.shadowRoot!.appendChild(span);
    await sleep(30); // throttle=0 → fires next tick

    expect(received.length).toBeGreaterThan(0);
    const spanSeen = received.flat().some(
      (m) => m.type === 'childList' && Array.from(m.addedNodes).includes(span),
    );
    expect(spanSeen).toBe(true);

    piercer.dispose();
  });

  it('dispose() cancels pending polling timers so no post-dispose piercing occurs', async () => {
    const host = document.createElement('cib-message'); // no shadow root
    document.body.appendChild(host);

    const received: MutationRecord[][] = [];
    const piercer = new ShadowPiercer(['cib-message'], (m) => received.push(m), 0);
    piercer.start(document.body);

    await sleep(30); // let the first poll fire and reschedule
    piercer.dispose(); // cancels all queued retry timers

    // Hydrate after dispose — must NOT be pierced.
    host.attachShadow({ mode: 'open' });
    host.shadowRoot!.appendChild(document.createElement('span'));
    await sleep(150); // well past any cancelled timers

    expect(received.length).toBe(0);
  });

  // ── Recursive piercing ───────────────────────────────────────────────────────

  it('recursively pierces a shadow host injected inside an already-pierced shadow root', async () => {
    // Level-1 host already in the DOM when start() is called (e.g. <cib-serp>).
    const level1 = makeShadowHost('cib-serp');
    document.body.appendChild(level1);

    const received: MutationRecord[][] = [];
    const piercer = new ShadowPiercer(['cib-serp', 'cib-message'], (m) => received.push(m), 0);
    piercer.start(document.body);

    // Copilot dynamically injects <cib-message> inside <cib-serp>'s shadow root.
    // The rootObserver on document.body never sees this insertion — only the
    // per-root observer on level-1 can detect and recursively pierce level-2.
    const level2 = makeShadowHost('cib-message');
    level1.shadowRoot!.appendChild(level2);
    await sleep(80); // throttle(0) + pierce fires; give an extra tick for observer to attach

    // Mutate inside the nested (level-2) shadow root.
    const streamingSpan = document.createElement('span');
    streamingSpan.textContent = 'nested streaming token';
    level2.shadowRoot!.appendChild(streamingSpan);
    await sleep(30);

    const allRecords = received.flat();
    const nestedMutationSeen = allRecords.some(
      (m) => m.type === 'childList' && Array.from(m.addedNodes).includes(streamingSpan),
    );
    expect(nestedMutationSeen).toBe(true);

    piercer.dispose();
  });

  // ── Integration: ShadowPiercer → VelocityDetector → DetectorChain ──────────

  it('integration: shadow-root streaming produces a promoted candidate in DetectorChain', async () => {
    const host = makeShadowHost('cib-message');
    document.body.appendChild(host);
    const shadow = host.shadowRoot!;

    // Container matches CONTAINER_NORMALIZE_SELECTOR via data-testid="assistant-message"
    // so VelocityDetector normalises its candidate to this same element.
    const container = document.createElement('div');
    container.setAttribute('data-testid', 'assistant-message');
    shadow.appendChild(container);

    const promoted: Element[] = [];
    const velocityDet = new VelocityDetector();

    // Stub detector immediately emits a semantic_attr candidate for `container` (0.8).
    // Combined with velocity (0.6): 1-(0.2×0.4)=0.92 ≥ 0.7 → promotion.
    const semanticStub: Detector = {
      name: 'semantic_attr',
      baseConfidence: 0.8,
      initialScan: () => [],
      start: (_root, onCandidate) => {
        onCandidate({
          node: container,
          detectorName: 'semantic_attr',
          confidence: 0.8,
          detectedAt: performance.now(),
        } as Candidate);
      },
      stop: () => {},
    };

    const chain = new DetectorChain([semanticStub, velocityDet]);
    chain.start(document.body, (node) => promoted.push(node));

    // throttleMs=0: each streaming mutation is forwarded in the next tick.
    const piercer = new ShadowPiercer(
      ['cib-message'],
      (mutations) => velocityDet.processMutations(mutations),
      0,
    );
    piercer.start(document.body);

    // Simulate AI streaming: 5 child-span insertions at 60ms intervals.
    // Velocity thresholds satisfied: MIN_CHUNKS=3 ✓, MIN_SPAN_MS=200ms ✓ (≈300ms span),
    // MIN_RATE=3.0/s ✓ (≈16/s), MIN_AVG_CHARS=2 ✓ ("word-N " = 7 chars).
    for (let i = 0; i < 5; i++) {
      const span = document.createElement('span');
      span.textContent = `word-${i} `;
      container.appendChild(span);
      await sleep(60);
    }
    await sleep(30); // flush any remaining throttle tick

    expect(promoted).toHaveLength(1);
    expect(promoted[0]).toBe(container);

    piercer.dispose();
    chain.stop();
  });
});
