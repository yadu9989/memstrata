import { AriaLiveDetector } from '../../../src/content/engine/detectors/AriaLiveDetector';

describe('AriaLiveDetector', () => {
  beforeEach(() => {
    document.body.innerHTML = '';
  });

  it('detects [aria-live="polite"] regions on initial scan', () => {
    document.body.innerHTML = '<div aria-live="polite" id="t"></div>';
    const detector = new AriaLiveDetector();
    const candidates = detector.initialScan(document.body);
    expect(candidates.length).toBe(1);
    expect(candidates[0].confidence).toBe(0.9);
  });

  it('detects dynamically added aria-live regions', async () => {
    const detector = new AriaLiveDetector();
    const candidates: any[] = [];
    detector.start(document.body, (c) => candidates.push(c));

    const div = document.createElement('div');
    div.setAttribute('aria-live', 'assertive');
    document.body.appendChild(div);
    await sleep(50);

    expect(candidates.length).toBe(1);
  });
});

function sleep(ms: number): Promise<void> {
  return new Promise((r) => setTimeout(r, ms));
}
