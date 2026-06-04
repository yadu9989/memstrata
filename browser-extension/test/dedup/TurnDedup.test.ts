import { djb2Hash, RollingHashCache, DEDUP_CACHE_SIZE } from '../../src/shared/dedup';

describe('djb2Hash', () => {
  it('returns a number', () => {
    expect(typeof djb2Hash('hello')).toBe('number');
  });

  it('same input produces same hash', () => {
    expect(djb2Hash('hello')).toBe(djb2Hash('hello'));
  });

  it('different inputs produce different hashes', () => {
    expect(djb2Hash('hello')).not.toBe(djb2Hash('world'));
  });

  it('empty string returns 5381', () => {
    expect(djb2Hash('')).toBe(5381);
  });
});

describe('RollingHashCache', () => {
  it('isDuplicate returns false for unseen hash', () => {
    const cache = new RollingHashCache();
    expect(cache.isDuplicate(42)).toBe(false);
  });

  it('isDuplicate returns true after add', () => {
    const cache = new RollingHashCache();
    cache.add(42);
    expect(cache.isDuplicate(42)).toBe(true);
  });

  it('size increments correctly', () => {
    const cache = new RollingHashCache();
    expect(cache.size).toBe(0);
    cache.add(1);
    expect(cache.size).toBe(1);
    cache.add(2);
    expect(cache.size).toBe(2);
  });

  it('adding 51 items keeps size at or below 50', () => {
    const cache = new RollingHashCache();
    for (let i = 0; i < DEDUP_CACHE_SIZE + 1; i++) {
      cache.add(i);
    }
    expect(cache.size).toBeLessThanOrEqual(DEDUP_CACHE_SIZE);
  });

  it('after eviction the first item hash is no longer duplicate', () => {
    const cache = new RollingHashCache();
    const firstHash = 0;
    cache.add(firstHash);
    // Fill remaining capacity and one more to trigger eviction of firstHash
    for (let i = 1; i <= DEDUP_CACHE_SIZE; i++) {
      cache.add(i);
    }
    expect(cache.isDuplicate(firstHash)).toBe(false);
  });
});
