import { StreamWatcher } from '../../src/content/engine/StreamWatcher';
import { TurnExtractor } from '../../src/content/engine/TurnExtractor';

// ── StreamWatcher ─────────────────────────────────────────────────────────────

describe('StreamWatcher', () => {
  let node: HTMLDivElement;

  beforeEach(() => {
    document.body.innerHTML = '';
    node = document.createElement('div');
    node.textContent = 'Streamed response text';
    document.body.appendChild(node);
  });

  it('fires onComplete after debounce_ms of silence', async () => {
    const calls: string[] = [];
    new StreamWatcher(node, 100, 0.9, ['aria_live'], (text, _meta) => calls.push(text));

    await sleep(220); // debounce 100ms + generous buffer

    expect(calls.length).toBe(1);
    expect(calls[0]).toContain('Streamed response text');
  });

  it('does not fire before debounce_ms elapses', async () => {
    const calls: string[] = [];
    const watcher = new StreamWatcher(node, 200, 0.9, ['velocity'], (text, _meta) => calls.push(text));

    await sleep(80); // well inside the 200ms window

    expect(calls.length).toBe(0);
    watcher.dispose(); // cancel the pending timer so it does not bleed into next test
  });

  it('resets the completion timer when mutations arrive during the debounce window', async () => {
    const calls: string[] = [];
    // debounce = 150ms; initial timer fires at t=150ms
    new StreamWatcher(node, 150, 0.9, ['velocity'], (text, _meta) => calls.push(text));

    // At t≈80ms — mutate the node; timer resets to t≈80+150=230ms
    await sleep(80);
    node.appendChild(document.createTextNode(' continued'));

    // At t≈180ms — still inside the reset debounce window (fires at 230ms)
    await sleep(100);
    expect(calls.length).toBe(0);

    // At t≈330ms — well past the reset deadline
    await sleep(150);
    expect(calls.length).toBe(1);
  });

  it('dispose cancels the completion timer', async () => {
    const calls: string[] = [];
    const watcher = new StreamWatcher(node, 100, 0.9, ['aria_live'], (text, _meta) => calls.push(text));

    watcher.dispose();
    await sleep(220); // would have fired without dispose

    expect(calls.length).toBe(0);
  });
});

// ── TurnExtractor ─────────────────────────────────────────────────────────────

describe('TurnExtractor', () => {
  it('extracts plain text content', () => {
    const el = document.createElement('div');
    el.textContent = 'Hello world';
    expect(TurnExtractor.extract(el)).toBe('Hello world');
  });

  it('replaces non-breaking spaces (\\u00a0) with regular spaces', () => {
    const el = document.createElement('div');
    el.textContent = 'Hello world';
    expect(TurnExtractor.extract(el)).toBe('Hello world');
  });

  it('replaces tab and carriage-return characters with spaces', () => {
    const el = document.createElement('div');
    el.textContent = 'col1\tcol2\rcol3';
    expect(TurnExtractor.extract(el)).toBe('col1 col2 col3');
  });

  it('collapses 3 or more consecutive newlines to a double newline', () => {
    // Use <pre> so whitespace (including newlines) is preserved by innerText
    const el = document.createElement('pre');
    el.textContent = 'Para 1\n\n\n\nPara 2';
    const result = TurnExtractor.extract(el);
    expect(result).toBe('Para 1\n\nPara 2');
  });

  it('trims leading and trailing whitespace', () => {
    const el = document.createElement('div');
    el.textContent = '   trimmed   ';
    expect(TurnExtractor.extract(el)).toBe('trimmed');
  });

  it('returns empty string for an element with no text content', () => {
    const el = document.createElement('div');
    expect(TurnExtractor.extract(el)).toBe('');
  });
});

function sleep(ms: number): Promise<void> {
  return new Promise((r) => setTimeout(r, ms));
}
