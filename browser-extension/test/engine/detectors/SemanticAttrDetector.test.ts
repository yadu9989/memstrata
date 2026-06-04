import { SemanticAttrDetector } from '../../../src/content/engine/detectors/SemanticAttrDetector';

describe('SemanticAttrDetector', () => {
  beforeEach(() => {
    document.body.innerHTML = '';
  });

  it('detects [role="article"] elements', () => {
    document.body.innerHTML = '<div role="article">response</div>';
    const detector = new SemanticAttrDetector();
    const candidates = detector.initialScan(document.body);
    expect(candidates.length).toBe(1);
    expect(candidates[0].confidence).toBeGreaterThan(0.7);
  });

  it('detects [data-message-author-role="assistant"] with boosted confidence', () => {
    document.body.innerHTML = '<div data-message-author-role="assistant">response</div>';
    const detector = new SemanticAttrDetector();
    const candidates = detector.initialScan(document.body);
    expect(candidates[0].confidence).toBeGreaterThan(0.9);
  });
});
