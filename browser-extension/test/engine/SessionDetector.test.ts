import { getExternalSessionId } from '../../src/content/engine/SessionDetector';

const CLAUDE_HINTS   = { url_session_pattern: '^/chat/([a-f0-9-]+)' };
const CHATGPT_HINTS  = { url_session_pattern: '^/c/([a-f0-9-]+)' };
const GEMINI_HINTS   = { url_session_pattern: '/app/([a-z0-9-]+)' };
const DEEPSEEK_HINTS = { url_session_pattern: '/a/chat/s/([a-zA-Z0-9-]+)' };
const GROK_HINTS     = { url_session_pattern: '/chat/([a-z0-9-]+)' };
const MISTRAL_HINTS  = { url_session_pattern: '/chat/([a-z0-9-]+)' };
const META_HINTS     = { url_session_pattern: '/c/([0-9]+)' };
const PERPLEXITY_HINTS = { url_session_pattern: '/search/([a-zA-Z0-9-]+)' };

describe('getExternalSessionId', () => {
  describe('claude.ai', () => {
    it('extracts session ID from a chat URL', () => {
      expect(getExternalSessionId('/chat/abc12345-def6-7890-abcd-ef1234567890', '', CLAUDE_HINTS))
        .toBe('abc12345-def6-7890-abcd-ef1234567890');
    });

    it('returns null on the new-chat landing page', () => {
      expect(getExternalSessionId('/', '', CLAUDE_HINTS)).toBeNull();
    });

    it('returns null for non-hex path segments', () => {
      // "new" contains characters outside [a-f0-9-]
      expect(getExternalSessionId('/chat/new', '', CLAUDE_HINTS)).toBeNull();
    });
  });

  describe('chatgpt.com', () => {
    it('extracts session ID from /c/<uuid>', () => {
      expect(getExternalSessionId('/c/aabbccdd-eeff-0011-2233-445566778899', '', CHATGPT_HINTS))
        .toBe('aabbccdd-eeff-0011-2233-445566778899');
    });

    it('returns null on the home page', () => {
      expect(getExternalSessionId('/', '', CHATGPT_HINTS)).toBeNull();
    });
  });

  describe('gemini.google.com', () => {
    it('extracts session ID from /app/<id>', () => {
      expect(getExternalSessionId('/app/abc123def456', '', GEMINI_HINTS)).toBe('abc123def456');
    });

    it('returns null on the home page', () => {
      expect(getExternalSessionId('/', '', GEMINI_HINTS)).toBeNull();
    });
  });

  describe('chat.deepseek.com', () => {
    it('extracts session ID from /a/chat/s/<id>', () => {
      expect(getExternalSessionId('/a/chat/s/MySession-123', '', DEEPSEEK_HINTS)).toBe('MySession-123');
    });
  });

  describe('grok.com', () => {
    it('extracts session ID from /chat/<id>', () => {
      expect(getExternalSessionId('/chat/abc123xyz', '', GROK_HINTS)).toBe('abc123xyz');
    });
  });

  describe('chat.mistral.ai', () => {
    it('extracts session ID from /chat/<id>', () => {
      expect(getExternalSessionId('/chat/mistral-abc123', '', MISTRAL_HINTS)).toBe('mistral-abc123');
    });
  });

  describe('www.meta.ai', () => {
    it('extracts numeric session ID from /c/<id>', () => {
      expect(getExternalSessionId('/c/1234567890', '', META_HINTS)).toBe('1234567890');
    });

    it('returns null when path has no numeric segment', () => {
      expect(getExternalSessionId('/', '', META_HINTS)).toBeNull();
    });
  });

  describe('www.perplexity.ai', () => {
    it('extracts session ID from /search/<id>', () => {
      expect(getExternalSessionId('/search/MySearch-abc123', '', PERPLEXITY_HINTS))
        .toBe('MySearch-abc123');
    });
  });

  describe('providers with no url_session_pattern', () => {
    it('returns null for github.com (Copilot) — no URL-based session', () => {
      expect(getExternalSessionId('/copilot/chat', '', {})).toBeNull();
    });

    it('returns null for copilot.microsoft.com — no URL-based session', () => {
      expect(getExternalSessionId('/', '', {})).toBeNull();
    });
  });

  describe('hash-based SPA routing', () => {
    it('matches a pattern in the hash when pathname has no match', () => {
      const hashHints = { url_session_pattern: '#/thread/([a-z0-9]+)' };
      expect(getExternalSessionId('/', '#/thread/abc123', hashHints)).toBe('abc123');
    });
  });

  describe('edge cases', () => {
    it('returns null when pattern is present but URL has no session yet', () => {
      expect(getExternalSessionId('/', '', CLAUDE_HINTS)).toBeNull();
    });

    it('returns null when hints object is empty ({})', () => {
      expect(getExternalSessionId('/chat/abc123', '', {})).toBeNull();
    });
  });
});
