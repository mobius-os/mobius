/* Memory — an Obsidian-style force-directed view of the agent's knowledge.
 *
 * Data contract (unchanged, load-bearing):
 *   GET /api/storage/shared/memory/graph.json  → { nodes, edges, problems }
 *     node:    { id, title, type:'note'|'moc', path, size_bytes,
 *                importance:int, access_count:int, mocs:[], tags:[] }
 *     edge:    { source, target, kind:'moc'|'link' }
 *     problem: { severity:'warn'|'error', kind, detail }
 *   GET /api/storage/shared/memory/<node.path>  → raw markdown (frontmatter + body)
 *
 * Offline + live-repaint (see makeSharedMemoryStore below): the graph and notes
 * live in SHARED storage (/data/shared/memory/, written by the chat + dreaming
 * agents), which window.mobius.storage cannot address — that runtime is hard-
 * scoped to /api/storage/apps/${appId}/ and the shell service worker sends every
 * other /api/* straight to network, so a raw shared GET renders blank offline and
 * never repaints when an agent rewrites the graph beneath an open app. So this
 * app carries its own read-through cache over the shared route: a Cache-Storage-
 * backed store serving the last-known value instantly (offline-capable),
 * revalidating in the background, with a visibility-aware poller so subscribed
 * views (the graph, the open note) repaint when the agent writes. It is the
 * shared-namespace twin of window.mobius.storage.get/getText/subscribe until the
 * runtime grows a shared scope; offline_capable then caches only the app CODE.
 *
 * Everything else here is presentation. The graph renderer intentionally follows
 * Quartz's durable shape: d3-force for layout and PixiJS for links, nodes, and
 * labels in one transformed scene. D3/Pixi load the same way Quartz does:
 * pinned global scripts — served same-origin from /vendor because the prod
 * CSP only allows 'self' + esm.sh for scripts. Markdown rendering still
 * lazy-loads marked + DOMPurify from esm.sh (which the CSP permits).
 */
import { useState, useEffect, useRef, useMemo, useCallback } from 'react';

const NOTE_BASE = '/api/storage/shared/memory/';
// Self-hosted under /vendor (frontend/public/vendor/, precached by sw.js).
// Prod CSP (script-src 'self' 'unsafe-inline' https://esm.sh) blocks
// cdn.jsdelivr.net, which silently degraded the graph to the list view.
// Same dist files the jsdelivr URLs served; both expose classic-script
// globals (window.d3 / window.PIXI).
const D3_URL = '/vendor/d3@7.9.0/d3.min.js';
const PIXI_URL = '/vendor/pixi.js@8.19.0/pixi.min.js';
const MAX_LOCAL_DEPTH = 4;
const WIKILINK_RE = /\[\[\s*([^\]|#]+?)\s*(?:#[^\]|]*)?(?:\|\s*([^\]]+?)\s*)?\]\]/g;

// Stable, theme-agnostic accent palette for primary-MOC color coding.
// Chosen for distinguishability in both light and dark mode; MOC nodes
// themselves render in the theme accent so they read as "hubs".
const PALETTE = [
  '#6ea8fe', '#f59e8b', '#7dd3a8', '#c7a3f0', '#f0c674',
  '#5fc8d8', '#ef9bc4', '#9bd065', '#f08c5a', '#8ea0ec',
  '#d99bef', '#5bbf9e',
];

function hashStr(s) {
  let h = 0;
  for (let i = 0; i < s.length; i++) {
    h = (h * 31 + s.charCodeAt(i)) | 0;
  }
  return Math.abs(h);
}

// Strip a leading YAML frontmatter block (between the first two '---' lines).
function stripFrontmatter(md) {
  if (!md) return '';
  const m = md.match(/^---\s*\n([\s\S]*?)\n---\s*\n?/);
  return m ? md.slice(m[0].length) : md;
}

// Pull a few useful fields out of frontmatter for the panel header.
function parseFrontmatter(md) {
  const out = {};
  if (!md) return out;
  const m = md.match(/^---\s*\n([\s\S]*?)\n---/);
  if (!m) return out;
  for (const line of m[1].split('\n')) {
    const kv = line.match(/^([A-Za-z0-9_-]+):\s*(.*)$/);
    if (kv) out[kv[1].toLowerCase()] = kv[2].trim();
  }
  return out;
}

function fmtBytes(n) {
  if (n == null) return '—';
  if (n < 1024) return n + ' B';
  if (n < 1024 * 1024) return (n / 1024).toFixed(1) + ' KB';
  return (n / (1024 * 1024)).toFixed(1) + ' MB';
}

export function nodeRadius(node = {}) {
  const importance = Number(node.importance);
  const accessCount = Number(node.access_count);
  const safeImportance = Number.isFinite(importance) && importance > 0 ? importance : 1;
  const safeAccessCount = Number.isFinite(accessCount) && accessCount > 0 ? accessCount : 0;
  const base = Math.max(safeImportance, 1 + Math.log2(1 + safeAccessCount));
  const radius = 3 + base * 1.55;
  return node.type === 'moc' ? radius * 1.4 : radius;
}

export function shouldShowNodeLabel(globalScale, node = {}, hoverId = null) {
  const isHover = hoverId === node.id;
  const hasMocList = Array.isArray(node.mocs) && node.mocs.length > 0;
  const isImportant = (Number(node.importance) || 0) >= 7;
  const isMoc = node.type === 'moc';
  const isLocalCenter = node.localDepth === 0;
  if (isHover || isLocalCenter || isMoc || node.showLabelAlways) return true;
  const scale = Number(globalScale);
  if (!Number.isFinite(scale)) return false;
  return scale >= 0.95
    || (isImportant && scale >= 0.18)
    || (hasMocList && scale >= 0.24);
}

export function buildTitleMap(nodes = []) {
  const map = {};
  for (const n of nodes || []) {
    if (!n || !n.id) continue;
    map[n.id] = n.title || n.id;
  }
  return map;
}

export function renderWikiLinks(md, nodes = []) {
  if (!md) return '';
  const titles = buildTitleMap(nodes);
  return String(md).replace(WIKILINK_RE, (_, rawSlug, rawAlias) => {
    const slug = String(rawSlug || '').trim();
    if (!slug) return _;
    const label = String(rawAlias || '').trim() || titles[slug] || slug;
    return `[${escapeMarkdownLinkText(label)}](#memory-node-${encodeURIComponent(slug)})`;
  });
}

export function buildLocalGraphData(graph, centerId, depth = 1) {
  if (!graph || !centerId) return { nodes: [], links: [] };
  const nodes = Array.isArray(graph.nodes) ? graph.nodes : [];
  const edges = Array.isArray(graph.edges) ? graph.edges : [];
  const byId = new Map(nodes.map((n) => [n.id, n]));
  if (!byId.has(centerId)) return { nodes: [], links: [] };

  const maxDepth = Math.max(0, Math.min(MAX_LOCAL_DEPTH, Number(depth) || 0));
  const adj = new Map();
  const add = (a, b) => {
    if (!adj.has(a)) adj.set(a, new Set());
    adj.get(a).add(b);
  };
  for (const e of edges) {
    const s = typeof e.source === 'object' ? e.source.id : e.source;
    const t = typeof e.target === 'object' ? e.target.id : e.target;
    if (!byId.has(s) || !byId.has(t)) continue;
    add(s, t); add(t, s);
  }

  const seen = new Map([[centerId, 0]]);
  const q = [centerId];
  while (q.length) {
    const cur = q.shift();
    const d = seen.get(cur) || 0;
    if (d >= maxDepth) continue;
    for (const next of adj.get(cur) || []) {
      if (seen.has(next)) continue;
      seen.set(next, d + 1);
      q.push(next);
    }
  }

  const keep = new Set(seen.keys());
  const showLabelAlways = keep.size <= 150;
  return {
    nodes: [...keep].map((id) => ({
      ...byId.get(id),
      localDepth: seen.get(id) || 0,
      showLabelAlways,
    })),
    links: edges
      .map((e) => ({
        source: typeof e.source === 'object' ? e.source.id : e.source,
        target: typeof e.target === 'object' ? e.target.id : e.target,
        kind: e.kind,
      }))
      .filter((e) => keep.has(e.source) && keep.has(e.target)),
  };
}

// A short, human relative-time from an ISO-ish frontmatter date string.
function relDate(s) {
  if (!s || s === 'null') return null;
  const t = Date.parse(s);
  if (Number.isNaN(t)) return null;
  const days = Math.floor((Date.now() - t) / 86400000);
  if (days <= 0) return 'today';
  if (days === 1) return 'yesterday';
  if (days < 30) return days + 'd ago';
  if (days < 365) return Math.floor(days / 30) + 'mo ago';
  return Math.floor(days / 365) + 'y ago';
}

// ── Shared-memory read-through store ──────────────────────────────────────
// The graph + notes live in SHARED storage (/api/storage/shared/memory/),
// which `window.mobius.storage` cannot reach — that runtime hard-scopes every
// read to /api/storage/apps/${appId}/ — and the shell service worker sends all
// other /api/* straight to network, so a raw shared GET is blank offline and
// load-once (stale after an agent rewrite). This factory is the shared-scope
// twin of window.mobius.storage.get/getText/subscribe: read-through cache
// (last-known value served instantly, offline-capable), background revalidate,
// and a visibility-aware poller so subscribed views repaint when the chat or
// dreaming agent rewrites the file. Pure factory (deps injected) so the offline
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

export default function App({ appId, token }) {
  const [graph, setGraph] = useState(null);
  const [status, setStatus] = useState('loading'); // loading | ready | empty | error
  const [errMsg, setErrMsg] = useState('');
  const [view, setView] = useState('graph'); // graph | list
  const [selected, setSelected] = useState(null); // node object
  const [noteState, setNoteState] = useState({ status: 'idle', md: '', fm: {}, revalidating: false });
  const [hoverId, setHoverId] = useState(null);
  const [sortKey, setSortKey] = useState('access_count');
  const [sortDir, setSortDir] = useState('desc');
  const [showHealth, setShowHealth] = useState(false);
  const [localDepth, setLocalDepth] = useState(1);
  // Node-detail tab: 'text' shows the note, 'graph' shows the local graph.
  // Defaults to 'text' — the user arrives here from the global graph, so they
  // already have spatial context; the note body is what they came to read.
  // Only the active tab's pane mounts, so the Pixi local-graph renderer is
  // never resized (the old draggable split rebuilt it on every drag tick and
  // crashed). See the graph/text tab panes below.
  const [detailTab, setDetailTab] = useState('text');
  const [graphRuntime, setGraphRuntime] = useState(undefined); // undefined loading | null failed | { d3, PIXI }
  const [marked, setMarked] = useState(null);
  const [purify, setPurify] = useState(null); // DOMPurify — audited HTML sanitizer
  const panelNavRef = useRef(null);

  const wrapRef = useRef(null);
  const localWrapRef = useRef(null);
  const [dims, setDims] = useState({ w: 0, h: 0 });
  const [localDims, setLocalDims] = useState({ w: 0, h: 0 });

  // Read-through, offline-capable, subscribe-driven store over the SHARED
  // /api/storage/shared/memory/ route (see makeSharedMemoryStore). One per
  // token so a token refresh rebuilds it; the cache mirror is keyed by URL and
  // shared across instances, so the offline value survives the rebuild.
  const store = useMemo(
    () => makeSharedMemoryStore({ getToken: () => token }),
    [token],
  );

  // --- Load the Quartz-style graph renderer runtime. ---
  useEffect(() => {
    let alive = true;
    (async () => {
      try {
        await Promise.all([
          loadScriptOnce(D3_URL),
          loadScriptOnce(PIXI_URL),
        ]);
        const d3 = window.d3;
        const PIXI = window.PIXI;
        if (!d3 || !PIXI) throw new Error('Graph runtime scripts loaded without d3/PIXI globals.');
        if (alive) setGraphRuntime({ d3, PIXI });
      } catch (e) {
        // Graph view degrades to the list view if the runtime can't load.
        if (alive) setGraphRuntime(null);
      }
    })();
    return () => { alive = false; };
  }, []);

  // --- Subscribe to the graph index. ---
  // graph.json is rewritten by the chat + dreaming agents while this app sits
  // open, so it MUST subscribe, not load-once (a mount-only read leaves the
  // owner on a stale graph after an agent write). The store serves the cached
  // graph instantly (offline-capable) and repaints on every agent rewrite.
  useEffect(() => {
    setStatus('loading');
    const unsub = store.subscribe('graph.json', ({ body, present, error }) => {
      if (error && body == null) {
        setErrMsg(String(error.message || error));
        setStatus('error');
        return;
      }
      if (!present || body == null) {
        setGraph({ nodes: [], edges: [], problems: [] });
        setStatus('empty');
        return;
      }
      let data;
      try { data = JSON.parse(body); } catch {
        setErrMsg('graph.json is not valid JSON');
        setStatus('error');
        return;
      }
      const nodes = Array.isArray(data.nodes) ? data.nodes : [];
      setGraph({
        nodes,
        edges: Array.isArray(data.edges) ? data.edges : [],
        problems: Array.isArray(data.problems) ? data.problems : [],
      });
      setStatus(nodes.length === 0 ? 'empty' : 'ready');
    });
    return unsub;
  }, [store]);

  // --- Measure graph containers in CSS pixels; Pixi handles the DPR backing store. ---
  useEffect(() => {
    const el = wrapRef.current;
    if (!el) return;
    const ro = new ResizeObserver((entries) => {
      const r = entries[0].contentRect;
      setDims({ w: Math.round(r.width), h: Math.round(r.height) });
    });
    ro.observe(el);
    return () => ro.disconnect();
  }, [view, status]);

  // The local-graph host only exists in the DOM while the graph tab is active,
  // so re-run the measurement when the tab flips — not just when the node
  // changes. Without the detailTab dep the observer would attach to a stale
  // (or absent) element and the graph would never get non-zero dimensions.
  useEffect(() => {
    const el = localWrapRef.current;
    if (!el || !selected || detailTab !== 'graph') return;
    const ro = new ResizeObserver((entries) => {
      const r = entries[0].contentRect;
      setLocalDims({ w: Math.round(r.width), h: Math.round(r.height) });
    });
    ro.observe(el);
    return () => ro.disconnect();
  }, [selected, detailTab]);

  // --- Build a color map: stable moc-slug -> palette color. ---
  const mocColors = useMemo(() => {
    const map = {};
    if (!graph) return map;
    const mocSlugs = new Set();
    for (const n of graph.nodes) {
      if (n.type === 'moc') mocSlugs.add(n.id);
      if (Array.isArray(n.mocs)) for (const m of n.mocs) mocSlugs.add(m);
    }
    // Sort for determinism so colors don't reshuffle between loads.
    const sorted = [...mocSlugs].sort();
    sorted.forEach((slug, i) => {
      map[slug] = PALETTE[hashStr(slug) % PALETTE.length] || PALETTE[i % PALETTE.length];
    });
    return map;
  }, [graph]);

  const colorForNode = useCallback((n) => {
    if (n.type === 'moc') return cssVar('--accent', '#a78bfa');
    const primary = Array.isArray(n.mocs) && n.mocs.length ? n.mocs[0] : null;
    if (primary && mocColors[primary]) return mocColors[primary];
    return cssVar('--muted', '#8a8a93');
  }, [mocColors]);

  // --- Node radius from importance + usage. ---
  const radiusForNode = useCallback((n) => nodeRadius(n), []);

  // --- D3 mutates node objects (x/y/vx/vy) in place, so the renderer gets
  //     its own object references. Build once per graph. ---
  const fgData = useMemo(() => {
    if (!graph) return { nodes: [], links: [] };
    const showLabelAlways = graph.nodes.length <= 120;
    return {
      nodes: graph.nodes.map((n) => ({ ...n, showLabelAlways })),
      links: graph.edges.map((e) => ({
        source: typeof e.source === 'object' ? e.source.id : e.source,
        target: typeof e.target === 'object' ? e.target.id : e.target,
        kind: e.kind,
      })),
    };
  }, [graph]);

  const nodesById = useMemo(() => {
    const map = new Map();
    if (graph) for (const n of graph.nodes) map.set(n.id, n);
    return map;
  }, [graph]);

  const localGraphData = useMemo(
    () => buildLocalGraphData(graph, selected?.id, localDepth),
    [graph, selected, localDepth],
  );

  // --- Subscribe to the selected note body. ---
  // The open note is exactly the kind of view an agent can rewrite underneath
  // the owner (the chat appends to a note, dreaming reorganizes it), so it
  // subscribes too: cached body paints instantly (offline), and an external
  // write to this path repaints it. The `revalidating` flag drives the
  // "merging…" indicator, which clears once the fresh body has landed.
  useEffect(() => {
    if (!selected) return;
    // node.path comes from agent-written graph.json — refuse traversal,
    // absolute paths, and query/fragment smuggling before fetching.
    const path = safeMemoryPath(selected.path || ('notes/' + selected.id + '.md'));
    if (!path) {
      setNoteState({ status: 'missing', md: '', fm: {}, revalidating: false });
      return;
    }
    setNoteState({ status: 'loading', md: '', fm: {}, revalidating: false });
    const unsub = store.subscribe(
      path,
      ({ body, present, error }) => {
        if (error && body == null) {
          setNoteState({ status: 'error', md: String(error.message || error), fm: {}, revalidating: false });
          return;
        }
        if (!present || body == null) {
          setNoteState((s) => ({ status: 'missing', md: '', fm: {}, revalidating: s.revalidating }));
          return;
        }
        setNoteState((s) => ({
          status: 'ready',
          md: stripFrontmatter(body),
          fm: parseFrontmatter(body),
          revalidating: s.revalidating,
        }));
      },
      { onRevalidate: (busy) => setNoteState((s) => ({ ...s, revalidating: busy })) },
    );
    return unsub;
  }, [selected, store]);

  // --- Lazy-load the markdown renderer the first time we need it. ---
  useEffect(() => {
    if ((marked && purify) || !selected) return;
    let alive = true;
    (async () => {
      try {
        const [mk, dp] = await Promise.all([
          import('https://esm.sh/marked@14.1.4'),
          import('https://esm.sh/dompurify@3.1.7'),
        ]);
        if (alive) {
          setMarked(() => mk.marked || mk.default);
          setPurify(() => dp.default || dp);
        }
      } catch (e) {
        if (alive) { setMarked(null); setPurify(null); }
      }
    })();
    return () => { alive = false; };
  }, [selected, marked, purify]);

  const noteHtml = useMemo(() => {
    if (noteState.status !== 'ready') return '';
    // Plain markdown links/images are neutralized BEFORE wikilink expansion:
    // notes can carry reflection-agent web-research content, so remote URLs are
    // dropped (their label text survives). Wikilinks expand after, so the only
    // live anchors are the #memory-node- fragments this app generates itself.
    const linkedMd = renderWikiLinks(neutralizeMemoryMarkdown(noteState.md), graph?.nodes || []);
    // Require BOTH the renderer AND the sanitizer before producing HTML — never
    // render un-sanitized markup. Notes can contain reflection-agent web-research
    // content, so DOMPurify (a real HTML-parser sanitizer) is the right tool;
    // a regex net is routinely bypassed.
    if (marked && purify) {
      try {
        const raw = marked(linkedMd, { breaks: true, gfm: true });
        return restrictNoteHtml(purify.sanitize(raw, MEMORY_SANITIZE_OPTIONS));
      } catch { return escapeHtml(noteState.md); }
    }
    return null; // renderer not ready yet -> fall back to plain text below
  }, [noteState, marked, purify, graph]);

  const onNoteClick = useCallback((e) => {
    const a = e.target?.closest?.('a[href^="#memory-node-"]');
    if (!a) return;
    e.preventDefault();
    // Note bodies are agent-written and can carry malformed percent-encoding
    // (e.g. a stray `[x](#memory-node-%)`); decodeURIComponent throws URIError on
    // those. A bad fragment must dead-end as a no-op, never break the handler.
    const raw = a.getAttribute('href').replace('#memory-node-', '');
    let slug;
    try {
      slug = decodeURIComponent(raw);
    } catch {
      return;
    }
    const node = nodesById.get(slug);
    if (node) {
      setSelected(node);
      setHoverId(slug);
    }
  }, [nodesById]);

  const closePanel = useCallback(() => {
    setSelected(null);
    setHoverId(null);
  }, []);

  const openPanel = useCallback(async (node, opts = {}) => {
    if (!node) return;
    if (!selected && window.mobius?.nav?.open) {
      try { panelNavRef.current?.close?.(); } catch {}
      const handle = window.mobius.nav.open('memory-note', () => {
        panelNavRef.current = null;
        setSelected(null);
        setHoverId(null);
      });
      panelNavRef.current = handle;
      const ready = await handle.ready?.catch(() => false);
      if (panelNavRef.current !== handle) return;
      if (!ready) panelNavRef.current = null;
    }
    setSelected(node);
    setHoverId(opts.hoverId ?? null);
    setDetailTab('text'); // every node opens on its note, not the graph
    if (opts.resetLocalDepth) setLocalDepth(1);
  }, [selected]);

  const discuss = useCallback((node) => {
    const title = node.title || node.id;
    const draft = "Let's talk about what you know: " + title;
    window.parent.postMessage(
      { type: 'moebius:new-chat', draft },
      window.location.origin,
    );
  }, []);

  // Esc closes the panel — keyboard parity with the scrim tap.
  useEffect(() => {
    if (!selected) return;
    const onKey = (e) => { if (e.key === 'Escape') closePanel(); };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [selected, closePanel]);

  useEffect(() => {
    if (selected) return;
    try { panelNavRef.current?.close?.(); } catch {}
    panelNavRef.current = null;
  }, [!!selected]);

  useEffect(() => () => {
    try { panelNavRef.current?.close?.(); } catch {}
  }, []);

  // --- List view: sorted rows with plain usage/size metadata. ---
  const sortedNodes = useMemo(() => {
    if (!graph) return [];
    const rows = [...graph.nodes];
    rows.sort((a, b) => {
      let av, bv;
      if (sortKey === 'title') { av = (a.title || a.id).toLowerCase(); bv = (b.title || b.id).toLowerCase(); }
      else { av = a[sortKey] || 0; bv = b[sortKey] || 0; }
      if (av < bv) return sortDir === 'asc' ? -1 : 1;
      if (av > bv) return sortDir === 'asc' ? 1 : -1;
      return 0;
    });
    return rows;
  }, [graph, sortKey, sortDir]);

  const toggleSort = (key) => {
    if (sortKey === key) setSortDir((d) => (d === 'asc' ? 'desc' : 'asc'));
    else { setSortKey(key); setSortDir(key === 'title' ? 'asc' : 'desc'); }
  };

  const legendItems = useMemo(() => {
    if (!graph) return [];
    const items = [];
    const byId = {};
    for (const n of graph.nodes) byId[n.id] = n;
    for (const slug of Object.keys(mocColors).sort()) {
      const node = byId[slug];
      items.push({ slug, label: node ? (node.title || slug) : slug, color: mocColors[slug] });
    }
    return items;
  }, [graph, mocColors]);

  const problems = graph?.problems || [];
  const errCount = problems.filter((p) => p.severity === 'error').length;
  const counts = useMemo(() => {
    const c = { note: 0, moc: 0 };
    if (graph) for (const n of graph.nodes) c[n.type === 'moc' ? 'moc' : 'note']++;
    return c;
  }, [graph]);
  const selectedUpdated = relDate(noteState.fm.updated);

  // ---------------------------------------------------------------- render ---
  return (
    <div style={S.root}>
      <style>{CSS}</style>

      <header style={S.header}>
        <div style={S.brand}>
          {/* The app's own glossy icon as the brand mark; falls back to the
              accent dot if this install has no custom icon (the route 404s). */}
          <img
            src={`/api/apps/${appId}/icon?size=64`}
            alt=""
            width={34}
            height={34}
            style={S.brandIcon}
            onError={(e) => {
              e.currentTarget.style.display = 'none'
              const dot = e.currentTarget.nextElementSibling
              if (dot) dot.style.display = 'flex'
            }}
          />
          <span style={{ ...S.brandDot, display: 'none' }}><span style={S.brandDotCore} /></span>
          <div style={{ minWidth: 0 }}>
            <div style={S.subtitle}>
              {status === 'ready'
                ? `${counts.note + counts.moc} notes · ${graph.edges.length} links`
                : 'What the agent knows'}
            </div>
          </div>
        </div>

        <div style={S.headerRight}>
          {problems.length > 0 && (
            <button
              style={{ ...S.healthBadge, ...(errCount ? S.healthErr : S.healthWarn) }}
              onClick={() => setShowHealth((v) => !v)}
              title="Graph health"
            >
              <span style={{ ...S.healthDot, background: errCount ? 'var(--danger)' : 'var(--accent-hover, #f0c674)' }} />
              {problems.length}
            </button>
          )}
          <div style={S.toggle}>
            <button
              className="mg-tgl"
              style={{ ...S.toggleBtn, ...(view === 'graph' ? S.toggleActive : {}) }}
              onClick={() => setView('graph')}
            >
              <GraphGlyph /> Graph
            </button>
            <button
              className="mg-tgl"
              style={{ ...S.toggleBtn, ...(view === 'list' ? S.toggleActive : {}) }}
              onClick={() => setView('list')}
            >
              <ListGlyph /> List
            </button>
          </div>
        </div>
      </header>

      {showHealth && problems.length > 0 && (
        <div style={S.healthPanel} className="mg-scroll">
          <div style={S.healthHead}>
            {errCount > 0
              ? `${errCount} error${errCount === 1 ? '' : 's'} block the graph from rebuilding`
              : 'A few loose threads — nothing broken'}
          </div>
          {problems.map((p, i) => (
            <div key={i} style={S.healthRow}>
              <span style={{ ...S.sevTag, ...(p.severity === 'error' ? S.sevErr : S.sevWarn) }}>
                {p.severity}
              </span>
              <span style={S.healthKind}>{String(p.kind || '').replace(/_/g, ' ')}</span>
              <span style={S.healthDetail}>{p.detail}</span>
            </div>
          ))}
        </div>
      )}

      <main style={S.main}>
        {status === 'loading' && (
          <div style={S.center}>
            <div className="mg-orbit"><span /><span /><span /></div>
            <div style={S.centerText}>Reading the agent's memory…</div>
          </div>
        )}

        {status === 'error' && (
          <div style={S.center}>
            <div style={S.errIcon}>!</div>
            <div style={S.centerTitle}>Couldn't load the graph</div>
            <div style={S.centerText}>{errMsg}</div>
          </div>
        )}

        {status === 'empty' && (
          <div style={S.center}>
            <EmptyConstellation />
            <div style={S.centerTitle}>Memory is just getting to know you</div>
            <div style={S.centerText}>
              It fills in as you use Möbius — every chat, app, and habit leaves a
              trace here. Come back once you've given it something to remember.
            </div>
          </div>
        )}

        {status === 'ready' && view === 'graph' && (
          <div ref={wrapRef} style={S.graphWrap} className="mg-graph">
            {graphRuntime && dims.w > 0 && dims.h > 0 ? (
              <MemoryGraphRenderer
                runtime={graphRuntime}
                graphData={fgData}
                width={dims.w}
                height={dims.h}
                mode="global"
                selectedId={selected?.id}
                hoverId={hoverId}
                colorForNode={colorForNode}
                radiusForNode={radiusForNode}
                onNodeClick={(n) => openPanel(n, { resetLocalDepth: true })}
                onNodeHover={(n) => setHoverId(n ? n.id : null)}
                onBackgroundClick={closePanel}
              />
            ) : (
              <div style={S.center}>
                <div className="mg-orbit"><span /><span /><span /></div>
                <div style={S.centerText}>
                  {graphRuntime === null ? 'Graph view is offline — try List.' : 'Laying out the graph…'}
                </div>
              </div>
            )}

            <div style={S.graphHint}>Drag to pan · scroll to zoom · tap a node to read it</div>

            {legendItems.length > 0 && (
              <div style={S.legend} className="mg-scroll">
                <div style={S.legendTitle}>Maps of Content</div>
                <div style={S.legendRow}>
                  <span style={{ ...S.legendSwatch, background: cssVar('--accent', '#a78bfa') }} />
                  <span style={S.legendLabel}>Hub (MOC)</span>
                </div>
                {legendItems.slice(0, 12).map((it) => (
                  <button
                    key={it.slug}
                    style={S.legendRow}
                    className="mg-legend-row"
                    onMouseEnter={() => setHoverId(it.slug)}
                    onMouseLeave={() => setHoverId(null)}
                    onClick={() => {
                      const n = graph.nodes.find((x) => x.id === it.slug);
                      if (n) openPanel(n);
                    }}
                  >
                    <span style={{ ...S.legendSwatch, background: it.color }} />
                    <span style={S.legendLabel}>{it.label}</span>
                  </button>
                ))}
              </div>
            )}
          </div>
        )}

        {status === 'ready' && view === 'list' && (
          <div style={S.listWrap} className="mg-scroll">
            <table style={S.table}>
              <thead>
                <tr>
                  <Th label="Note" active={sortKey === 'title'} dir={sortDir} onSort={() => toggleSort('title')} align="left" />
                  <Th label="Type" />
                  <Th label="Weight" active={sortKey === 'importance'} dir={sortDir} onSort={() => toggleSort('importance')} />
                  <Th label="Reads" active={sortKey === 'access_count'} dir={sortDir} onSort={() => toggleSort('access_count')} />
                  <Th label="Size" active={sortKey === 'size_bytes'} dir={sortDir} onSort={() => toggleSort('size_bytes')} />
                </tr>
              </thead>
              <tbody>
                {sortedNodes.map((n) => (
                  <tr
                    key={n.id}
                    style={S.tr}
                    onClick={() => openPanel(n)}
                    className="mg-row"
                    role="button"
                    tabIndex={0}
                    aria-label={`Open ${n.title || n.id}`}
                    onKeyDown={(e) => {
                      // Enter/Space activate the row like a button; preventDefault
                      // on Space stops the list pane scrolling instead of opening.
                      if (e.key === 'Enter' || e.key === ' ') {
                        e.preventDefault();
                        openPanel(n);
                      }
                    }}
                  >
                    <td style={S.tdTitle}>
                      <span style={{ ...S.rowDot, background: colorForNode(n) }} />
                      <span style={S.rowTitleText}>{n.title || n.id}</span>
                    </td>
                    <td style={S.td}>
                      <span style={{ ...S.typeTag, ...(n.type === 'moc' ? S.typeMoc : {}) }}>
                        {n.type === 'moc' ? 'hub' : 'note'}
                      </span>
                    </td>
                    <td style={{ ...S.td, ...S.tdNum }}>
                      <ImportanceDots value={n.importance || 1} />
                    </td>
                    <td style={{ ...S.td, ...S.tdMeta }}>{n.access_count || 0}</td>
                    <td style={{ ...S.td, ...S.tdMeta }}>{fmtBytes(n.size_bytes)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </main>

      {/* ----------------------------------------------------- note panel --- */}
      {selected && (
        <>
          <div style={S.scrim} className="mg-scrim" onClick={closePanel} />
          <aside style={S.panel} className="mg-panel">
            <div style={{ ...S.panelAccent, background: colorForNode(selected) }} />
            <div style={S.panelHead} className="mg-panel-head">
              <div style={S.panelHeadMain}>
                <span style={{ ...S.rowDot, background: colorForNode(selected), width: 11, height: 11, marginTop: 5 }} />
                <div style={{ minWidth: 0 }}>
                  <div style={S.panelTitle}>{selected.title || selected.id}</div>
                  <div style={S.panelMetaLine}>
                    <span>{selected.type === 'moc' ? 'Hub' : 'Note'}</span>
                    <span>Weight <ImportanceDots value={selected.importance || 1} /></span>
                    <span>{selected.access_count || 0} reads</span>
                    <span>{fmtBytes(selected.size_bytes)}</span>
                    {selectedUpdated && <span>{selectedUpdated}</span>}
                  </div>
                </div>
              </div>
              <button style={S.closeBtn} className="mg-close" onClick={closePanel} aria-label="Close">×</button>
            </div>

            {Array.isArray(selected.tags) && selected.tags.length > 0 && (
              <div style={S.tagRow} className="mg-tag-row">
                {selected.tags.map((t) => <span key={t} style={S.tag}>#{t}</span>)}
              </div>
            )}

            {/* Tab toggle replaces the old resizable note/graph split. Only the
                active tab's pane mounts: keeping the local graph unmounted while
                reading text means the Pixi renderer is never resized, which was
                the entire crash class the draggable divider produced. */}
            <div style={S.detailBar}>
              <div style={S.detailContext}>
                {detailTab === 'graph' ? (
                  <>
                    <span style={S.paneHead}>Local graph</span>
                    <span style={S.localCount}>
                      {localGraphData.nodes.length} nodes · {localGraphData.links.length} links
                    </span>
                  </>
                ) : (
                  <span style={S.paneHead}>Note</span>
                )}
              </div>

              {detailTab === 'graph' && (
                <div style={S.depthToggle} aria-label="Local graph depth">
                  {[1, 2, 3, 4].map((d) => (
                    <button
                      key={d}
                      style={{ ...S.depthBtn, ...(localDepth === d ? S.depthBtnActive : {}) }}
                      onClick={() => setLocalDepth(d)}
                      title={`${d} hop${d === 1 ? '' : 's'}`}
                    >
                      {d}
                    </button>
                  ))}
                </div>
              )}

              <div style={S.tabToggle} role="tablist" aria-label="Note or local graph">
                <button
                  className="mg-tab"
                  style={{ ...S.tabBtn, ...(detailTab === 'text' ? S.tabBtnActive : {}) }}
                  onClick={() => setDetailTab('text')}
                  role="tab"
                  aria-selected={detailTab === 'text'}
                  aria-label="Show note text"
                  title="Note text"
                >
                  <TextGlyph />
                </button>
                <button
                  className="mg-tab"
                  style={{ ...S.tabBtn, ...(detailTab === 'graph' ? S.tabBtnActive : {}) }}
                  onClick={() => setDetailTab('graph')}
                  role="tab"
                  aria-selected={detailTab === 'graph'}
                  aria-label="Show local graph"
                  title="Local graph"
                >
                  <NetworkGlyph />
                </button>
              </div>
            </div>

            <div style={S.detailBody}>
              {detailTab === 'text' ? (
                <div style={S.panelBody} className="mg-md mg-scroll" onClick={onNoteClick}>
                  {noteState.status === 'loading' && (
                    <div style={S.notePlaceholder}>
                      <div className="mg-skel" style={{ width: '70%' }} />
                      <div className="mg-skel" style={{ width: '95%' }} />
                      <div className="mg-skel" style={{ width: '88%' }} />
                      <div className="mg-skel" style={{ width: '60%' }} />
                    </div>
                  )}
                  {noteState.status === 'missing' && <div style={S.centerText}>No note body on disk for this entry.</div>}
                  {noteState.status === 'error' && <div style={S.centerText}>Couldn't load: {noteState.md}</div>}
                  {noteState.status === 'ready' && noteState.revalidating && (
                    <div style={S.mergePill} role="status" aria-live="polite">
                      <span style={S.mergeDot} aria-hidden="true" />
                      Merging latest…
                    </div>
                  )}
                  {noteState.status === 'ready' && (
                    noteHtml != null
                      ? <div dangerouslySetInnerHTML={{ __html: noteHtml }} />
                      : <pre style={S.pre}>{noteState.md}</pre>
                  )}
                </div>
              ) : (
                <div ref={localWrapRef} style={S.localGraphWrap} className="mg-local-graph">
                  {graphRuntime && localDims.w > 0 && localDims.h > 0 && localGraphData.nodes.length > 0 ? (
                    <MemoryGraphRenderer
                      runtime={graphRuntime}
                      graphData={localGraphData}
                      width={localDims.w}
                      height={localDims.h}
                      mode="local"
                      selectedId={selected?.id}
                      hoverId={hoverId}
                      colorForNode={colorForNode}
                      radiusForNode={radiusForNode}
                      onNodeClick={(n) => openPanel(nodesById.get(n.id) || n)}
                      onNodeHover={(n) => setHoverId(n ? n.id : null)}
                    />
                  ) : (
                    <div style={S.localEmpty}>
                      {graphRuntime === null ? 'Graph view is offline.' : 'Laying out local graph…'}
                    </div>
                  )}
                </div>
              )}
            </div>

            <div style={S.panelFoot}>
              <button style={S.discussBtn} className="mg-discuss" onClick={() => discuss(selected)}>
                <ChatGlyph />
                Discuss in a new chat
              </button>
            </div>
          </aside>
        </>
      )}
    </div>
  );
}

// ------------------------------------------------------------- subcomponents ---

function Th({ label, subLabel, active, dir, onSort, align }) {
  // scope=col names the column for assistive tech; aria-sort reflects the live
  // sort on the one active column ('ascending'/'descending') and 'none' on the
  // other sortable columns, so a screen reader announces the current ordering.
  const ariaSort = onSort
    ? (active ? (dir === 'asc' ? 'ascending' : 'descending') : 'none')
    : undefined;
  const inner = (
    <>
      <span style={S.thMain}>
        {label}
        {active && <span style={S.sortCaret}>{dir === 'asc' ? '↑' : '↓'}</span>}
      </span>
      {subLabel && <span style={S.thSub}>{subLabel}</span>}
    </>
  );
  // Sortable headers are a real <button> inside the <th>: native Enter/Space
  // activation + focus, instead of an un-focusable <th onClick>. A non-sortable
  // header (e.g. Type) stays a plain, non-interactive cell.
  if (onSort) {
    return (
      <th scope="col" style={{ ...S.th, textAlign: align || 'right' }} aria-sort={ariaSort}>
        <button
          type="button"
          className="mg-th"
          style={{ ...S.thButton, justifyContent: align === 'left' ? 'flex-start' : 'flex-end' }}
          onClick={onSort}
        >
          {inner}
        </button>
      </th>
    );
  }
  return (
    <th scope="col" style={{ ...S.th, textAlign: align || 'right' }}>
      {inner}
    </th>
  );
}

function MemoryGraphRenderer({
  runtime,
  graphData,
  width,
  height,
  mode,
  selectedId,
  hoverId,
  colorForNode,
  radiusForNode,
  onNodeClick,
  onNodeHover,
  onBackgroundClick,
}) {
  const hostRef = useRef(null);
  const latestRef = useRef({});

  useEffect(() => {
    latestRef.current = {
      selectedId,
      hoverId,
      colorForNode,
      radiusForNode,
      onNodeClick,
      onNodeHover,
      onBackgroundClick,
    };
  }, [selectedId, hoverId, colorForNode, radiusForNode, onNodeClick, onNodeHover, onBackgroundClick]);

  useEffect(() => {
    const host = hostRef.current;
    if (!host || !runtime || width <= 0 || height <= 0) return undefined;

    let disposed = false;
    let cleanup = () => {};
    host.replaceChildren();

    createMemoryGraphScene({
      host,
      runtime,
      graphData,
      width,
      height,
      mode,
      latestRef,
      isDisposed: () => disposed,
    }).then((nextCleanup) => {
      if (disposed) {
        try { nextCleanup?.(); } catch {}
      } else {
        cleanup = nextCleanup || cleanup;
      }
    }).catch((err) => {
      console.error('[Memory] Graph renderer failed', err);
      if (!disposed) host.textContent = 'Graph could not render.';
    });

    return () => {
      disposed = true;
      try { cleanup(); } catch {}
      host.replaceChildren();
    };
  }, [runtime, graphData, width, height, mode]);

  return (
    <div
      ref={hostRef}
      style={S.pixiGraph}
      className="mg-pixi-graph"
      aria-label={mode === 'local' ? 'Local note graph' : 'Memory graph'}
    />
  );
}

async function createMemoryGraphScene({
  host,
  runtime,
  graphData,
  width,
  height,
  mode,
  latestRef,
  isDisposed,
}) {
  const { d3, PIXI } = runtime;
  const graph = normalizeRendererGraphData(graphData, width, height);
  const app = new PIXI.Application();
  await app.init({
    width,
    height,
    antialias: true,
    backgroundAlpha: 0,
    autoDensity: true,
    resolution: window.devicePixelRatio || 1,
    // Drive rendering ourselves (below) so each app.render() is wrapped in a
    // guard — a single bad batcher frame can never take down the whole app.
    autoStart: false,
  });
  if (isDisposed()) {
    try { app.destroy(true, { children: true, texture: true, textureSource: true }); } catch {}
    return () => {};
  }

  const canvas = app.canvas || app.view;
  canvas.style.width = '100%';
  canvas.style.height = '100%';
  canvas.style.display = 'block';
  canvas.style.touchAction = 'none';
  host.appendChild(canvas);

  const scene = new PIXI.Container();
  const linkLayer = new PIXI.Graphics();
  const nodeLayer = new PIXI.Container();
  const labelLayer = new PIXI.Container();
  scene.addChild(linkLayer);
  scene.addChild(nodeLayer);
  scene.addChild(labelLayer);
  app.stage.addChild(scene);

  const neighbors = buildRendererNeighborMap(graph.links);
  const labelRanks = buildLabelRankMap(graph.nodes);
  const focus = new Map(graph.nodes.map((node) => [node.id, 1]));
  let currentTransform = d3.zoomIdentity.translate(width / 2, height / 2).scale(mode === 'local' ? 0.95 : 0.82);
  let lastFrame = performance.now();
  let dragStart = null;
  let activeDragNode = null;
  let lastNodeClickAt = 0;
  let lastHoverId = null;

  const linkDistance = mode === 'local' ? 42 : 64;
  const chargeStrength = mode === 'local' ? -120 : -185;
  const centerForce = d3.forceCenter(0, 0);
  if (typeof centerForce.strength === 'function') centerForce.strength(mode === 'local' ? 0.12 : 0.06);

  const simulation = d3.forceSimulation(graph.nodes)
    .force('charge', d3.forceManyBody().strength((node) => node.type === 'moc' ? chargeStrength * 1.25 : chargeStrength))
    .force('link', d3.forceLink(graph.links).distance((link) => link.kind === 'moc' ? linkDistance * 0.82 : linkDistance).strength((link) => link.kind === 'moc' ? 0.42 : 0.22))
    .force('center', centerForce)
    .force('collide', d3.forceCollide().radius((node) => latestRadius(node) + 8).iterations(2))
    .velocityDecay(mode === 'local' ? 0.34 : 0.3)
    .stop();

  simulation.tick(mode === 'local' ? 80 : 130);

  const renderNodes = graph.nodes.map((node) => {
    const gfx = new PIXI.Graphics();
    const label = new PIXI.Container();
    const labelBg = new PIXI.Graphics();
    const labelText = new PIXI.Text({
      text: truncateGraphLabel(node.title || node.id),
      style: {
        fontFamily: graphFontFamily(),
        fontSize: node.type === 'moc' ? 12 : 11,
        fontWeight: node.type === 'moc' ? '700' : '650',
        fill: colorNumber(cssVar('--text', '#e5e5e5'), '#e5e5e5'),
      },
      resolution: (window.devicePixelRatio || 1) * 3,
    });
    labelText.anchor.set(0.5, 0);
    label.addChild(labelBg);
    label.addChild(labelText);
    nodeLayer.addChild(gfx);
    labelLayer.addChild(label);
    return { node, gfx, label, labelBg, labelText };
  });

  function latestRadius(node) {
    return latestRef.current.radiusForNode?.(node) ?? nodeRadius(node);
  }

  function latestColor(node) {
    return latestRef.current.colorForNode?.(node) || cssVar('--muted', '#8a8a93');
  }

  function isFocused(id) {
    const hovered = latestRef.current.hoverId;
    if (!hovered) return true;
    return id === hovered || neighbors.get(hovered)?.has(id);
  }

  function focusOf(id, dt) {
    const goal = isFocused(id) ? 1 : 0.18;
    const current = focus.get(id) ?? 1;
    const k = 1 - Math.pow(0.002, Math.min(48, dt) / 1000);
    const next = current + (goal - current) * k;
    focus.set(id, next);
    return next;
  }

  function applyTransform(transform) {
    currentTransform = transform;
    scene.position.set(transform.x, transform.y);
    scene.scale.set(transform.k, transform.k);
  }

  function hitNode(screenX, screenY) {
    const k = currentTransform.k || 1;
    const x = (screenX - currentTransform.x) / k;
    const y = (screenY - currentTransform.y) / k;
    let best = null;
    let bestDist = Infinity;
    for (const node of graph.nodes) {
      if (!Number.isFinite(node.x) || !Number.isFinite(node.y)) continue;
      const dx = x - node.x;
      const dy = y - node.y;
      const dist = Math.sqrt(dx * dx + dy * dy);
      const hitR = latestRadius(node) + 10 / k;
      if (dist <= hitR && dist < bestDist) {
        best = node;
        bestDist = dist;
      }
    }
    return best;
  }

  function draw() {
    if (isDisposed()) return;
    const now = performance.now();
    const dt = now - lastFrame;
    lastFrame = now;
    const hover = latestRef.current.hoverId;
    const selected = latestRef.current.selectedId;
    const textColor = colorNumber(cssVar('--text', '#e5e5e5'), '#e5e5e5');
    const bgColor = colorNumber(cssVar('--bg', '#0d0d0d'), '#0d0d0d');
    const borderColor = colorNumber(cssVar('--text', '#e5e5e5'), '#e5e5e5');
    const linkColor = colorNumber(cssVar('--text', '#e5e5e5'), '#e5e5e5');
    const accentColor = colorNumber(cssVar('--accent', '#a78bfa'), '#a78bfa');
    const scale = currentTransform.k || 1;

    linkLayer.clear();
    for (const link of graph.links) {
      const s = link.source;
      const t = link.target;
      if (!Number.isFinite(s.x) || !Number.isFinite(s.y) || !Number.isFinite(t.x) || !Number.isFinite(t.y)) continue;
      const f = Math.min(focus.get(s.id) ?? 1, focus.get(t.id) ?? 1);
      const isMoc = link.kind === 'moc';
      linkLayer.moveTo(s.x, s.y);
      linkLayer.lineTo(t.x, t.y);
      linkLayer.stroke({
        width: isMoc ? 1.55 : 0.8,
        color: isMoc ? accentColor : linkColor,
        alpha: (isMoc ? 0.48 : 0.28) * (0.18 + 0.82 * f),
      });
    }

    for (const item of renderNodes) {
      const { node, gfx, label, labelBg, labelText } = item;
      if (!Number.isFinite(node.x) || !Number.isFinite(node.y)) continue;
      const f = focusOf(node.id, dt);
      const r = latestRadius(node);
      const color = colorNumber(latestColor(node), '#8a8a93');
      const isHover = hover === node.id;
      const isSelected = selected === node.id;
      const isHub = node.type === 'moc';

      // A node is a flat colored disc with a thin ring — no glow halo, no
      // specular highlight dot (owner asked to simplify: the color carries the
      // identity, the ring carries hover/selected emphasis).
      gfx.clear();
      gfx.position.set(node.x, node.y);
      gfx.circle(0, 0, r);
      gfx.fill({ color, alpha: 0.18 + 0.82 * f });
      gfx.circle(0, 0, r);
      gfx.stroke({
        width: isHover || isSelected || isHub ? 1.4 / scale : 0.85 / scale,
        color: isHover || isSelected ? accentColor : borderColor,
        alpha: isHover || isSelected || isHub ? 0.62 : 0.24 * f,
      });

      const rank = labelRanks.get(node.id) ?? 9999;
      const showLabel = shouldShowScreenLabel(node, scale, rank, { mode, hoverId: hover, selectedId: selected });
      // Hide via alpha rather than .visible — toggling visibility on a label
      // mid-frame has tripped the Pixi v8 batcher; alpha=0 is the safe mute.
      if (!showLabel) {
        label.alpha = 0;
        continue;
      }

      const strong = isHub || isHover || isSelected || node.localDepth === 0;
      label.alpha = strong ? 1 : 0.7 + 0.25 * f;
      label.scale.set(1 / scale);
      label.position.set(node.x, node.y + r + 5 / scale);
      labelText.style.fill = textColor;
      labelText.style.fontSize = strong ? 12 : 11;
      labelText.style.fontWeight = strong ? '700' : '650';
      labelText.position.set(0, 2);

      const w = Math.ceil(labelText.width + 12);
      const h = Math.ceil(labelText.height + 5);
      labelBg.clear();
      labelBg.roundRect(-w / 2, 0, w, h, 7);
      labelBg.fill({ color: bgColor, alpha: strong ? 0.88 : 0.74 });
      labelBg.roundRect(-w / 2, 0, w, h, 7);
      labelBg.stroke({ width: 1, color: borderColor, alpha: strong ? 0.26 : 0.16 });
    }
  }

  const selection = d3.select(canvas);
  const zoom = d3.zoom()
    .extent([[0, 0], [width, height]])
    .scaleExtent([0.22, 4.6])
    .filter((event) => {
      if (event.type === 'mousedown') return !hitNode(event.offsetX, event.offsetY);
      if (event.type === 'touchstart') {
        // Same node-exclusion as mousedown. Without it, zoom and drag both
        // claim the touch (touch events skip the mousedown branch) and
        // dragging a node also pans the whole scene. Touch events carry no
        // offsetX/Y, so map the first touch into canvas coordinates by hand.
        const touch = event.touches?.[0];
        if (touch) {
          const rect = canvas.getBoundingClientRect();
          return !hitNode(touch.clientX - rect.left, touch.clientY - rect.top);
        }
      }
      return true;
    })
    .on('zoom', (event) => {
      applyTransform(event.transform);
      draw();
    });

  selection.call(zoom);
  const fit = computeRendererFitTransform(graph.nodes, width, height, {
    padding: mode === 'local' ? 34 : 72,
    minScale: mode === 'local' ? 0.72 : 0.42,
    maxScale: mode === 'local' ? 1.45 : 1.08,
  });
  selection.call(zoom.transform, d3.zoomIdentity.translate(fit.x, fit.y).scale(fit.k));

  const drag = d3.drag()
    .container(canvas)
    .subject((event) => hitNode(event.x, event.y))
    .on('start', (event) => {
      if (!event.subject) return;
      activeDragNode = event.subject;
      dragStart = { x: event.x, y: event.y, t: Date.now() };
      event.sourceEvent?.stopPropagation?.();
      simulation.alphaTarget(0.2).restart();
      event.subject.fx = event.subject.x;
      event.subject.fy = event.subject.y;
      latestRef.current.onNodeHover?.(event.subject);
    })
    .on('drag', (event) => {
      if (!event.subject) return;
      const k = currentTransform.k || 1;
      event.subject.fx = (event.x - currentTransform.x) / k;
      event.subject.fy = (event.y - currentTransform.y) / k;
    })
    .on('end', (event) => {
      if (!event.subject) return;
      event.sourceEvent?.stopPropagation?.();
      simulation.alphaTarget(0);
      event.subject.fx = null;
      event.subject.fy = null;
      const moved = dragStart
        ? Math.hypot(event.x - dragStart.x, event.y - dragStart.y)
        : Infinity;
      const quick = dragStart ? Date.now() - dragStart.t < 520 : false;
      activeDragNode = null;
      dragStart = null;
      if (moved < 7 && quick) {
        lastNodeClickAt = Date.now();
        latestRef.current.onNodeClick?.(event.subject);
      }
    });

  selection.call(drag);

  const onPointerMove = (event) => {
    if (activeDragNode) return;
    const node = hitNode(event.offsetX, event.offsetY);
    const nextId = node?.id || null;
    if (nextId === lastHoverId) return;
    lastHoverId = nextId;
    latestRef.current.onNodeHover?.(node || null);
  };
  const onPointerLeave = () => {
    lastHoverId = null;
    latestRef.current.onNodeHover?.(null);
  };
  const onCanvasClick = (event) => {
    if (Date.now() - lastNodeClickAt < 160) return;
    if (hitNode(event.offsetX, event.offsetY)) return;
    latestRef.current.onBackgroundClick?.();
  };
  canvas.addEventListener('pointermove', onPointerMove);
  canvas.addEventListener('pointerleave', onPointerLeave);
  canvas.addEventListener('click', onCanvasClick);

  // Self-driven render loop. app.render() is in a try/catch so a single bad
  // Pixi batcher frame is skipped (logged once) instead of throwing out of the
  // ticker and tearing down the whole app.
  let rafId = 0;
  let renderErrorLogged = false;
  const frame = () => {
    if (isDisposed()) return;
    draw();
    try {
      app.render();
    } catch (err) {
      if (!renderErrorLogged) {
        renderErrorLogged = true;
        console.warn('[Memory] Skipped a bad render frame', err);
      }
    }
    rafId = requestAnimationFrame(frame);
  };
  simulation.alpha(mode === 'local' ? 0.22 : 0.34).restart();
  rafId = requestAnimationFrame(frame);

  return () => {
    canvas.removeEventListener('pointermove', onPointerMove);
    canvas.removeEventListener('pointerleave', onPointerLeave);
    canvas.removeEventListener('click', onCanvasClick);
    try { selection.on('.zoom', null).on('.drag', null); } catch {}
    simulation.stop();
    if (rafId) cancelAnimationFrame(rafId);
    // Destroy children + textures explicitly: every label owns a canvas-backed
    // texture, and resize remounts rebuild the whole scene — without this the
    // old scene's textures linger until GC gets around to them.
    try { app.destroy(true, { children: true, texture: true, textureSource: true }); } catch {}
  };
}

export function normalizeRendererGraphData(graphData = {}, width = 0, height = 0) {
  const rawNodes = Array.isArray(graphData.nodes) ? graphData.nodes : [];
  const rawLinks = Array.isArray(graphData.links) ? graphData.links : [];
  const spread = Math.max(80, Math.min(Math.max(width, 1), Math.max(height, 1)) * 0.34);
  const nodes = rawNodes
    .filter((node) => node && node.id)
    .map((node, index) => {
      const seeded = seededGraphPosition(node.id, index, rawNodes.length || 1, spread);
      return {
        ...node,
        x: Number.isFinite(node.x) ? node.x : seeded.x,
        y: Number.isFinite(node.y) ? node.y : seeded.y,
      };
    });
  const byId = new Map(nodes.map((node) => [node.id, node]));
  const links = rawLinks
    .map((link) => {
      const sourceId = typeof link.source === 'object' ? link.source.id : link.source;
      const targetId = typeof link.target === 'object' ? link.target.id : link.target;
      const source = byId.get(sourceId);
      const target = byId.get(targetId);
      if (!source || !target) return null;
      return { ...link, source, target, sourceId, targetId };
    })
    .filter(Boolean);
  return { nodes, links };
}

export function computeRendererFitTransform(nodes = [], width = 0, height = 0, opts = {}) {
  const finiteNodes = (Array.isArray(nodes) ? nodes : [])
    .filter((node) => Number.isFinite(node.x) && Number.isFinite(node.y));
  const padding = Number.isFinite(opts.padding) ? opts.padding : 64;
  const minScale = Number.isFinite(opts.minScale) ? opts.minScale : 0.35;
  const maxScale = Number.isFinite(opts.maxScale) ? opts.maxScale : 1.15;
  if (!finiteNodes.length || width <= 0 || height <= 0) {
    return { x: width / 2, y: height / 2, k: 1 };
  }
  let minX = Infinity;
  let minY = Infinity;
  let maxX = -Infinity;
  let maxY = -Infinity;
  for (const node of finiteNodes) {
    minX = Math.min(minX, node.x);
    minY = Math.min(minY, node.y);
    maxX = Math.max(maxX, node.x);
    maxY = Math.max(maxY, node.y);
  }
  const graphW = Math.max(1, maxX - minX);
  const graphH = Math.max(1, maxY - minY);
  const scale = clamp(
    Math.min((width - padding * 2) / graphW, (height - padding * 2) / graphH),
    minScale,
    maxScale,
  );
  const cx = (minX + maxX) / 2;
  const cy = (minY + maxY) / 2;
  return {
    x: width / 2 - cx * scale,
    y: height / 2 - cy * scale,
    k: scale,
  };
}

function buildRendererNeighborMap(links = []) {
  const map = new Map();
  const add = (a, b) => {
    if (!map.has(a)) map.set(a, new Set());
    map.get(a).add(b);
  };
  for (const link of links) {
    const s = link.source?.id || link.sourceId || link.source;
    const t = link.target?.id || link.targetId || link.target;
    if (!s || !t) continue;
    add(s, t);
    add(t, s);
  }
  return map;
}

function buildLabelRankMap(nodes = []) {
  const ranked = [...nodes]
    .map((node) => ({ node, score: labelScore(node) }))
    .sort((a, b) => b.score - a.score);
  return new Map(ranked.map(({ node }, index) => [node.id, index]));
}

function seededGraphPosition(id, index, total, spread) {
  const h = hashStr(String(id));
  const angle = ((h % 3600) / 3600) * Math.PI * 2;
  const ring = 0.35 + ((hashStr(String(id) + ':r') % 1000) / 1000) * 0.65;
  const fallbackAngle = total > 0 ? (index / total) * Math.PI * 2 : angle;
  const a = Number.isFinite(angle) ? angle : fallbackAngle;
  return {
    x: Math.cos(a) * spread * ring,
    y: Math.sin(a) * spread * ring,
  };
}

function truncateGraphLabel(label) {
  const text = String(label || '');
  if (text.length <= 34) return text;
  return text.slice(0, 31).trimEnd() + '...';
}

function graphFontFamily() {
  try {
    return cssVar('--font', 'Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif');
  } catch {
    return 'Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif';
  }
}

function colorNumber(color, fallback) {
  const rgb = parseRGB(color) || parseRGB(fallback) || [138, 138, 147];
  return (rgb[0] << 16) + (rgb[1] << 8) + rgb[2];
}

// Importance 1..5 rendered as filled/empty pips — calmer than a raw number,
// and it reads as a rating at a glance.
function ImportanceDots({ value }) {
  const v = Math.max(1, Math.min(5, value | 0));
  return (
    <span style={S.dotsWrap} title={`importance ${v}/5`}>
      {[1, 2, 3, 4, 5].map((i) => (
        <span key={i} style={{ ...S.pip, ...(i <= v ? S.pipOn : {}) }} />
      ))}
    </span>
  );
}

// A small constellation drawn in SVG for the empty state — on-brand, not a
// generic spinner. Theme-aware via currentColor / theme vars.
function EmptyConstellation() {
  return (
    <svg width="132" height="96" viewBox="0 0 132 96" fill="none" style={{ opacity: 0.92 }}>
      <g className="mg-constellation">
        <line x1="66" y1="48" x2="30" y2="26" stroke="var(--border)" strokeWidth="1.2" />
        <line x1="66" y1="48" x2="104" y2="30" stroke="var(--border)" strokeWidth="1.2" />
        <line x1="66" y1="48" x2="42" y2="74" stroke="var(--border)" strokeWidth="1.2" />
        <line x1="66" y1="48" x2="96" y2="70" stroke="var(--border)" strokeWidth="1.2" />
        <line x1="30" y1="26" x2="42" y2="74" stroke="var(--border)" strokeWidth="0.8" strokeDasharray="2 3" />
        <circle cx="30" cy="26" r="3.5" fill="var(--muted)" className="mg-star" style={{ animationDelay: '0.1s' }} />
        <circle cx="104" cy="30" r="3" fill="var(--muted)" className="mg-star" style={{ animationDelay: '0.6s' }} />
        <circle cx="42" cy="74" r="3" fill="var(--muted)" className="mg-star" style={{ animationDelay: '1.1s' }} />
        <circle cx="96" cy="70" r="2.6" fill="var(--muted)" className="mg-star" style={{ animationDelay: '1.5s' }} />
        <circle cx="66" cy="48" r="7" fill="var(--accent)" className="mg-star-hub" />
        <circle cx="66" cy="48" r="11" fill="none" stroke="var(--accent)" strokeWidth="1" opacity="0.35" className="mg-pulse" />
      </g>
    </svg>
  );
}

function GraphGlyph() {
  return (
    <svg width="13" height="13" viewBox="0 0 16 16" fill="none" aria-hidden="true" style={{ marginRight: 5 }}>
      <circle cx="4" cy="4" r="2.2" fill="currentColor" />
      <circle cx="12" cy="6" r="2.2" fill="currentColor" />
      <circle cx="6" cy="12" r="2.2" fill="currentColor" />
      <path d="M5.4 5.2 10.6 5.6M5 5.8 6 10.4" stroke="currentColor" strokeWidth="1.1" />
    </svg>
  );
}

function ListGlyph() {
  return (
    <svg width="13" height="13" viewBox="0 0 16 16" fill="none" aria-hidden="true" style={{ marginRight: 5 }}>
      <path d="M3 4h10M3 8h10M3 12h7" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" />
    </svg>
  );
}

function ChatGlyph() {
  return (
    <svg width="15" height="15" viewBox="0 0 16 16" fill="none" aria-hidden="true" style={{ marginRight: 7 }}>
      <path d="M2.5 4.5a1.5 1.5 0 0 1 1.5-1.5h8a1.5 1.5 0 0 1 1.5 1.5v5A1.5 1.5 0 0 1 12 11H6l-3 2.5V11H4a1.5 1.5 0 0 1-1.5-1.5v-5Z"
        stroke="currentColor" strokeWidth="1.3" strokeLinejoin="round" />
    </svg>
  );
}

// Document glyph for the "note text" tab — a page with a few ruled lines.
function TextGlyph() {
  return (
    <svg width="15" height="15" viewBox="0 0 16 16" fill="none" aria-hidden="true">
      <path d="M4 2.5h5l3 3V13a0.5 0.5 0 0 1-0.5 0.5h-7A0.5 0.5 0 0 1 4 13V3a0.5 0.5 0 0 1 0.5-0.5Z"
        stroke="currentColor" strokeWidth="1.2" strokeLinejoin="round" />
      <path d="M9 2.5V5.5h3" stroke="currentColor" strokeWidth="1.2" strokeLinejoin="round" />
      <path d="M6 8h4M6 10.5h4" stroke="currentColor" strokeWidth="1.1" strokeLinecap="round" />
    </svg>
  );
}

// Network glyph for the "local graph" tab — three linked nodes.
function NetworkGlyph() {
  return (
    <svg width="15" height="15" viewBox="0 0 16 16" fill="none" aria-hidden="true">
      <path d="M5.4 5.4 10.3 4M4.8 6.6 7 10.6" stroke="currentColor" strokeWidth="1.1" />
      <circle cx="4" cy="5" r="2.1" fill="currentColor" />
      <circle cx="12" cy="3.6" r="2" fill="currentColor" />
      <circle cx="8" cy="12" r="2.1" fill="currentColor" />
    </svg>
  );
}

// ----------------------------------------------------------------- helpers ---

function loadScriptOnce(src) {
  if (typeof document === 'undefined') return Promise.reject(new Error('document is not available'));
  const existing = document.querySelector(`script[src="${src}"]`);
  if (existing?.dataset.loaded === 'true') return Promise.resolve();
  if (existing?.dataset.loading === 'true') {
    return new Promise((resolve, reject) => {
      existing.addEventListener('load', resolve, { once: true });
      existing.addEventListener('error', reject, { once: true });
    });
  }
  return new Promise((resolve, reject) => {
    const script = document.createElement('script');
    script.src = src;
    script.async = true;
    script.crossOrigin = 'anonymous';
    script.dataset.loading = 'true';
    script.onload = () => {
      script.dataset.loading = 'false';
      script.dataset.loaded = 'true';
      resolve();
    };
    script.onerror = () => reject(new Error('Failed to load ' + src));
    document.head.appendChild(script);
  });
}

function escapeHtml(s) {
  return String(s)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
}

function escapeMarkdownLinkText(s) {
  return String(s).replace(/\\/g, '\\\\').replace(/]/g, '\\]');
}

// Note paths come from agent-written graph.json. Reject traversal, absolute
// paths, query/fragment smuggling, and non-markdown targets, and encode each
// segment so the fetch URL can't be reshaped by the path contents.
export function safeMemoryPath(path) {
  if (typeof path !== 'string') return null;
  const trimmed = path.trim();
  if (!trimmed || trimmed.startsWith('/') || trimmed.includes('\\')) return null;
  if (trimmed.includes('?') || trimmed.includes('#')) return null;
  const parts = trimmed.split('/');
  if (parts.some((part) => !part || part === '.' || part === '..')) return null;
  if (!parts[parts.length - 1].endsWith('.md')) return null;
  return parts.map((part) => encodeURIComponent(part)).join('/');
}

// DOMPurify policy for memory notes: on top of the html profile, forbid every
// network-bearing tag and attribute (the prod CSP blocks remote img/connect,
// but form-action is NOT covered by default-src and test instances run without
// the Caddy CSP entirely — so the app forbids these itself). href is the one
// URL attribute left allowed, because wikilink anchors need it;
// restrictNoteHtml then drops every href that isn't a #memory-node- fragment.
export const MEMORY_SANITIZE_OPTIONS = {
  USE_PROFILES: { html: true },
  FORBID_TAGS: ['img', 'picture', 'source', 'video', 'audio', 'iframe', 'object', 'embed', 'form', 'input', 'button'],
  FORBID_ATTR: ['src', 'srcset', 'xlink:href', 'formaction'],
};

// Markdown-level twin of the sanitize policy: plain links keep their label
// but lose the URL, images collapse to their alt text. Wikilink syntax
// ([[slug]] / [[slug|alias]]) never matches either pattern, so running this
// before renderWikiLinks leaves wikilinks intact.
export function neutralizeMemoryMarkdown(md) {
  return (md || '')
    .replace(/!\[([^\]]*)\]\(([^)]+)\)/g, (_m, alt) => ` ${alt || 'image'} `)
    .replace(/\[([^\]]+)\]\(([^)]+)\)/g, '$1');
}

// Post-sanitize pass over already-DOMPurify-clean HTML: strip every anchor
// href except the #memory-node- fragments renderWikiLinks generates. Removal
// only — it cannot introduce markup the sanitizer didn't already allow.
function restrictNoteHtml(html) {
  const tpl = document.createElement('template');
  tpl.innerHTML = html;
  for (const a of tpl.content.querySelectorAll('a[href]')) {
    if (!a.getAttribute('href').startsWith('#memory-node-')) a.removeAttribute('href');
  }
  return tpl.innerHTML;
}

function clamp(v, min, max) {
  return Math.max(min, Math.min(max, v));
}

export function labelScore(node = {}) {
  const importance = Number(node.importance) || 0;
  const access = Number(node.access_count) || 0;
  const mocBonus = node.type === 'moc' ? 1000 : 0;
  const linkedBonus = Array.isArray(node.mocs) && node.mocs.length > 0 ? 18 : 0;
  const localBonus = node.localDepth === 0 ? 800 : node.localDepth === 1 ? 120 : 0;
  return mocBonus + localBonus + importance * 12 + access * 4 + linkedBonus;
}

export function shouldShowScreenLabel(node = {}, scale = 1, labelRank = 0, opts = {}) {
  const isHover = node.id === opts.hoverId;
  const isSelected = node.id === opts.selectedId;
  const isHub = node.type === 'moc';
  const isLocalCenter = node.localDepth === 0;
  if (isHover || isSelected || isHub || isLocalCenter) return true;

  if (opts.mode === 'local') {
    if (node.localDepth === 1 && scale >= 0.72) return true;
    if (node.localDepth === 2 && scale >= 1.15) return true;
    return scale >= 1.7 && labelRank < 18;
  }

  if (scale < 0.9) return false;
  if (scale < 1.25) return labelRank < 6;
  if (scale < 1.7) return labelRank < 14;
  if (scale < 2.2) return labelRank < 26;
  return labelRank < 60;
}

// Read a CSS custom property off :root (computed) with a fallback.
// Re-read on each entry because the parent can swap the theme live
// (moebius:frame-theme); caching the computed style would freeze the old
// palette after a light/dark toggle.
function cssVar(name, fallback) {
  const v = getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  return v || fallback;
}

// Parse a CSS color string to [r,g,b]. Handles #rgb, #rrggbb, and rgb()/rgba().
function parseRGB(c) {
  if (!c) return null;
  const s = c.trim();
  let m = s.match(/^#?([0-9a-fA-F]{3})$/);
  if (m) {
    const h = m[1];
    return [parseInt(h[0] + h[0], 16), parseInt(h[1] + h[1], 16), parseInt(h[2] + h[2], 16)];
  }
  m = s.match(/^#?([0-9a-fA-F]{6})$/);
  if (m) {
    const i = parseInt(m[1], 16);
    return [(i >> 16) & 255, (i >> 8) & 255, i & 255];
  }
  m = s.match(/rgba?\(\s*([0-9.]+)[, ]+([0-9.]+)[, ]+([0-9.]+)/);
  if (m) return [+m[1], +m[2], +m[3]];
  return null;
}

// ------------------------------------------------------------------- styles ---

const S = {
  root: {
    height: '100%', overflow: 'hidden', background: 'var(--bg)', color: 'var(--text)',
    fontFamily: 'var(--font)', display: 'flex', flexDirection: 'column', position: 'relative',
  },
  header: {
    display: 'flex', alignItems: 'center', justifyContent: 'space-between',
    padding: '11px 14px', borderBottom: '1px solid var(--border)',
    background: 'var(--surface)', flexShrink: 0, gap: 12, position: 'relative', zIndex: 5,
  },
  brand: { display: 'flex', alignItems: 'center', gap: 11, minWidth: 0 },
  brandDot: {
    width: 34, height: 34, borderRadius: '50%', flexShrink: 0,
    display: 'flex', alignItems: 'center', justifyContent: 'center',
    background: 'radial-gradient(circle at 32% 28%, var(--accent-hover, #c4b5fd), var(--accent))',
    boxShadow: '0 0 0 1px var(--accent-dim, rgba(167,139,250,0.18)), 0 4px 14px var(--accent-dim, rgba(167,139,250,0.3))',
  },
  brandDotCore: {
    width: 7, height: 7, borderRadius: '50%', background: 'rgba(255,255,255,0.92)',
    boxShadow: '0 0 6px rgba(255,255,255,0.7)',
  },
  brandIcon: {
    width: 34, height: 34, borderRadius: 7, flexShrink: 0, display: 'block',
    objectFit: 'cover',
  },
  subtitle: {
    fontSize: 11.5, color: 'var(--muted)', marginTop: 2, whiteSpace: 'nowrap',
    overflow: 'hidden', textOverflow: 'ellipsis', fontVariantNumeric: 'tabular-nums',
  },
  headerRight: { display: 'flex', alignItems: 'center', gap: 8 },

  toggle: {
    display: 'flex', background: 'var(--surface2)', borderRadius: 9, padding: 3,
    border: '1px solid var(--border)', gap: 2,
  },
  toggleBtn: {
    display: 'flex', alignItems: 'center', border: 'none', background: 'transparent',
    color: 'var(--muted)', fontSize: 12.5, fontWeight: 600, padding: '5px 11px',
    borderRadius: 6, cursor: 'pointer', fontFamily: 'var(--font)', transition: 'color 0.15s, background 0.15s',
  },
  toggleActive: {
    background: 'var(--bg)', color: 'var(--text)',
    boxShadow: '0 1px 3px rgba(0,0,0,0.18)',
  },

  healthBadge: {
    display: 'flex', alignItems: 'center', gap: 6, border: '1px solid var(--border)',
    background: 'var(--surface2)', color: 'var(--text)', borderRadius: 9,
    fontSize: 12, fontWeight: 700, padding: '5px 10px', cursor: 'pointer',
    fontFamily: 'var(--font)', fontVariantNumeric: 'tabular-nums',
  },
  healthWarn: {},
  healthErr: { borderColor: 'var(--danger)', color: 'var(--danger)' },
  healthDot: { width: 7, height: 7, borderRadius: '50%', flexShrink: 0 },
  healthPanel: {
    padding: '10px 14px', background: 'var(--surface2)',
    borderBottom: '1px solid var(--border)', maxHeight: 176, overflowY: 'auto',
    flexShrink: 0, position: 'relative', zIndex: 4,
  },
  healthHead: {
    fontSize: 12, fontWeight: 600, color: 'var(--text)', marginBottom: 8,
  },
  healthRow: { display: 'flex', alignItems: 'baseline', gap: 8, padding: '3px 0', fontSize: 12 },
  sevTag: {
    fontSize: 9.5, fontWeight: 700, textTransform: 'uppercase', borderRadius: 4,
    padding: '1px 5px', flexShrink: 0, letterSpacing: '0.04em',
  },
  sevWarn: { background: 'rgba(240,198,116,0.16)', color: 'var(--accent-hover, #f0c674)' },
  sevErr: { background: 'rgba(248,113,113,0.18)', color: 'var(--danger)' },
  healthKind: { fontWeight: 600, color: 'var(--text)', flexShrink: 0 },
  healthDetail: { color: 'var(--muted)', wordBreak: 'break-word' },

  main: { flex: 1, position: 'relative', overflow: 'hidden', minHeight: 0 },

  center: {
    position: 'absolute', inset: 0, display: 'flex', flexDirection: 'column',
    alignItems: 'center', justifyContent: 'center', gap: 16, padding: 28, textAlign: 'center',
  },
  centerTitle: { fontSize: 17, fontWeight: 600, color: 'var(--text)', letterSpacing: '-0.01em' },
  centerText: { fontSize: 13.5, color: 'var(--muted)', maxWidth: 320, lineHeight: 1.55 },
  errIcon: {
    width: 42, height: 42, borderRadius: '50%', display: 'flex', alignItems: 'center',
    justifyContent: 'center', fontSize: 22, fontWeight: 800, color: 'var(--danger)',
    border: '2px solid var(--danger)',
  },

  graphWrap: { position: 'absolute', inset: 0 },
  pixiGraph: { position: 'absolute', inset: 0, overflow: 'hidden' },
  graphHint: {
    position: 'absolute', top: 12, left: '50%', transform: 'translateX(-50%)',
    fontSize: 11, color: 'var(--muted)', background: 'var(--surface)',
    border: '1px solid var(--border)', borderRadius: 999, padding: '4px 12px',
    pointerEvents: 'none', opacity: 0.92, whiteSpace: 'nowrap', maxWidth: '92%',
    overflow: 'hidden', textOverflow: 'ellipsis',
  },
  legend: {
    position: 'absolute', left: 12, bottom: 12, background: 'var(--surface)',
    border: '1px solid var(--border)', borderRadius: 12, padding: '11px 12px',
    maxWidth: 210, maxHeight: '58%', overflowY: 'auto',
    boxShadow: '0 8px 28px rgba(0,0,0,0.3)', backdropFilter: 'blur(4px)',
  },
  legendTitle: {
    fontSize: 10, fontWeight: 700, textTransform: 'uppercase', letterSpacing: '0.07em',
    color: 'var(--muted)', marginBottom: 8,
  },
  legendRow: {
    display: 'flex', alignItems: 'center', gap: 9, padding: '3px 4px', width: '100%',
    background: 'transparent', border: 'none', borderRadius: 6, cursor: 'pointer',
    textAlign: 'left', fontFamily: 'var(--font)', color: 'var(--text)',
  },
  legendSwatch: {
    width: 11, height: 11, borderRadius: '50%', flexShrink: 0,
    boxShadow: '0 0 0 3px var(--surface2)',
  },
  legendLabel: {
    fontSize: 12, color: 'var(--text)', whiteSpace: 'nowrap',
    overflow: 'hidden', textOverflow: 'ellipsis',
  },

  listWrap: { position: 'absolute', inset: 0, overflowY: 'auto', padding: '0 0 24px' },
  table: { width: '100%', borderCollapse: 'collapse', fontSize: 13 },
  th: {
    position: 'sticky', top: 0, zIndex: 1, background: 'var(--surface)',
    color: 'var(--muted)', fontWeight: 600, fontSize: 11, textTransform: 'uppercase',
    letterSpacing: '0.05em', padding: '11px 12px', borderBottom: '1px solid var(--border)',
    whiteSpace: 'nowrap', userSelect: 'none',
  },
  // The sortable-header button fills the cell and inherits its typography so it
  // looks identical to the old <th onClick>, while being natively focusable +
  // keyboard-activatable. Padding lives on the <th>, so the button carries none.
  thButton: {
    display: 'flex', alignItems: 'center', gap: 0, width: '100%', padding: 0, margin: 0,
    border: 0, background: 'transparent', cursor: 'pointer', font: 'inherit',
    color: 'inherit', letterSpacing: 'inherit', textTransform: 'inherit', textAlign: 'inherit',
  },
  thMain: { display: 'block', lineHeight: 1.05 },
  thSub: {
    display: 'block', marginTop: 3, fontSize: 9, fontWeight: 600, color: 'var(--muted)',
    textTransform: 'none', letterSpacing: 0, opacity: 0.8,
  },
  sortCaret: { marginLeft: 5, fontSize: 11, color: 'var(--accent)' },
  tr: { cursor: 'pointer', borderBottom: '1px solid var(--border-light, var(--border))' },
  td: { padding: '10px 12px', verticalAlign: 'middle', color: 'var(--text)' },
  tdNum: { textAlign: 'right' },
  tdTitle: {
    padding: '10px 12px', fontWeight: 600, display: 'flex', alignItems: 'center', gap: 9,
    maxWidth: 300, minWidth: 150,
  },
  rowTitleText: { overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' },
  tdMeta: {
    textAlign: 'right', color: 'var(--muted)', fontVariantNumeric: 'tabular-nums',
    whiteSpace: 'nowrap',
  },
  rowDot: {
    width: 9, height: 9, borderRadius: '50%', flexShrink: 0, display: 'inline-block',
    boxShadow: '0 0 0 3px var(--surface)',
  },
  typeTag: {
    fontSize: 10.5, fontWeight: 600, padding: '2px 8px', borderRadius: 999,
    background: 'var(--surface2)', color: 'var(--muted)', letterSpacing: '0.01em',
    border: '1px solid var(--border)',
  },
  typeMoc: {
    background: 'var(--accent-dim, rgba(167,139,250,0.15))', color: 'var(--accent)',
    borderColor: 'transparent',
  },

  dotsWrap: { display: 'inline-flex', gap: 3, alignItems: 'center', verticalAlign: 'middle' },
  pip: {
    width: 5, height: 5, borderRadius: '50%', background: 'var(--border)',
    display: 'inline-block',
  },
  pipOn: { background: 'var(--accent)' },

  scrim: { position: 'absolute', inset: 0, background: 'rgba(0,0,0,0.5)', zIndex: 20 },
  panel: {
    position: 'absolute', zIndex: 21, background: 'var(--surface)',
    borderLeft: '1px solid var(--border)', display: 'flex', flexDirection: 'column',
    boxShadow: '-12px 0 40px rgba(0,0,0,0.35)', overflow: 'hidden',
  },
  panelAccent: { position: 'absolute', top: 0, left: 0, right: 0, height: 3, zIndex: 1 },
  panelHead: {
    display: 'flex', alignItems: 'flex-start', justifyContent: 'space-between',
    padding: '15px 16px 10px', gap: 10,
  },
  panelHeadMain: { display: 'flex', gap: 11, minWidth: 0, alignItems: 'flex-start' },
  panelTitle: { fontSize: 18, fontWeight: 700, lineHeight: 1.18, letterSpacing: '-0.01em' },
  panelMetaLine: {
    display: 'flex', alignItems: 'center', gap: 8, flexWrap: 'wrap',
    marginTop: 5, color: 'var(--muted)', fontSize: 11.5,
    fontVariantNumeric: 'tabular-nums',
  },
  closeBtn: {
    border: 'none', background: 'var(--surface2)', color: 'var(--muted)',
    width: 30, height: 30, borderRadius: 8, fontSize: 20, lineHeight: 1, cursor: 'pointer',
    flexShrink: 0, fontFamily: 'var(--font)', display: 'flex', alignItems: 'center',
    justifyContent: 'center', transition: 'background 0.15s, color 0.15s',
  },

  tagRow: { display: 'flex', flexWrap: 'wrap', gap: 6, padding: '0 16px 8px' },
  tag: {
    fontSize: 11.5, color: 'var(--accent)', background: 'var(--accent-dim, rgba(167,139,250,0.12))',
    borderRadius: 999, padding: '2px 9px', fontWeight: 500,
  },
  // Thin tab strip: context label (left) + depth control (graph tab only) +
  // the icon toggle (right). Minimal chrome — one row, no full-width tab bar.
  detailBar: {
    display: 'flex', alignItems: 'center', gap: 10, padding: '9px 12px 8px',
    borderTop: '1px solid var(--border)', borderBottom: '1px solid var(--border)',
    flexShrink: 0,
  },
  detailContext: {
    display: 'flex', alignItems: 'baseline', gap: 8, flex: 1, minWidth: 0,
    overflow: 'hidden',
  },
  paneHead: {
    fontSize: 10, fontWeight: 700, textTransform: 'uppercase', letterSpacing: 0,
    color: 'var(--muted)', whiteSpace: 'nowrap',
  },
  localCount: {
    fontSize: 11, color: 'var(--muted)', fontVariantNumeric: 'tabular-nums',
    whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis',
  },
  depthToggle: {
    display: 'flex', alignItems: 'center', gap: 3, padding: 3, borderRadius: 8,
    background: 'var(--surface2)', border: '1px solid var(--border)', flexShrink: 0,
  },
  depthBtn: {
    minWidth: 28, height: 26, border: 'none', borderRadius: 6, background: 'transparent',
    color: 'var(--muted)', fontSize: 12, fontWeight: 700, fontFamily: 'var(--font)',
    cursor: 'pointer',
  },
  depthBtnActive: {
    background: 'var(--bg)', color: 'var(--text)', boxShadow: '0 1px 3px rgba(0,0,0,0.16)',
  },
  tabToggle: {
    display: 'flex', gap: 2, padding: 3, borderRadius: 8,
    background: 'var(--surface2)', border: '1px solid var(--border)', flexShrink: 0,
  },
  tabBtn: {
    display: 'flex', alignItems: 'center', justifyContent: 'center',
    width: 32, height: 26, border: 'none', borderRadius: 6, background: 'transparent',
    color: 'var(--muted)', cursor: 'pointer', fontFamily: 'var(--font)',
    transition: 'color 0.15s, background 0.15s',
  },
  tabBtnActive: {
    background: 'var(--bg)', color: 'var(--text)', boxShadow: '0 1px 3px rgba(0,0,0,0.16)',
  },
  // The active tab's pane fills all remaining panel height. The local graph
  // mounts absolutely-positioned inside it so Pixi always gets the full box.
  detailBody: { flex: 1, minHeight: 0, position: 'relative', display: 'flex', flexDirection: 'column' },
  localGraphWrap: { position: 'absolute', inset: 0, overflow: 'hidden' },
  localEmpty: {
    position: 'absolute', inset: 0, display: 'flex', alignItems: 'center', justifyContent: 'center',
    color: 'var(--muted)', fontSize: 13, padding: 18, textAlign: 'center',
  },
  panelBody: {
    flex: 1, overflowY: 'auto', padding: '10px 16px 20px', fontSize: 14, lineHeight: 1.62,
    minHeight: 0,
  },
  notePlaceholder: { display: 'flex', flexDirection: 'column', gap: 11, paddingTop: 4 },
  mergePill: {
    display: 'inline-flex', alignItems: 'center', gap: 6, alignSelf: 'flex-start',
    margin: '0 0 10px', padding: '3px 10px', borderRadius: 999,
    fontSize: 11.5, fontWeight: 600, letterSpacing: 0.2,
    color: 'var(--muted)', background: 'var(--surface2)',
    border: '1px solid var(--border)',
  },
  mergeDot: {
    width: 6, height: 6, borderRadius: '50%', background: 'var(--accent)',
    animation: 'mg-pulse 1s ease-in-out infinite',
  },
  pre: { whiteSpace: 'pre-wrap', wordBreak: 'break-word', fontFamily: 'var(--mono)', fontSize: 12.5 },
  panelFoot: { padding: 12, borderTop: '1px solid var(--border)', background: 'var(--surface)' },
  discussBtn: {
    width: '100%', border: 'none', borderRadius: 10, padding: '10px 14px',
    background: 'var(--accent)', color: '#fff', fontSize: 14, fontWeight: 600,
    cursor: 'pointer', fontFamily: 'var(--font)', display: 'flex', alignItems: 'center',
    justifyContent: 'center', transition: 'filter 0.15s, transform 0.05s',
    boxShadow: '0 6px 18px var(--accent-dim, rgba(167,139,250,0.35))',
  },
};

const CSS = `
@keyframes mg-orbit-spin { to { transform: rotate(360deg); } }
.mg-orbit {
  position: relative; width: 46px; height: 46px;
  animation: mg-orbit-spin 2.4s linear infinite;
}
.mg-orbit span {
  position: absolute; width: 8px; height: 8px; border-radius: 50%;
  background: var(--accent); top: 50%; left: 50%; margin: -4px;
}
.mg-orbit span:nth-child(1) { transform: rotate(0deg) translateX(18px); opacity: 1; }
.mg-orbit span:nth-child(2) { transform: rotate(120deg) translateX(18px); opacity: 0.6; }
.mg-orbit span:nth-child(3) { transform: rotate(240deg) translateX(18px); opacity: 0.3; }

@keyframes mg-twinkle { 0%,100% { opacity: 0.35; } 50% { opacity: 1; } }
.mg-star { animation: mg-twinkle 2.8s ease-in-out infinite; }
.mg-star-hub { filter: drop-shadow(0 0 6px var(--accent)); }
@keyframes mg-pulse-ring {
  0% { transform: scale(0.8); opacity: 0.5; }
  70% { transform: scale(1.5); opacity: 0; }
  100% { opacity: 0; }
}
.mg-pulse { transform-origin: 66px 48px; animation: mg-pulse-ring 2.6s ease-out infinite; }

.mg-graph { cursor: grab; }
.mg-graph:active { cursor: grabbing; }

.mg-row:hover { background: var(--surface2); }
.mg-th:hover { color: var(--text); }
/* Keyboard-focus ring for the now-focusable list rows + sort-header buttons,
   so the keyboard affordance these gained is actually visible. */
.mg-row:focus-visible { outline: 2px solid var(--accent); outline-offset: -2px; }
.mg-th:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; border-radius: 4px; }
.mg-legend-row:hover { background: var(--surface2); }
.mg-tgl:hover { color: var(--text); }
.mg-tab:hover { color: var(--text); }
.mg-close:hover { background: var(--border); color: var(--text); }
.mg-discuss:hover { filter: brightness(1.06); }
.mg-discuss:active { transform: translateY(1px); }

.mg-scroll::-webkit-scrollbar { width: 9px; height: 9px; }
.mg-scroll::-webkit-scrollbar-thumb {
  background: var(--border); border-radius: 999px;
  border: 2px solid var(--surface);
}
.mg-scroll::-webkit-scrollbar-thumb:hover { background: var(--muted); }
.mg-scroll::-webkit-scrollbar-track { background: transparent; }

@keyframes mg-skel-pulse { 0%,100% { opacity: 0.5; } 50% { opacity: 1; } }
@keyframes mg-pulse { 0%,100% { opacity: 0.4; } 50% { opacity: 1; } }
.mg-skel {
  height: 13px; border-radius: 5px;
  background: linear-gradient(90deg, var(--surface2), var(--border), var(--surface2));
  animation: mg-skel-pulse 1.4s ease-in-out infinite;
}

@keyframes mg-panel-in {
  from { transform: translateX(20px); opacity: 0; }
  to { transform: translateX(0); opacity: 1; }
}
@keyframes mg-scrim-in { from { opacity: 0; } to { opacity: 1; } }
.mg-panel { inset: 0 0 0 auto; width: min(980px, 96vw); animation: mg-panel-in 0.22s cubic-bezier(0.22,1,0.36,1); }
.mg-scrim { animation: mg-scrim-in 0.2s ease; }
.mg-local-graph { cursor: grab; background: var(--bg); }
.mg-local-graph:active { cursor: grabbing; }
.mg-md a[href^="#memory-node-"] {
  border: 1px solid var(--accent-dim, rgba(167,139,250,0.35));
  background: var(--accent-dim, rgba(167,139,250,0.12));
  border-radius: 6px;
  padding: 0 5px;
  font-weight: 600;
}
@media (max-width: 640px) {
  .mg-scrim { display: none; }
  .mg-panel {
    inset: 0; width: 100%; height: 100%; border-left: none;
    border-top: none; border-radius: 0; box-shadow: none;
    animation: mg-panel-in 0.18s cubic-bezier(0.22,1,0.36,1);
  }
  .mg-panel-head { padding: 11px 12px 8px !important; }
  .mg-panel .mg-close {
    width: 34px !important; height: 34px !important; border-radius: 10px !important;
  }
  .mg-panel .mg-tag-row {
    flex-wrap: nowrap !important; overflow-x: auto; padding: 0 12px 7px !important;
    scrollbar-width: none;
  }
  .mg-panel .mg-tag-row::-webkit-scrollbar { display: none; }
  .mg-md {
    padding: 10px 14px 18px !important;
    font-size: 13px !important;
    line-height: 1.54 !important;
  }
  .mg-md h1 { font-size: 17px !important; }
  .mg-md h2 { font-size: 15px !important; }
  .mg-md h3 { font-size: 13px !important; }
  .mg-md p { margin: 8px 0 !important; }
  .mg-md ul, .mg-md ol { margin: 8px 0 !important; }
  .mg-md code { font-size: 0.82em !important; }
  .mg-panel .mg-discuss { padding: 9px 12px !important; }
}
@media (prefers-reduced-motion: reduce) {
  .mg-orbit, .mg-star, .mg-pulse, .mg-skel, .mg-panel, .mg-scrim, .mg-star-hub { animation: none !important; }
}

.mg-md h1, .mg-md h2, .mg-md h3 { margin: 16px 0 7px; line-height: 1.25; font-weight: 700; letter-spacing: -0.01em; }
.mg-md h1 { font-size: 19px; } .mg-md h2 { font-size: 16px; } .mg-md h3 { font-size: 14px; }
.mg-md h1:first-child, .mg-md h2:first-child, .mg-md h3:first-child { margin-top: 0; }
.mg-md p { margin: 9px 0; }
.mg-md ul, .mg-md ol { margin: 9px 0; padding-left: 22px; }
.mg-md li { margin: 4px 0; }
.mg-md li::marker { color: var(--muted); }
.mg-md a { color: var(--accent); text-decoration: none; border-bottom: 1px solid var(--accent-dim, rgba(167,139,250,0.4)); }
.mg-md a:hover { border-bottom-color: var(--accent); }
.mg-md strong { color: var(--text); font-weight: 700; }
.mg-md code { background: var(--surface2); border-radius: 5px; padding: 1px 5px; font-family: var(--mono); font-size: 0.85em; border: 1px solid var(--border-light, var(--border)); }
.mg-md pre { background: var(--surface2); border: 1px solid var(--border); border-radius: 9px; padding: 13px; overflow-x: auto; margin: 11px 0; }
.mg-md pre code { background: none; padding: 0; border: none; }
.mg-md blockquote { border-left: 3px solid var(--accent); margin: 11px 0; padding: 3px 0 3px 13px; color: var(--muted); }
.mg-md table { border-collapse: collapse; margin: 11px 0; font-size: 13px; width: 100%; }
.mg-md th, .mg-md td { border: 1px solid var(--border); padding: 6px 10px; text-align: left; }
.mg-md th { background: var(--surface2); font-weight: 600; }
.mg-md img { max-width: 100%; border-radius: 8px; }
.mg-md hr { border: none; border-top: 1px solid var(--border); margin: 16px 0; }
`;
