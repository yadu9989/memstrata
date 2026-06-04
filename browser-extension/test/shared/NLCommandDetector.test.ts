// Phase 32 — NLCommandDetector unit tests (Hard Rule 66)
import { describe, it, expect } from 'vitest';
import { NLCommandDetector, COMMANDS } from '../../src/content/shared/NLCommandDetector';

const detector = new NLCommandDetector();

// ---------------------------------------------------------------------------
// detect() — pattern matching
// ---------------------------------------------------------------------------

describe('NLCommandDetector.detect()', () => {
  describe('delete_this_chat', () => {
    it.each([
      'delete this chat',
      'delete this chat history',
      'wipe memory for this chat',
      'forget this conversation',
      'clear memory layer for this chat',
      'delete this chat.',
      '  delete this chat  ',
    ])('matches %s', (input) => {
      const cmd = detector.detect(input);
      expect(cmd).not.toBeNull();
      expect(cmd!.id).toBe('delete_this_chat');
    });

    it('does not match generic delete phrases', () => {
      expect(detector.detect('delete this file')).toBeNull();
      expect(detector.detect('delete history')).toBeNull();
    });
  });

  describe('delete_all_memory', () => {
    it.each([
      'delete all my memory',
      'delete all memory layer data',
      'wipe all memory layer data',
      'nuke everything',
      'delete all memory.',
    ])('matches %s', (input) => {
      const cmd = detector.detect(input);
      expect(cmd).not.toBeNull();
      expect(cmd!.id).toBe('delete_all_memory');
    });

    it('requires double confirmation', () => {
      const cmd = detector.detect('delete all my memory');
      expect(cmd!.confirmationLevel).toBe('double');
    });
  });

  describe('pause', () => {
    it.each([
      'pause memory layer',
      'stop tracking',
      'disable memory layer',
    ])('matches %s', (input) => {
      const cmd = detector.detect(input);
      expect(cmd).not.toBeNull();
      expect(cmd!.id).toBe('pause');
    });

    it('is not destructive', () => {
      expect(detector.detect('pause memory layer')!.destructive).toBe(false);
    });
  });

  describe('show_memory', () => {
    it.each([
      'what do you remember',
      'what do you remember?',
      'show memory',
      "show me what you've saved",
      'show me what you saved',
    ])('matches %s', (input) => {
      const cmd = detector.detect(input);
      expect(cmd).not.toBeNull();
      expect(cmd!.id).toBe('show_memory');
    });

    it('is not destructive', () => {
      expect(detector.detect('show memory')!.destructive).toBe(false);
    });
  });

  describe('normal prompts pass through', () => {
    it.each([
      'fix this bug in my code',
      'how do I use Python async?',
      'explain this function',
      '',
      'memory',
      'delete',
      'pause',
    ])('returns null for %j', (input) => {
      // Only the exact short words "pause" / "memory" should NOT match
      // (they don't match the full pattern). Verify non-commands return null.
      if (['pause', 'delete', 'memory'].includes(input.trim())) {
        expect(detector.detect(input)).toBeNull();
      } else {
        expect(detector.detect(input)).toBeNull();
      }
    });
  });
});

// ---------------------------------------------------------------------------
// All commands have required properties
// ---------------------------------------------------------------------------

describe('COMMANDS structure (Hard Rule 66 invariants)', () => {
  it('every command has a unique id', () => {
    const ids = COMMANDS.map((c) => c.id);
    expect(new Set(ids).size).toBe(ids.length);
  });

  it('every destructive command requires single or double confirmation', () => {
    for (const cmd of COMMANDS) {
      if (cmd.destructive) {
        expect(['single', 'double']).toContain(cmd.confirmationLevel);
      }
    }
  });

  it('delete_all_memory requires double confirmation (highest level)', () => {
    const nuke = COMMANDS.find((c) => c.id === 'delete_all_memory');
    expect(nuke).toBeDefined();
    expect(nuke!.confirmationLevel).toBe('double');
  });

  it('every command has at least one pattern', () => {
    for (const cmd of COMMANDS) {
      expect(cmd.patterns.length).toBeGreaterThan(0);
    }
  });

  it('every command has a description string', () => {
    for (const cmd of COMMANDS) {
      expect(typeof cmd.description).toBe('string');
      expect(cmd.description.length).toBeGreaterThan(0);
    }
  });
});
