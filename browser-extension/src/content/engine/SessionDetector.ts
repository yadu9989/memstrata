import type { ProviderHints } from './types';

/**
 * Extracts the AI provider's own conversation/session ID from the current URL.
 * Patterns are loaded from provider_hints.json (remotely updatable per phase20_refactor §5)
 * so URL schema changes don't require an extension release.
 *
 * Returns null when no pattern is configured for this provider or when the
 * current URL doesn't contain a session ID (e.g. a new-chat page).
 */
export function getExternalSessionId(
  pathname: string,
  hash: string,
  hints: Pick<ProviderHints, 'url_session_pattern'>,
): string | null {
  if (!hints.url_session_pattern) return null;
  const re = new RegExp(hints.url_session_pattern);
  const match = (pathname + hash).match(re);
  return match?.[1] ?? null;
}
