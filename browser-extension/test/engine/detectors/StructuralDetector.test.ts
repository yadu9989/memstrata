import { StructuralDetector } from '../../../src/content/engine/detectors/StructuralDetector';

describe('StructuralDetector', () => {
  beforeEach(() => {
    document.body.innerHTML = '';
  });

  // ── initialScan ─────────────────────────────────────────────────────────────

  it('initialScan always returns an empty array (runtime-only detector)', () => {
    document.body.innerHTML = '<div role="log"><div>msg1</div><div>msg2</div></div>';
    const det = new StructuralDetector();
    expect(det.initialScan(document.body)).toHaveLength(0);
  });

  // ── Semantic container detection ─────────────────────────────────────────────

  it('emits a candidate (confidence 0.4) when a new element-node child is appended to a [role="log"] container', async () => {
    document.body.innerHTML = `
      <div role="log">
        <div>User message</div>
        <div>AI response 1</div>
      </div>
    `;
    const container = document.querySelector('[role="log"]')!;
    const det = new StructuralDetector();
    const candidates: any[] = [];
    det.start(document.body, (c) => candidates.push(c));

    const newMsg = document.createElement('div');
    newMsg.textContent = 'AI response 2';
    container.appendChild(newMsg);
    await sleep(50);

    expect(candidates.length).toBe(1);
    expect(candidates[0].confidence).toBe(0.4);
    expect(candidates[0].detectorName).toBe('structural');
    expect(candidates[0].node).toBe(newMsg);
  });

  it('does NOT emit for text-node children (only Element nodes are AI turns)', async () => {
    document.body.innerHTML = '<div role="log"><div>msg1</div><div>msg2</div></div>';
    const container = document.querySelector('[role="log"]')!;
    const det = new StructuralDetector();
    const candidates: any[] = [];
    det.start(document.body, (c) => candidates.push(c));

    // Append a bare text node (not an Element)
    container.appendChild(document.createTextNode('raw text'));
    await sleep(50);

    expect(candidates.length).toBe(0);
  });

  it('ignores mutations on elements other than the identified container', async () => {
    document.body.innerHTML = `
      <div role="log"><div>msg1</div><div>msg2</div></div>
      <div id="sidebar"><span>nav item</span></div>
    `;
    const sidebar = document.getElementById('sidebar')!;
    const det = new StructuralDetector();
    const candidates: any[] = [];
    det.start(document.body, (c) => candidates.push(c));

    // Mutate the sidebar — NOT the message-list container
    sidebar.appendChild(document.createElement('span'));
    await sleep(50);

    expect(candidates.length).toBe(0);
  });

  it('ignores mutations on deep descendants of the container (only direct children)', async () => {
    document.body.innerHTML = `
      <div role="log">
        <div id="turn1">initial text</div>
        <div id="turn2">initial text</div>
      </div>
    `;
    const turn1 = document.getElementById('turn1')!;
    const det = new StructuralDetector();
    const candidates: any[] = [];
    det.start(document.body, (c) => candidates.push(c));

    // Append inside an existing turn, NOT directly to the container
    turn1.appendChild(document.createElement('span'));
    await sleep(50);

    expect(candidates.length).toBe(0);
  });

  // ── Structural fallback ──────────────────────────────────────────────────────

  it('finds the message container via child-count heuristic when no semantic markers exist', async () => {
    // No role, no aria-label — purely structural
    document.body.innerHTML = `
      <div id="list">
        <div>Message A</div>
        <div>Message B</div>
      </div>
    `;
    const list = document.getElementById('list')!;
    const det = new StructuralDetector();
    const candidates: any[] = [];
    det.start(document.body, (c) => candidates.push(c));

    const newEl = document.createElement('div');
    newEl.textContent = 'Message C';
    list.appendChild(newEl);
    await sleep(50);

    expect(candidates.length).toBe(1);
    expect(candidates[0].confidence).toBe(0.4);
    expect(candidates[0].node).toBe(newEl);
  });

  // ── Lifecycle ────────────────────────────────────────────────────────────────

  it('stop() prevents further candidate emissions', async () => {
    document.body.innerHTML = '<div role="log"><div>msg1</div><div>msg2</div></div>';
    const container = document.querySelector('[role="log"]')!;
    const det = new StructuralDetector();
    const candidates: any[] = [];
    det.start(document.body, (c) => candidates.push(c));

    det.stop();

    container.appendChild(document.createElement('div'));
    await sleep(50);

    expect(candidates.length).toBe(0);
  });
});

function sleep(ms: number): Promise<void> {
  return new Promise((r) => setTimeout(r, ms));
}
