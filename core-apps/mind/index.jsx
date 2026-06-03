/* Mind — an Obsidian-style force-directed view of the agent's knowledge.
 *
 * Data contract (unchanged, load-bearing):
 *   GET /api/storage/shared/memory/graph.json  → { nodes, edges, problems }
 *     node:    { id, title, type:'note'|'moc', path, size_bytes,
 *                importance:int, access_count:int, mocs:[], tags:[] }
 *     edge:    { source, target, kind:'moc'|'link' }
 *     problem: { severity:'warn'|'error', kind, detail }
 *   GET /api/storage/shared/memory/<node.path>  → raw markdown (frontmatter + body)
 *
 * Everything else here is presentation. The graph library
 * (react-force-graph-2d) and the markdown renderer (marked + DOMPurify) load
 * from esm.sh with react externalized so the single bundled React instance is
 * shared. All canvas drawing is done in CSS px (divided by globalScale) so it
 * stays crisp on HiDPI; react-force-graph handles the dpr backing store.
 */
import { useState, useEffect, useRef, useMemo, useCallback } from 'react';

const GRAPH_URL = '/api/storage/shared/memory/graph.json';
const NOTE_BASE = '/api/storage/shared/memory/';

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

// A normalized 0..1 position of v within [min,max], guarded for degenerate ranges.
function norm(v, min, max) {
  if (max <= min) return v > min ? 1 : 0;
  return Math.max(0, Math.min(1, (v - min) / (max - min)));
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

export default function App({ appId, token }) {
  const [graph, setGraph] = useState(null);
  const [status, setStatus] = useState('loading'); // loading | ready | empty | error
  const [errMsg, setErrMsg] = useState('');
  const [view, setView] = useState('graph'); // graph | list
  const [selected, setSelected] = useState(null); // node object
  const [noteState, setNoteState] = useState({ status: 'idle', md: '', fm: {} });
  const [hoverId, setHoverId] = useState(null);
  const [sortKey, setSortKey] = useState('access_count');
  const [sortDir, setSortDir] = useState('desc');
  const [showHealth, setShowHealth] = useState(false);
  const [FG, setFG] = useState(null); // ForceGraph2D component
  const [marked, setMarked] = useState(null);
  const [purify, setPurify] = useState(null); // DOMPurify — audited HTML sanitizer

  const wrapRef = useRef(null);
  const fgRef = useRef(null);
  const [dims, setDims] = useState({ w: 0, h: 0 });

  const authHeaders = useMemo(
    () => ({ Authorization: 'Bearer ' + token }),
    [token],
  );

  // --- Load the force-graph component (dynamic import, react externalized). ---
  useEffect(() => {
    let alive = true;
    (async () => {
      try {
        const mod = await import(
          'https://esm.sh/react-force-graph-2d@1.27.1?external=react,react-dom'
        );
        if (alive) setFG(() => mod.default);
      } catch (e) {
        // Graph view degrades to the list view if the lib can't load.
        if (alive) setFG(null);
      }
    })();
    return () => { alive = false; };
  }, []);

  // --- Load the graph index. ---
  useEffect(() => {
    let alive = true;
    (async () => {
      setStatus('loading');
      try {
        const res = await fetch(GRAPH_URL, { headers: authHeaders });
        if (res.status === 404) {
          if (alive) { setGraph({ nodes: [], edges: [], problems: [] }); setStatus('empty'); }
          return;
        }
        if (!res.ok) throw new Error('HTTP ' + res.status);
        const data = await res.json();
        const nodes = Array.isArray(data.nodes) ? data.nodes : [];
        if (!alive) return;
        setGraph({
          nodes,
          edges: Array.isArray(data.edges) ? data.edges : [],
          problems: Array.isArray(data.problems) ? data.problems : [],
        });
        setStatus(nodes.length === 0 ? 'empty' : 'ready');
      } catch (e) {
        if (alive) { setErrMsg(String(e.message || e)); setStatus('error'); }
      }
    })();
    return () => { alive = false; };
  }, [authHeaders]);

  // --- Measure the canvas container in CSS pixels (force-graph handles dpr). ---
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
  const radiusForNode = useCallback((n) => {
    const base = Math.max(n.importance || 1, 1 + Math.log2(1 + (n.access_count || 0)));
    const r = 3 + base * 1.55;
    return n.type === 'moc' ? r * 1.4 : r;
  }, []);

  // --- Neighbor sets for hover dimming. ---
  const neighbors = useMemo(() => {
    const map = new Map();
    if (!graph) return map;
    const add = (a, b) => {
      if (!map.has(a)) map.set(a, new Set());
      map.get(a).add(b);
    };
    for (const e of graph.edges) {
      const s = typeof e.source === 'object' ? e.source.id : e.source;
      const t = typeof e.target === 'object' ? e.target.id : e.target;
      add(s, t); add(t, s);
    }
    return map;
  }, [graph]);

  // --- react-force-graph mutates node objects (x/y/vx/vy) in place, so it
  //     needs its own object references. Build once per graph. ---
  const fgData = useMemo(() => {
    if (!graph) return { nodes: [], links: [] };
    return {
      nodes: graph.nodes.map((n) => ({ ...n })),
      links: graph.edges.map((e) => ({
        source: typeof e.source === 'object' ? e.source.id : e.source,
        target: typeof e.target === 'object' ? e.target.id : e.target,
        kind: e.kind,
      })),
    };
  }, [graph]);

  // --- Smooth hover focus: a 0..1 value per node that eases toward 1 for the
  //     hovered node + its neighbors and toward 0 for everything else, so the
  //     dimming fades instead of snapping (the Obsidian feel). Animated via
  //     rAF; we keep the values on a ref the painter reads each frame and ask
  //     the canvas to repaint while anything is still in motion. ---
  const focusRef = useRef(new Map()); // nodeId -> current 0..1
  const rafRef = useRef(0);
  useEffect(() => {
    const targets = (id) => {
      if (!hoverId) return null; // null => everything fully lit
      if (id === hoverId) return 1;
      return neighbors.get(hoverId)?.has(id) ? 1 : 0;
    };
    let last = performance.now();
    const tick = (now) => {
      const dt = Math.min(48, now - last); last = now;
      const k = 1 - Math.pow(0.0015, dt / 1000); // ~time-constant ease
      let moving = false;
      const cur = focusRef.current;
      if (graph) for (const n of graph.nodes) {
        const want = targets(n.id);
        const goal = want == null ? 1 : want;
        const v0 = cur.has(n.id) ? cur.get(n.id) : 1;
        const v1 = v0 + (goal - v0) * k;
        cur.set(n.id, v1);
        if (Math.abs(goal - v1) > 0.004) moving = true;
      }
      const fg = fgRef.current;
      if (fg && fg.refresh) fg.refresh(); // force a canvas redraw this frame
      if (moving) rafRef.current = requestAnimationFrame(tick);
      else rafRef.current = 0;
    };
    cancelAnimationFrame(rafRef.current);
    rafRef.current = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(rafRef.current);
  }, [hoverId, neighbors, graph]);

  const focusOf = useCallback((id) => {
    const v = focusRef.current.get(id);
    return v == null ? 1 : v;
  }, []);

  // --- Load a note body when a node is selected. ---
  useEffect(() => {
    if (!selected) return;
    let alive = true;
    const path = selected.path || ('notes/' + selected.id + '.md');
    setNoteState({ status: 'loading', md: '', fm: {} });
    (async () => {
      try {
        const res = await fetch(NOTE_BASE + path, { headers: authHeaders });
        if (res.status === 404) {
          if (alive) setNoteState({ status: 'missing', md: '', fm: {} });
          return;
        }
        if (!res.ok) throw new Error('HTTP ' + res.status);
        const raw = await res.text();
        if (!alive) return;
        setNoteState({ status: 'ready', md: stripFrontmatter(raw), fm: parseFrontmatter(raw) });
      } catch (e) {
        if (alive) setNoteState({ status: 'error', md: String(e.message || e), fm: {} });
      }
    })();
    return () => { alive = false; };
  }, [selected, authHeaders]);

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
    // Require BOTH the renderer AND the sanitizer before producing HTML — never
    // render un-sanitized markup. Notes can contain dreaming-agent web-research
    // content, so DOMPurify (a real HTML-parser sanitizer) is the right tool;
    // a regex net is routinely bypassed.
    if (marked && purify) {
      try {
        const raw = marked(noteState.md, { breaks: true, gfm: true });
        return purify.sanitize(raw, { USE_PROFILES: { html: true } });
      } catch { return escapeHtml(noteState.md); }
    }
    return null; // renderer not ready yet -> fall back to plain text below
  }, [noteState, marked, purify]);

  const closePanel = useCallback(() => setSelected(null), []);

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
    const onKey = (e) => { if (e.key === 'Escape') setSelected(null); };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [selected]);

  // --- Canvas link painter: a soft curve, brighter when it touches the
  //     hovered focus. Drawn before nodes (react-force-graph paints links
  //     under nodes by default; we override per-link color for the focus glow). ---
  const linkColor = useCallback((link) => {
    const s = typeof link.source === 'object' ? link.source.id : link.source;
    const t = typeof link.target === 'object' ? link.target.id : link.target;
    // A link is "in focus" when both endpoints are lit. Use the min of the two
    // focus values so a link to a dimmed node also dims.
    const f = Math.min(focusOf(s), focusOf(t));
    const isMoc = link.kind === 'moc';
    const baseA = isMoc ? 0.5 : 0.32;
    const dimA = 0.05;
    const a = dimA + (baseA - dimA) * f;
    return isMoc
      ? cssVarA('--accent', '#a78bfa', a)
      : cssVarA('--text', '#e5e5e5', a * 0.9);
  }, [focusOf]);

  const linkWidth = useCallback((link) => {
    const s = typeof link.source === 'object' ? link.source.id : link.source;
    const t = typeof link.target === 'object' ? link.target.id : link.target;
    const f = Math.min(focusOf(s), focusOf(t));
    const base = link.kind === 'moc' ? 1.3 : 0.7;
    return base + f * (link.kind === 'moc' ? 1.1 : 0.6);
  }, [focusOf]);

  // --- Canvas node painter: glow halo + disc + crisp label, all in CSS px. ---
  const paintNode = useCallback((node, ctx, globalScale) => {
    const r = radiusForNode(node);
    const f = focusOf(node.id);        // 0..1, eased
    const isHover = hoverId === node.id;
    const isMoc = node.type === 'moc';
    const col = colorForNode(node);
    const alpha = 0.16 + 0.84 * f;     // dimmed nodes never vanish entirely

    // Outer glow — the Obsidian halo. Scales with focus so the hovered node
    // and its neighborhood bloom. Drawn as a radial gradient in CSS px.
    const glowR = r * (isHover ? 4.2 : 2.6);
    const glowStrength = (isMoc ? 0.5 : 0.34) * (0.25 + 0.75 * f);
    const grad = ctx.createRadialGradient(node.x, node.y, r * 0.6, node.x, node.y, glowR);
    grad.addColorStop(0, withAlpha(col, glowStrength));
    grad.addColorStop(1, withAlpha(col, 0));
    ctx.beginPath();
    ctx.arc(node.x, node.y, glowR, 0, 2 * Math.PI, false);
    ctx.fillStyle = grad;
    ctx.fill();

    // Core disc.
    ctx.globalAlpha = alpha;
    ctx.beginPath();
    ctx.arc(node.x, node.y, r, 0, 2 * Math.PI, false);
    ctx.fillStyle = col;
    ctx.fill();

    // A subtle inner sheen so discs read as spheres, not flat dots.
    const sheen = ctx.createRadialGradient(
      node.x - r * 0.35, node.y - r * 0.4, r * 0.1,
      node.x, node.y, r,
    );
    sheen.addColorStop(0, 'rgba(255,255,255,0.32)');
    sheen.addColorStop(0.6, 'rgba(255,255,255,0)');
    ctx.beginPath();
    ctx.arc(node.x, node.y, r, 0, 2 * Math.PI, false);
    ctx.fillStyle = sheen;
    ctx.fill();

    // Ring — every node gets a faint hairline so it sits cleanly on the bg;
    // MOC nodes + the hovered node get a bright accent ring.
    ctx.lineWidth = (isHover || isMoc ? 1.6 : 1) / globalScale;
    ctx.strokeStyle = isHover
      ? cssVar('--accent', '#a78bfa')
      : isMoc
        ? withAlpha(cssVar('--text', '#e5e5e5'), 0.55 * alpha)
        : withAlpha(col, 0.5 * alpha);
    ctx.stroke();

    // Label — zoom-based LOD, drawn in CSS px (font size / zoom) with a
    // rounded pill underlay so text stays legible over links and halos.
    const hasMocList = Array.isArray(node.mocs) && node.mocs.length > 0;
    const isImportant = (node.importance || 0) >= 7;
    const showLabel = globalScale >= 1.4
      || (globalScale >= 0.8 && (hasMocList || isImportant || isHover))
      || (globalScale >= 0.4 && (hasMocList || isHover));
    if (showLabel) {
      const label = node.title || node.id;
      const labelPx = clamp((isMoc ? 11.5 : 10.5) * Math.sqrt(globalScale), 9, 15);
      const fontSize = labelPx / globalScale;
      const padX = 5.5 / globalScale;
      const padY = 2.5 / globalScale;
      const gap = 3 / globalScale;
      const labelY = node.y + r + gap;
      const pillAlpha = (isHover ? 0.88 : 0.62) * (0.35 + 0.65 * f);

      ctx.save();
      ctx.font = `${isMoc ? 600 : 500} ${fontSize}px var(--font, sans-serif)`;
      ctx.textAlign = 'center';
      ctx.textBaseline = 'middle';
      ctx.globalAlpha = isHover ? 1 : 0.4 + 0.6 * f;

      const width = ctx.measureText(label).width + padX * 2;
      const height = fontSize + padY * 2;
      const x = node.x - width / 2;
      const y = labelY;
      ctx.fillStyle = withAlpha(cssVar('--bg', '#0d0d0d'), pillAlpha);
      roundedRect(ctx, x, y, width, height, 5 / globalScale);
      ctx.fill();
      ctx.lineWidth = 0.75 / globalScale;
      ctx.strokeStyle = withAlpha(cssVar('--text', '#e5e5e5'), 0.12 * (isHover ? 1 : f));
      ctx.stroke();

      ctx.fillStyle = cssVar('--text', '#e5e5e5');
      ctx.fillText(label, node.x, y + height / 2);
      ctx.restore();
    }
    ctx.globalAlpha = 1;
  }, [colorForNode, radiusForNode, hoverId, focusOf]);

  // Larger pointer hit-area than the dot so small nodes are clickable.
  const paintPointer = useCallback((node, color, ctx) => {
    const r = radiusForNode(node);
    ctx.fillStyle = color;
    ctx.beginPath();
    ctx.arc(node.x, node.y, r + 4, 0, 2 * Math.PI, false);
    ctx.fill();
  }, [radiusForNode]);

  // --- List view: sorted rows with normalized usage/size bars. ---
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

  const ranges = useMemo(() => {
    const r = { size_bytes: [Infinity, -Infinity], access_count: [Infinity, -Infinity], importance: [1, 5] };
    if (graph) for (const n of graph.nodes) {
      for (const k of ['size_bytes', 'access_count']) {
        const v = n[k] || 0;
        if (v < r[k][0]) r[k][0] = v;
        if (v > r[k][1]) r[k][1] = v;
      }
    }
    for (const k of ['size_bytes', 'access_count']) {
      if (r[k][0] === Infinity) r[k] = [0, 0];
    }
    return r;
  }, [graph]);

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

  // ---------------------------------------------------------------- render ---
  return (
    <div style={S.root}>
      <style>{CSS}</style>

      <header style={S.header}>
        <div style={S.brand}>
          <span style={S.brandDot}><span style={S.brandDotCore} /></span>
          <div style={{ minWidth: 0 }}>
            <div style={S.title}>Mind</div>
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
              <span style={S.healthKind}>{p.kind.replace(/_/g, ' ')}</span>
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
            <div style={S.centerTitle}>Mind is just getting to know you</div>
            <div style={S.centerText}>
              It fills in as you use Möbius — every chat, app, and habit leaves a
              trace here. Come back once you've given it something to remember.
            </div>
          </div>
        )}

        {status === 'ready' && view === 'graph' && (
          <div ref={wrapRef} style={S.graphWrap} className="mg-graph">
            {FG && dims.w > 0 ? (
              <FG
                ref={fgRef}
                graphData={fgData}
                width={dims.w}
                height={dims.h}
                backgroundColor="rgba(0,0,0,0)"
                nodeRelSize={1}
                nodeVal={(n) => radiusForNode(n)}
                nodeLabel={() => ''}
                nodeCanvasObject={paintNode}
                nodeCanvasObjectMode="replace"
                nodePointerAreaPaint={paintPointer}
                linkColor={linkColor}
                linkWidth={linkWidth}
                linkCurvature={0.08}
                linkDirectionalParticles={(link) => (link.kind === 'moc' ? 1 : 0)}
                linkDirectionalParticleSpeed={0.0025}
                linkDirectionalParticleWidth={(link) => (link.kind === 'moc' ? 1.35 : 0.9)}
                linkDirectionalParticleColor={(link) => linkColor(link)}
                autoPauseRedraw={false}
                cooldownTicks={260}
                cooldownTime={30000}
                onNodeClick={(n) => { setSelected(n); setHoverId(null); }}
                onNodeHover={(n) => setHoverId(n ? n.id : null)}
                onBackgroundClick={() => setSelected(null)}
                d3VelocityDecay={0.24}
                warmupTicks={28}
              />
            ) : (
              <div style={S.center}>
                <div className="mg-orbit"><span /><span /><span /></div>
                <div style={S.centerText}>
                  {FG === null ? 'Graph view is offline — try List.' : 'Laying out the graph…'}
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
                      if (n) setSelected(n);
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
                  <Th label="Note" active={sortKey === 'title'} dir={sortDir} onClick={() => toggleSort('title')} align="left" />
                  <Th label="Type" />
                  <Th label="Weight" active={sortKey === 'importance'} dir={sortDir} onClick={() => toggleSort('importance')} />
                  <Th label="Used" active={sortKey === 'access_count'} dir={sortDir} onClick={() => toggleSort('access_count')} />
                  <Th label="Size" active={sortKey === 'size_bytes'} dir={sortDir} onClick={() => toggleSort('size_bytes')} />
                </tr>
              </thead>
              <tbody>
                {sortedNodes.map((n) => {
                  const sN = norm(n.size_bytes || 0, ranges.size_bytes[0], ranges.size_bytes[1]);
                  const uN = norm(n.access_count || 0, ranges.access_count[0], ranges.access_count[1]);
                  return (
                    <tr
                      key={n.id}
                      style={S.tr}
                      onClick={() => { setSelected(n); }}
                      className="mg-row"
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
                      <td style={S.tdBar}>
                        <Bar value={uN} label={String(n.access_count || 0)} hue="var(--green, #6ee7b7)" />
                      </td>
                      <td style={S.tdBar}>
                        <Bar value={sN} label={fmtBytes(n.size_bytes)} hue={colorForNode(n)} />
                      </td>
                    </tr>
                  );
                })}
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
            <div style={S.panelHead}>
              <div style={S.panelHeadMain}>
                <span style={{ ...S.rowDot, background: colorForNode(selected), width: 12, height: 12, marginTop: 5 }} />
                <div style={{ minWidth: 0 }}>
                  <div style={S.panelKicker}>
                    {selected.type === 'moc' ? 'Map of content' : 'Note'}
                  </div>
                  <div style={S.panelTitle}>{selected.title || selected.id}</div>
                </div>
              </div>
              <button style={S.closeBtn} className="mg-close" onClick={closePanel} aria-label="Close">×</button>
            </div>

            {/* Frontmatter chips — importance / usage / size / recency. */}
            <div style={S.chipRow}>
              <Chip label="weight"><ImportanceDots value={selected.importance || 1} /></Chip>
              <Chip label="used">{(selected.access_count || 0)}×</Chip>
              <Chip label="size">{fmtBytes(selected.size_bytes)}</Chip>
              {relDate(noteState.fm.updated) && (
                <Chip label="updated">{relDate(noteState.fm.updated)}</Chip>
              )}
            </div>

            {Array.isArray(selected.tags) && selected.tags.length > 0 && (
              <div style={S.tagRow}>
                {selected.tags.map((t) => <span key={t} style={S.tag}>#{t}</span>)}
              </div>
            )}

            <div style={S.panelBody} className="mg-md mg-scroll">
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
              {noteState.status === 'ready' && (
                noteHtml != null
                  ? <div dangerouslySetInnerHTML={{ __html: noteHtml }} />
                  : <pre style={S.pre}>{noteState.md}</pre>
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

function Th({ label, active, dir, onClick, align }) {
  return (
    <th
      style={{ ...S.th, textAlign: align || 'right', cursor: onClick ? 'pointer' : 'default' }}
      onClick={onClick}
      className={onClick ? 'mg-th' : undefined}
    >
      {label}
      {active && <span style={S.sortCaret}>{dir === 'asc' ? '↑' : '↓'}</span>}
    </th>
  );
}

function Bar({ value, label, hue }) {
  return (
    <div style={S.barCell}>
      <div style={S.barTrack}>
        <div style={{ ...S.barFill, width: Math.round(value * 100) + '%', background: hue }} />
      </div>
      <span style={S.barLabel}>{label}</span>
    </div>
  );
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

function Chip({ label, children }) {
  return (
    <div style={S.chip}>
      <span style={S.chipLabel}>{label}</span>
      <span style={S.chipValue}>{children}</span>
    </div>
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

// ----------------------------------------------------------------- helpers ---

function escapeHtml(s) {
  return String(s)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
}

function clamp(v, min, max) {
  return Math.max(min, Math.min(max, v));
}

function roundedRect(ctx, x, y, w, h, r) {
  const rr = Math.min(r, w / 2, h / 2);
  ctx.beginPath();
  ctx.moveTo(x + rr, y);
  ctx.lineTo(x + w - rr, y);
  ctx.quadraticCurveTo(x + w, y, x + w, y + rr);
  ctx.lineTo(x + w, y + h - rr);
  ctx.quadraticCurveTo(x + w, y + h, x + w - rr, y + h);
  ctx.lineTo(x + rr, y + h);
  ctx.quadraticCurveTo(x, y + h, x, y + h - rr);
  ctx.lineTo(x, y + rr);
  ctx.quadraticCurveTo(x, y, x + rr, y);
  ctx.closePath();
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

// Apply an alpha to any color string (hex or named/rgb resolved via parse).
function withAlpha(c, alpha) {
  const rgb = parseRGB(c);
  if (!rgb) return c;
  return `rgba(${rgb[0]},${rgb[1]},${rgb[2]},${alpha})`;
}

// CSS var resolved to an rgba() with the given alpha (for canvas strokes).
function cssVarA(name, fallback, alpha) {
  return withAlpha(cssVar(name, fallback), alpha);
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
    width: 28, height: 28, borderRadius: '50%', flexShrink: 0,
    display: 'flex', alignItems: 'center', justifyContent: 'center',
    background: 'radial-gradient(circle at 32% 28%, var(--accent-hover, #c4b5fd), var(--accent))',
    boxShadow: '0 0 0 1px var(--accent-dim, rgba(167,139,250,0.18)), 0 4px 14px var(--accent-dim, rgba(167,139,250,0.3))',
  },
  brandDotCore: {
    width: 7, height: 7, borderRadius: '50%', background: 'rgba(255,255,255,0.92)',
    boxShadow: '0 0 6px rgba(255,255,255,0.7)',
  },
  title: { fontSize: 16, fontWeight: 700, lineHeight: 1.05, letterSpacing: '-0.015em' },
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
  sortCaret: { marginLeft: 5, fontSize: 11, color: 'var(--accent)' },
  tr: { cursor: 'pointer', borderBottom: '1px solid var(--border-light, var(--border))' },
  td: { padding: '10px 12px', verticalAlign: 'middle', color: 'var(--text)' },
  tdNum: { textAlign: 'right' },
  tdTitle: {
    padding: '10px 12px', fontWeight: 600, display: 'flex', alignItems: 'center', gap: 9,
    maxWidth: 300, minWidth: 150,
  },
  rowTitleText: { overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' },
  tdBar: { padding: '10px 12px', minWidth: 116 },
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

  barCell: { display: 'flex', alignItems: 'center', gap: 9 },
  barTrack: {
    flex: 1, height: 6, background: 'var(--surface2)', borderRadius: 999,
    overflow: 'hidden', minWidth: 40, boxShadow: 'inset 0 0 0 1px var(--border-light, var(--border))',
  },
  barFill: { height: '100%', borderRadius: 999, transition: 'width 0.25s ease', minWidth: 2 },
  barLabel: {
    fontSize: 11.5, color: 'var(--muted)', fontVariantNumeric: 'tabular-nums',
    minWidth: 52, textAlign: 'right', whiteSpace: 'nowrap',
  },

  scrim: { position: 'absolute', inset: 0, background: 'rgba(0,0,0,0.5)', zIndex: 20 },
  panel: {
    position: 'absolute', zIndex: 21, background: 'var(--surface)',
    borderLeft: '1px solid var(--border)', display: 'flex', flexDirection: 'column',
    boxShadow: '-12px 0 40px rgba(0,0,0,0.35)', overflow: 'hidden',
  },
  panelAccent: { position: 'absolute', top: 0, left: 0, right: 0, height: 3, zIndex: 1 },
  panelHead: {
    display: 'flex', alignItems: 'flex-start', justifyContent: 'space-between',
    padding: '18px 16px 12px', gap: 10,
  },
  panelHeadMain: { display: 'flex', gap: 11, minWidth: 0, alignItems: 'flex-start' },
  panelKicker: {
    fontSize: 10, fontWeight: 700, textTransform: 'uppercase', letterSpacing: '0.08em',
    color: 'var(--muted)', marginBottom: 3,
  },
  panelTitle: { fontSize: 19, fontWeight: 700, lineHeight: 1.22, letterSpacing: '-0.015em' },
  closeBtn: {
    border: 'none', background: 'var(--surface2)', color: 'var(--muted)',
    width: 30, height: 30, borderRadius: 8, fontSize: 20, lineHeight: 1, cursor: 'pointer',
    flexShrink: 0, fontFamily: 'var(--font)', display: 'flex', alignItems: 'center',
    justifyContent: 'center', transition: 'background 0.15s, color 0.15s',
  },

  chipRow: { display: 'flex', flexWrap: 'wrap', gap: 7, padding: '0 16px 4px' },
  chip: {
    display: 'flex', flexDirection: 'column', gap: 2, background: 'var(--surface2)',
    border: '1px solid var(--border)', borderRadius: 9, padding: '6px 10px', minWidth: 0,
  },
  chipLabel: {
    fontSize: 9, fontWeight: 700, textTransform: 'uppercase', letterSpacing: '0.06em',
    color: 'var(--muted)',
  },
  chipValue: {
    fontSize: 13, fontWeight: 600, color: 'var(--text)', fontVariantNumeric: 'tabular-nums',
    display: 'flex', alignItems: 'center', minHeight: 16,
  },

  tagRow: { display: 'flex', flexWrap: 'wrap', gap: 6, padding: '10px 16px 4px' },
  tag: {
    fontSize: 11.5, color: 'var(--accent)', background: 'var(--accent-dim, rgba(167,139,250,0.12))',
    borderRadius: 999, padding: '2px 9px', fontWeight: 500,
  },
  panelBody: {
    flex: 1, overflowY: 'auto', padding: '14px 16px 20px', fontSize: 14, lineHeight: 1.62,
    borderTop: '1px solid var(--border)', marginTop: 10,
  },
  notePlaceholder: { display: 'flex', flexDirection: 'column', gap: 11, paddingTop: 4 },
  pre: { whiteSpace: 'pre-wrap', wordBreak: 'break-word', fontFamily: 'var(--mono)', fontSize: 12.5 },
  panelFoot: { padding: 14, borderTop: '1px solid var(--border)', background: 'var(--surface)' },
  discussBtn: {
    width: '100%', border: 'none', borderRadius: 11, padding: '12px 16px',
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
.mg-legend-row:hover { background: var(--surface2); }
.mg-tgl:hover { color: var(--text); }
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
.mg-panel { inset: 0 0 0 auto; width: min(460px, 92vw); animation: mg-panel-in 0.22s cubic-bezier(0.22,1,0.36,1); }
.mg-scrim { animation: mg-scrim-in 0.2s ease; }
@media (max-width: 640px) {
  .mg-panel {
    inset: auto 0 0 0; width: 100%; height: 82%; border-left: none;
    border-top: 1px solid var(--border); border-radius: 18px 18px 0 0;
    animation: mg-sheet-in 0.26s cubic-bezier(0.22,1,0.36,1);
  }
}
@keyframes mg-sheet-in { from { transform: translateY(28px); opacity: 0; } to { transform: translateY(0); opacity: 1; } }

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
