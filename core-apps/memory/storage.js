import { NOTE_BASE } from './constants.js'

// ── Shared-memory read-through store ──────────────────────────────────────
// The graph + notes live in SHARED storage (/api/storage/shared/memory/),
// which `window.mobius.storage` cannot reach — that runtime hard-scopes every
// read to /api/storage/apps/${appId}/ — and the shell service worker sends all
// other /api/* straight to network, so a raw shared GET is blank offline and
// load-once (stale after an agent rewrite). This factory is the shared-scope
// twin of window.mobius.storage.get/getText/subscribe: read-through cache
// (last-known value served instantly, offline-capable), background revalidate,
// and a visibility-aware poller so subscribed views repaint when the chat or
// reflection agent rewrites the file. Pure factory (deps injected) so the offline
// harness can drive it with a mocked cache + fetch and no network.
export function makeSharedMemoryStore({
  baseUrl = NOTE_BASE,
  getToken,
  fetchImpl,
  cacheStore,
  cacheName = 'mobius-memory-shared-v1',
  pollMs = 4000,
  isVisible = () => (typeof document === 'undefined'
    ? true
    : document.visibilityState !== 'hidden'),
} = {}) {
  const doFetch = fetchImpl
    || (typeof fetch === 'function' ? (...a) => fetch(...a) : null);

  // The cache is a thin key->{ body, present } map. Backed by Cache Storage when
  // available (survives reloads, the offline mirror), else an in-memory Map so
  // a mock / a browser without caches still works (degrades to online-only).
  function memoryCache() {
    const m = new Map();
    return {
      async read(key) { return m.has(key) ? m.get(key) : null; },
      async write(key, entry) { m.set(key, entry); },
    };
  }
  async function openCacheStore() {
    if (cacheStore) return cacheStore;
    if (typeof caches === 'undefined' || !caches.open) return memoryCache();
    let c;
    try { c = await caches.open(cacheName); } catch { return memoryCache(); }
    return {
      async read(key) {
        const res = await c.match(key);
        if (!res) return null;
        const present = res.headers.get('x-memory-present') !== '0';
        const body = present ? await res.text() : null;
        return { body, present };
      },
      async write(key, entry) {
        const headers = { 'x-memory-present': entry.present ? '1' : '0' };
        try { await c.put(key, new Response(entry.body ?? '', { headers })); }
        catch { /* cache write is best-effort; reads still hit network */ }
      },
    };
  }
  let cacheReady = null;
  function cache() { return (cacheReady ||= openCacheStore()); }

  function url(path) {
    return path === 'graph.json' ? baseUrl + 'graph.json' : baseUrl + path;
  }

  // One network read. Returns { present, body } on a definitive answer (200 or
  // 404) and writes it through to the cache; throws on transient failure
  // (offline / 5xx) so the caller can fall back to the cached value.
  async function fetchThrough(path) {
    if (!doFetch) throw new Error('no fetch');
    const token = typeof getToken === 'function' ? await getToken() : null;
    const headers = token ? { Authorization: 'Bearer ' + token } : {};
    const res = await doFetch(url(path), { headers });
    if (res.status === 404) {
      const entry = { body: null, present: false };
      (await cache()).write(url(path), entry);
      return entry;
    }
    if (!res.ok) throw new Error('HTTP ' + res.status);
    const body = await res.text();
    const entry = { body, present: true };
    (await cache()).write(url(path), entry);
    return entry;
  }

  // Read-through: cached value first (instant, offline), revalidated in the
  // background. Returns { body, present, fromCache, error }. `error` is set only
  // when there is NO cached value AND the network failed — the genuine
  // can't-render state; a background-revalidate failure is swallowed (the
  // cached value already answered).
  async function read(path) {
    const cached = await (await cache()).read(url(path));
    if (cached) {
      fetchThrough(path).catch(() => {}); // revalidate; poller delivers fresh data
      return { ...cached, fromCache: true, error: null };
    }
    try {
      const fresh = await fetchThrough(path);
      return { ...fresh, fromCache: false, error: null };
    } catch (e) {
      return { body: null, present: false, fromCache: false, error: e };
    }
  }

  function parseJSON(body) {
    if (body == null) return null;
    try { return JSON.parse(body); } catch { return null; }
  }

  async function getJSON(path) {
    const r = await read(path);
    return { value: r.present ? parseJSON(r.body) : null, present: r.present, error: r.error };
  }
  async function getText(path) {
    const r = await read(path);
    return { value: r.present ? (r.body ?? '') : null, present: r.present, error: r.error };
  }

  // Subscribe a path: fire `cb` immediately with the cached/first value, then on
  // every poll where the raw body changed (an agent write). The poller only
  // ticks while the tab is visible, so a backgrounded app costs nothing. `cb`
  // receives { body, present, error } so callers parse for their own kind.
  // `opts.onRevalidate(bool)` brackets each background revalidation so a view
  // can show a "merging…" indicator while fresh shared data is being pulled in
  // and clear it once the new content (or a no-change verdict) has landed.
  function subscribe(path, cb, opts = {}) {
    const onRevalidate = typeof opts.onRevalidate === 'function' ? opts.onRevalidate : () => {};
    let alive = true;
    let last; // last raw body delivered — repaint only on a real change
    let timer = null;

    function deliver(body, present, error) {
      last = body;
      try { cb({ body, present, error: error || null }); }
      catch { /* a subscriber throwing must not kill the poller */ }
    }

    async function revalidate() {
      onRevalidate(true);
      try {
        const e = await fetchThrough(path);
        if (alive && e.body !== last) deliver(e.body, e.present, null);
      } catch { /* transient: keep the last value, just clear the indicator */ }
      finally { if (alive) onRevalidate(false); }
    }

    async function init() {
      const cached = await (await cache()).read(url(path));
      if (!alive) return;
      if (cached) {
        // Cached value paints instantly (offline-capable); then revalidate so an
        // agent write since last open is merged in.
        deliver(cached.body, cached.present, null);
        revalidate();
      } else {
        // Nothing cached: the first read IS the revalidation.
        onRevalidate(true);
        try {
          const e = await fetchThrough(path);
          if (alive) deliver(e.body, e.present, null);
        } catch (e) {
          if (alive) deliver(null, false, e);
        } finally { if (alive) onRevalidate(false); }
      }
    }

    function schedule() {
      if (!alive || pollMs <= 0) return;
      timer = setTimeout(async () => {
        if (isVisible()) await revalidate();
        schedule();
      }, pollMs);
    }

    init().finally(schedule);
    return () => { alive = false; if (timer) clearTimeout(timer); };
  }

  return { read, getJSON, getText, subscribe, _url: url };
}
