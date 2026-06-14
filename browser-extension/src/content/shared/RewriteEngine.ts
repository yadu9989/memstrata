// src/content/shared/RewriteEngine.ts
//
// Phase 34 — Rewrite mode: retrieve session context and embed into the prompt.
//
// Hard Rule 67: the caller MUST show a diff view before submitting the
// rewritten prompt. Auto-rewrite without showing the diff is rejected.
//
// Phase 34.4: generateWithRetrieval() calls POST /context/for-chat-rewrite
// and formats the result using <Established_Context> / <Active_Prompt> tags.

import type { RetrievalResult } from './memstrata_client.js';
import { fetchChatRewriteContext } from './memstrata_client.js';

export interface RewriteResult {
  originalPrompt: string;
  rewrittenPrompt: string;
  diff: DiffSegment[];
  estimatedTokensSaved: number;
  estimatedCostSaved: number;
  retrievalResult?: RetrievalResult | null;
}

export interface DiffSegment {
  type: 'unchanged' | 'added' | 'removed';
  text: string;
}

// ---------------------------------------------------------------------------
// Word-level LCS diff (no external dependencies)
// ---------------------------------------------------------------------------

type Token = string;

function tokenize(text: string): Token[] {
  // Split on whitespace-runs; keep whitespace as separate tokens so we can
  // reassemble the string exactly from the token list.
  return text.split(/(\s+)/).filter((t) => t !== '');
}

function computeLCS(a: Token[], b: Token[]): number[][] {
  const m = a.length;
  const n = b.length;
  const dp: number[][] = Array.from({ length: m + 1 }, () => new Array(n + 1).fill(0));
  for (let i = 1; i <= m; i++) {
    for (let j = 1; j <= n; j++) {
      dp[i][j] = a[i - 1] === b[j - 1]
        ? dp[i - 1][j - 1] + 1
        : Math.max(dp[i - 1][j], dp[i][j - 1]);
    }
  }
  return dp;
}

export function computeDiff(original: string, rewritten: string): DiffSegment[] {
  const a = tokenize(original);
  const b = tokenize(rewritten);

  if (a.length === 0 && b.length === 0) return [];
  if (a.length === 0) return [{ type: 'added', text: rewritten }];
  if (b.length === 0) return [{ type: 'removed', text: original }];

  const dp = computeLCS(a, b);
  const segments: DiffSegment[] = [];

  let i = a.length;
  let j = b.length;
  const ops: Array<{ type: 'unchanged' | 'added' | 'removed'; tok: string }> = [];

  while (i > 0 || j > 0) {
    if (i > 0 && j > 0 && a[i - 1] === b[j - 1]) {
      ops.unshift({ type: 'unchanged', tok: a[i - 1] });
      i--;
      j--;
    } else if (j > 0 && (i === 0 || dp[i][j - 1] >= dp[i - 1][j])) {
      ops.unshift({ type: 'added', tok: b[j - 1] });
      j--;
    } else {
      ops.unshift({ type: 'removed', tok: a[i - 1] });
      i--;
    }
  }

  // Merge consecutive operations of the same type into single segments
  for (const op of ops) {
    const last = segments[segments.length - 1];
    if (last && last.type === op.type) {
      last.text += op.tok;
    } else {
      segments.push({ type: op.type, text: op.tok });
    }
  }

  return segments;
}

// ---------------------------------------------------------------------------
// RewriteEngine
// ---------------------------------------------------------------------------

export class RewriteEngine {
  /**
   * Generate a rewritten prompt that compresses *contextText* and embeds it
   * into *originalPrompt*. Returns a RewriteResult with the diff and cost
   * estimates so the caller can show the diff view (Hard Rule 67).
   *
   * @param originalPrompt  The user's original typed prompt.
   * @param contextText     The retrieved context text from the session.
   * @param maxContextChars Maximum chars of context to embed (default 600).
   */
  generate(
    originalPrompt: string,
    contextText: string,
    maxContextChars = 600,
  ): RewriteResult {
    const compressed = this._compress(contextText, maxContextChars);
    const rewritten = compressed
      ? `[Session context: ${compressed}]\n\n${originalPrompt}`
      : originalPrompt;

    const diff = computeDiff(originalPrompt, rewritten);

    // Rough token savings: characters saved / 4 (average chars per token)
    const charsSaved = Math.max(0, contextText.length - compressed.length);
    const estimatedTokensSaved = Math.round(charsSaved / 4);
    // GPT-4 input pricing ≈ $0.03 / 1K tokens (rough estimate for display)
    const estimatedCostSaved = parseFloat((estimatedTokensSaved * 0.00003).toFixed(5));

    return {
      originalPrompt,
      rewrittenPrompt: rewritten,
      diff,
      estimatedTokensSaved,
      estimatedCostSaved,
    };
  }

  private _compress(text: string, maxChars: number): string {
    if (!text || !text.trim()) return '';
    const trimmed = text.trim();
    if (trimmed.length <= maxChars) return trimmed;

    // Try to break at a newline boundary
    const newline = trimmed.lastIndexOf('\n', maxChars);
    if (newline > maxChars / 2) return trimmed.slice(0, newline).trimEnd() + '\n…';

    // Fall back to word boundary
    const space = trimmed.lastIndexOf(' ', maxChars);
    if (space > 0) return trimmed.slice(0, space) + '…';

    return trimmed.slice(0, maxChars) + '…';
  }

  /**
   * Retrieve relevant context from the backend and produce a rewritten prompt.
   * Falls back gracefully if the backend is unreachable (Hard Rule 64).
   *
   * @param originalPrompt   The user's typed prompt.
   * @param externalSessionId Chat session ID from the provider URL.
   * @param providerId       e.g. "claude", "openai".
   * @param tokenBudget      Max tokens of retrieved context to embed.
   */
  async generateWithRetrieval(
    originalPrompt: string,
    externalSessionId: string,
    providerId: string,
    tokenBudget = 1500,
  ): Promise<RewriteResult> {
    let retrieval: RetrievalResult | null = null;

    if (externalSessionId && providerId) {
      try {
        retrieval = await fetchChatRewriteContext(
          externalSessionId,
          providerId,
          originalPrompt,
          tokenBudget,
        );
      } catch {
        // Hard Rule 64: backend unavailable → degrade to original prompt
      }
    }

    const rewritten = this._formatRewrittenPrompt(retrieval, originalPrompt);
    const diff = computeDiff(originalPrompt, rewritten);

    const contextTokens = retrieval?.token_budget_used ?? 0;
    const estimatedCostSaved = parseFloat((contextTokens * 0.00003).toFixed(5));

    return {
      originalPrompt,
      rewrittenPrompt: rewritten,
      diff,
      estimatedTokensSaved: contextTokens,
      estimatedCostSaved,
      retrievalResult: retrieval,
    };
  }

  private _formatRewrittenPrompt(
    retrieval: RetrievalResult | null,
    draftPrompt: string,
  ): string {
    if (!retrieval || !retrieval.retrieved_turns || retrieval.retrieved_turns.length === 0) {
      return draftPrompt;
    }
    const ctxLines = retrieval.retrieved_turns
      .map((t) => `[${t.role.toUpperCase()}] ${t.text}`)
      .join('\n\n');
    return `<Established_Context>\n${ctxLines}\n</Established_Context>\n\n<Active_Prompt>\n${draftPrompt}\n</Active_Prompt>`;
  }
}
