import { DetectorChain, combinedConfidence } from '../../src/content/engine/DetectorChain';

// Controllable fake detector — emit() triggers the onCandidate callback as any real detector would
function makeDetector(name: string, baseConf: number) {
  let _emit: ((c: any) => void) | null = null;
  const detector = {
    name,
    baseConfidence: baseConf,
    initialScan: () => [] as any[],
    start: (_root: Element, onCandidate: (c: any) => void) => { _emit = onCandidate; },
    stop: () => { _emit = null; },
  };
  const emit = (node: Element, confidence: number) => {
    _emit?.({ node, detectorName: name, confidence, detectedAt: performance.now() });
  };
  return { detector, emit };
}

describe('DetectorChain voting', () => {
  let node: Element;

  beforeEach(() => {
    document.body.innerHTML = '';
    node = document.createElement('div');
    document.body.appendChild(node);
  });

  it('promotes node when aria_live alone fires (conf 0.9 ≥ 0.7)', () => {
    const { detector, emit } = makeDetector('aria_live', 0.9);
    const chain = new DetectorChain([detector] as any);
    const promoted: any[] = [];
    chain.start(document.body, (n, c) => promoted.push({ node: n, conf: c }));

    emit(node, 0.9);

    expect(promoted.length).toBe(1);
    expect(promoted[0].conf).toBeGreaterThanOrEqual(0.7);
  });

  it('promotes node when semantic_attr alone fires (conf 0.8 ≥ 0.7)', () => {
    const { detector, emit } = makeDetector('semantic_attr', 0.8);
    const chain = new DetectorChain([detector] as any);
    const promoted: any[] = [];
    chain.start(document.body, (n, c) => promoted.push({ node: n, conf: c }));

    emit(node, 0.8);

    expect(promoted.length).toBe(1);
    expect(promoted[0].conf).toBeGreaterThanOrEqual(0.7);
  });

  it('does NOT promote node when velocity alone fires (conf 0.6 < 0.7)', () => {
    const { detector, emit } = makeDetector('velocity', 0.6);
    const chain = new DetectorChain([detector] as any);
    const promoted: any[] = [];
    chain.start(document.body, (n, c) => promoted.push({ node: n, conf: c }));

    emit(node, 0.6);

    expect(promoted.length).toBe(0);
  });

  it('promotes node when velocity + structural agree (combined ≥ 0.7)', () => {
    const { detector: velDet, emit: velEmit } = makeDetector('velocity', 0.6);
    const { detector: strDet, emit: strEmit } = makeDetector('structural', 0.4);
    const chain = new DetectorChain([velDet, strDet] as any);
    const promoted: any[] = [];
    chain.start(document.body, (n, c) => promoted.push({ node: n, conf: c }));

    velEmit(node, 0.6);
    strEmit(node, 0.4);

    // combined = 1 - (1-0.6)*(1-0.4) = 1 - 0.24 = 0.76
    expect(promoted.length).toBe(1);
    expect(promoted[0].conf).toBeCloseTo(0.76, 2);
  });

  it('combines multiple detectors with multiplicative-with-floor formula', () => {
    // 1 - (1-0.6)*(1-0.4) = 1 - 0.24 = 0.76
    expect(combinedConfidence([{ confidence: 0.6 }, { confidence: 0.4 }])).toBeCloseTo(0.76);
  });

  it('handles ancestor/descendant relationship correctly (grouped as same candidate)', () => {
    const { detector: ariaDet, emit: ariaEmit } = makeDetector('aria_live', 0.9);
    const { detector: velDet, emit: velEmit } = makeDetector('velocity', 0.6);
    const chain = new DetectorChain([ariaDet, velDet] as any);
    const promoted: any[] = [];
    chain.start(document.body, (n, c) => promoted.push({ node: n, conf: c }));

    // aria_live fires on the node — promoted immediately (0.9 >= 0.7)
    ariaEmit(node, 0.9);
    expect(promoted.length).toBe(1);

    // velocity also fires on the same node — must NOT double-promote
    velEmit(node, 0.6);
    expect(promoted.length).toBe(1);
  });
});
