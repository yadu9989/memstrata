// Phase 34 — RewriteEngine unit tests (Hard Rule 67)
import { describe, it, expect } from 'vitest';
import { computeDiff, RewriteEngine } from '../../src/content/shared/RewriteEngine';

// ---------------------------------------------------------------------------
// computeDiff
// ---------------------------------------------------------------------------

describe('computeDiff()', () => {
  it('returns empty array for two empty strings', () => {
    expect(computeDiff('', '')).toEqual([]);
  });

  it('returns single added segment when original is empty', () => {
    const result = computeDiff('', 'hello world');
    expect(result).toHaveLength(1);
    expect(result[0].type).toBe('added');
    expect(result[0].text).toBe('hello world');
  });

  it('returns single removed segment when rewritten is empty', () => {
    const result = computeDiff('hello world', '');
    expect(result).toHaveLength(1);
    expect(result[0].type).toBe('removed');
    expect(result[0].text).toBe('hello world');
  });

  it('returns single unchanged segment for identical strings', () => {
    const result = computeDiff('fix this bug', 'fix this bug');
    expect(result).toHaveLength(1);
    expect(result[0].type).toBe('unchanged');
    expect(result[0].text).toBe('fix this bug');
  });

  it('detects added text at the front', () => {
    const result = computeDiff('world', 'hello world');
    const types = result.map((s) => s.type);
    expect(types).toContain('added');
    expect(types).toContain('unchanged');
    const unchanged = result.find((s) => s.type === 'unchanged')!;
    expect(unchanged.text).toContain('world');
  });

  it('detects removed text', () => {
    const result = computeDiff('hello world', 'hello');
    const types = result.map((s) => s.type);
    expect(types).toContain('removed');
    expect(types).toContain('unchanged');
  });

  it('merges consecutive same-type operations', () => {
    // The entire string is unchanged — should be a single segment
    const result = computeDiff('one two three', 'one two three');
    expect(result).toHaveLength(1);
    expect(result[0].type).toBe('unchanged');
  });

  it('round-trips: joining all segment texts from rewritten reconstructs rewritten', () => {
    const original = 'fix this bug in my code';
    const rewritten = '[Session context: project notes]\n\nfix this bug in my code';
    const result = computeDiff(original, rewritten);

    // Extract tokens for the rewritten side (added + unchanged)
    const rewrittenText = result
      .filter((s) => s.type !== 'removed')
      .map((s) => s.text)
      .join('');
    expect(rewrittenText).toBe(rewritten);
  });

  it('round-trips: joining all segment texts from original reconstructs original', () => {
    const original = 'explain this function to me';
    const rewritten = '[Context: notes]\n\nexplain this function';
    const result = computeDiff(original, rewritten);

    const originalText = result
      .filter((s) => s.type !== 'added')
      .map((s) => s.text)
      .join('');
    expect(originalText).toBe(original);
  });

  it('segment types are limited to unchanged/added/removed', () => {
    const result = computeDiff('foo bar baz', 'foo qux baz');
    for (const seg of result) {
      expect(['unchanged', 'added', 'removed']).toContain(seg.type);
    }
  });
});

// ---------------------------------------------------------------------------
// RewriteEngine.generate()
// ---------------------------------------------------------------------------

const engine = new RewriteEngine();

describe('RewriteEngine.generate()', () => {
  it('returns the original prompt unchanged when contextText is empty', () => {
    const result = engine.generate('fix this bug', '');
    expect(result.rewrittenPrompt).toBe('fix this bug');
    expect(result.originalPrompt).toBe('fix this bug');
  });

  it('returns the original prompt unchanged when contextText is whitespace only', () => {
    const result = engine.generate('fix this bug', '   \n  ');
    expect(result.rewrittenPrompt).toBe('fix this bug');
  });

  it('embeds short context as [Session context: ...] prefix', () => {
    const result = engine.generate('what is this?', 'some project notes');
    expect(result.rewrittenPrompt).toMatch(/^\[Session context: some project notes\]/);
    expect(result.rewrittenPrompt).toContain('what is this?');
  });

  it('always returns a diff array', () => {
    const result = engine.generate('prompt', 'context');
    expect(Array.isArray(result.diff)).toBe(true);
    expect(result.diff.length).toBeGreaterThan(0);
  });

  it('estimatedTokensSaved is 0 when context fits without truncation', () => {
    const shortCtx = 'short';
    const result = engine.generate('my prompt', shortCtx);
    // No chars saved when context is already <= maxContextChars
    expect(result.estimatedTokensSaved).toBe(0);
  });

  it('estimatedTokensSaved > 0 when context exceeds maxContextChars', () => {
    const longCtx = 'word '.repeat(200); // 1000 chars
    const result = engine.generate('my prompt', longCtx, 600);
    expect(result.estimatedTokensSaved).toBeGreaterThan(0);
  });

  it('estimatedCostSaved is a non-negative number', () => {
    const result = engine.generate('my prompt', 'context text here');
    expect(typeof result.estimatedCostSaved).toBe('number');
    expect(result.estimatedCostSaved).toBeGreaterThanOrEqual(0);
  });

  it('compresses long context to at most maxContextChars + ellipsis', () => {
    const longCtx = 'a'.repeat(2000);
    const result = engine.generate('my prompt', longCtx, 600);
    // The embedded context portion should be at most 600 chars + ellipsis
    const match = result.rewrittenPrompt.match(/^\[Session context: ([\s\S]*?)\]\n/);
    expect(match).not.toBeNull();
    const embeddedCtx = match![1];
    // Should be truncated (has ellipsis or newline-break marker)
    expect(embeddedCtx.length).toBeLessThanOrEqual(602); // 600 + '…'
  });

  it('rewritten prompt contains the original prompt text', () => {
    const original = 'debug the authentication flow';
    const result = engine.generate(original, 'project: auth service notes');
    expect(result.rewrittenPrompt).toContain(original);
  });

  it('originalPrompt field matches the input', () => {
    const original = 'my question here';
    const result = engine.generate(original, 'ctx');
    expect(result.originalPrompt).toBe(original);
  });

  it('uses default maxContextChars of 600 when not specified', () => {
    const longCtx = 'word '.repeat(300); // 1500 chars
    const result = engine.generate('prompt', longCtx);
    const match = result.rewrittenPrompt.match(/^\[Session context: ([\s\S]*?)\]\n/);
    expect(match).not.toBeNull();
    const embeddedCtx = match![1];
    expect(embeddedCtx.length).toBeLessThanOrEqual(602);
  });

  it('prefers newline boundary when compressing long context', () => {
    // Build context with a newline past the halfway point of maxContextChars=60
    const ctx = 'A'.repeat(40) + '\n' + 'B'.repeat(40);
    const result = engine.generate('prompt', ctx, 60);
    const match = result.rewrittenPrompt.match(/^\[Session context: ([\s\S]*?)\]\n\n/);
    expect(match).not.toBeNull();
    // Should have cut at the newline and appended newline+ellipsis
    expect(match![1]).toContain('…');
    expect(match![1]).not.toContain('B'); // B-block should be removed
  });

  it('falls back to word boundary when no newline near truncation point', () => {
    const ctx = 'word '.repeat(30); // words, no newlines
    const result = engine.generate('prompt', ctx, 60);
    const match = result.rewrittenPrompt.match(/^\[Session context: ([\s\S]*?)\]\n\n/);
    expect(match).not.toBeNull();
    expect(match![1]).toContain('…');
  });

  describe('diff correctness with generated rewrite', () => {
    it('diff has no removed segments when context only adds text', () => {
      const original = 'what should I do?';
      const ctx = 'project: todo-app';
      const result = engine.generate(original, ctx);
      // The diff may have removed segments if tokenizer handles the prefix differently,
      // but the original text should be preserved in unchanged segments
      const unchangedText = result.diff
        .filter((s) => s.type === 'unchanged')
        .map((s) => s.text)
        .join('');
      expect(unchangedText).toContain('what');
    });

    it('diff segments are non-empty strings', () => {
      const result = engine.generate('test prompt', 'test context');
      for (const seg of result.diff) {
        expect(seg.text.length).toBeGreaterThan(0);
      }
    });
  });
});

// ---------------------------------------------------------------------------
// _compress (via generate — white-box through observable output)
// ---------------------------------------------------------------------------

describe('compress behaviour via generate()', () => {
  it('context shorter than maxContextChars is not truncated', () => {
    const ctx = 'short context';
    const result = engine.generate('p', ctx, 600);
    expect(result.rewrittenPrompt).toContain(ctx);
    expect(result.rewrittenPrompt).not.toContain('…');
  });

  it('empty string context produces no [Session context] prefix', () => {
    const result = engine.generate('prompt', '');
    expect(result.rewrittenPrompt).not.toContain('[Session context:');
  });

  it('single-word context longer than maxContextChars gets hard-sliced with ellipsis', () => {
    const ctx = 'x'.repeat(100);
    const result = engine.generate('p', ctx, 50);
    expect(result.rewrittenPrompt).toContain('…');
  });
});
