import hints from './provider_hints.json';

const SCHEMA_VERSION: number = (hints as { _schema_version: number })._schema_version;

export class ConfigLoader {
  private static cached: typeof hints = hints;

  static async load(): Promise<typeof hints> {
    // Fire remote refresh without blocking — local defaults are always returned immediately
    void ConfigLoader.refreshRemote();
    return ConfigLoader.cached;
  }

  private static async refreshRemote(): Promise<void> {
    try {
      const stored = await chrome.storage.local.get(['remote_provider_hints', 'hints_fetched_at']);
      const cacheAge = Date.now() - ((stored['hints_fetched_at'] as number | undefined) ?? 0);

      if (cacheAge < 24 * 3600 * 1000 && stored['remote_provider_hints']) {
        ConfigLoader.cached = {
          ...ConfigLoader.cached,
          ...(stored['remote_provider_hints'] as object),
        } as typeof hints;
        return;
      }

      const res = await fetch(
        `https://config.memory-layer.io/provider_hints.json?v=${SCHEMA_VERSION}`,
      );
      if (!res.ok) return;

      const remote = await res.json() as { _schema_version?: number };
      if (remote._schema_version !== SCHEMA_VERSION) return; // schema mismatch — ignore

      ConfigLoader.cached = { ...ConfigLoader.cached, ...remote } as typeof hints;
      chrome.storage.local.set({ remote_provider_hints: remote, hints_fetched_at: Date.now() });
    } catch {
      // Keep bundled defaults on any network or storage error
    }
  }
}
