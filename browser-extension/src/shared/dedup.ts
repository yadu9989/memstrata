export const DEDUP_CACHE_SIZE = 50;

export function djb2Hash(text: string): number {
  let hash = 5381;
  for (let i = 0; i < text.length; i++) {
    hash = (((hash << 5) + hash) ^ text.charCodeAt(i)) | 0;
  }
  return hash >>> 0;
}

export class RollingHashCache {
  private readonly hashes: number[] = [];
  private readonly seen = new Set<number>();

  isDuplicate(hash: number): boolean {
    return this.seen.has(hash);
  }

  add(hash: number): void {
    if (this.seen.has(hash)) return;
    if (this.hashes.length >= DEDUP_CACHE_SIZE) {
      const evicted = this.hashes.shift()!;
      this.seen.delete(evicted);
    }
    this.hashes.push(hash);
    this.seen.add(hash);
  }

  get size(): number {
    return this.hashes.length;
  }
}
