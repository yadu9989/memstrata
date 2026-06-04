import { VelocityDetector } from '../../../src/content/engine/detectors/VelocityDetector';

describe('VelocityDetector', () => {
  let detector: VelocityDetector;

  beforeEach(() => {
    detector = new VelocityDetector();
    document.body.innerHTML = '';
  });

  it('detects AI stream pattern: chunks every 50ms for 2 seconds', async () => {
    const node = document.createElement('div');
    document.body.appendChild(node);
    const candidates: any[] = [];
    detector.start(document.body, (c) => candidates.push(c));

    // Simulate 40 chunks of 10 chars each, every 50ms (2 seconds total)
    for (let i = 0; i < 40; i++) {
      node.appendChild(document.createTextNode('1234567890'));
      await sleep(50);
    }

    expect(candidates.length).toBeGreaterThan(0);
    expect(candidates[0].confidence).toBeGreaterThan(0.5);
  });

  it('ignores instant large insertion (paste)', async () => {
    const node = document.createElement('div');
    document.body.appendChild(node);
    const candidates: any[] = [];
    detector.start(document.body, (c) => candidates.push(c));

    // Single large append (paste pattern)
    node.appendChild(document.createTextNode('A'.repeat(5000)));
    await sleep(2000);

    expect(candidates.length).toBe(0);
  });

  it('ignores typing in textarea (1 char at a time, slow)', async () => {
    const ta = document.createElement('textarea');
    document.body.appendChild(ta);
    const candidates: any[] = [];
    detector.start(document.body, (c) => candidates.push(c));

    // Note: textarea value changes don't fire MutationObserver
    // This test verifies we don't somehow detect on input-like elements
    for (let i = 0; i < 20; i++) {
      ta.value += 'a';
      await sleep(150);
    }

    expect(candidates.length).toBe(0);
  });

  it('ignores typing in contenteditable (excluded by ancestor)', async () => {
    const ce = document.createElement('div');
    ce.setAttribute('contenteditable', 'true');
    const inner = document.createElement('span');
    ce.appendChild(inner);
    document.body.appendChild(ce);
    const candidates: any[] = [];
    detector.start(document.body, (c) => candidates.push(c));

    for (let i = 0; i < 20; i++) {
      inner.appendChild(document.createTextNode('a'));
      await sleep(150);
    }

    expect(candidates.length).toBe(0);
  });

  it('detects short bursts only if sustained (no single-burst false positive)', async () => {
    const node = document.createElement('div');
    document.body.appendChild(node);
    const candidates: any[] = [];
    detector.start(document.body, (c) => candidates.push(c));

    // 5 quick mutations then stop (toast-notification pattern)
    for (let i = 0; i < 5; i++) {
      node.appendChild(document.createTextNode('x'));
      await sleep(30);
    }
    await sleep(2000);

    expect(candidates.length).toBe(0);
  });

  it('signals only after MIN_SPAN_MS sustained activity', async () => {
    const node = document.createElement('div');
    document.body.appendChild(node);
    const candidates: any[] = [];
    detector.start(document.body, (c) => candidates.push(c));

    // 3 chunks in 90ms — fast but too short
    node.appendChild(document.createTextNode('Hello '));
    await sleep(30);
    node.appendChild(document.createTextNode('there '));
    await sleep(30);
    node.appendChild(document.createTextNode('world'));
    await sleep(30);

    expect(candidates.length).toBe(0);

    // Continue for another 500ms — now should fire
    for (let i = 0; i < 10; i++) {
      node.appendChild(document.createTextNode('more '));
      await sleep(50);
    }

    expect(candidates.length).toBeGreaterThan(0);
  });
});

function sleep(ms: number): Promise<void> {
  return new Promise((r) => setTimeout(r, ms));
}
