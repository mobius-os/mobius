/* Memory Graph — an Obsidian-style force-directed view of the agent's knowledge. */
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
  if (max <= min) return 0.5;
  return Math.max(0, Math.min(1, (v - min) / (max - min)));
}

export default function App({ appId, token }) {
  const [graph, setGraph] = useState(null);
  const [status, setStatus] = useState('loading'); // loading | ready | empty | error
  const [errMsg, setErrMsg] = useState('');
  const [view, setView] = useState('graph'); // graph | list
  const [selected, setSelected] = useState(null); // node object
  const [noteState, setNoteState] = useState({ status: 'idle', md: '', fm: {} });
  const [hoverId, setHoverId] = useState(null);
  const [sortKey, setSortKey] = useState('size_bytes');
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
    const r = 3 + base * 1.6;
    return n.type === 'moc' ? r * 1.35 : r;
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

  // --- Canvas node painter: circle + label in CSS px, theme-colored. ---
  const paintNode = useCallback((node, ctx, globalScale) => {
    const r = radiusForNode(node);
    const isHover = hoverId === node.id;
    const dim = hoverId && hoverId !== node.id &&
      !(neighbors.get(hoverId)?.has(node.id));
    ctx.globalAlpha = dim ? 0.18 : 1;

    ctx.beginPath();
    ctx.arc(node.x, node.y, r, 0, 2 * Math.PI, false);
    ctx.fillStyle = colorForNode(node);
    ctx.fill();

    if (node.type === 'moc') {
      ctx.lineWidth = 1.5 / globalScale;
      ctx.strokeStyle = cssVar('--text', '#e5e5e5');
      ctx.stroke();
    }
    if (isHover) {
      ctx.lineWidth = 2 / globalScale;
      ctx.strokeStyle = cssVar('--accent', '#a78bfa');
      ctx.stroke();
    }

    // Label: draw in CSS px (divide font size by zoom so it stays readable).
    const label = node.title || node.id;
    const fontSize = Math.max(2.5, 11 / globalScale);
    if (globalScale > 0.55 || isHover) {
      ctx.font = `${fontSize}px var(--font, sans-serif)`;
      ctx.textAlign = 'center';
      ctx.textBaseline = 'top';
      ctx.fillStyle = cssVar('--text', '#e5e5e5');
      ctx.globalAlpha = dim ? 0.12 : (isHover ? 1 : 0.85);
      ctx.fillText(label, node.x, node.y + r + 1.5 / globalScale);
    }
    ctx.globalAlpha = 1;
  }, [colorForNode, radiusForNode, hoverId, neighbors]);

  // Larger pointer hit-area than the dot so small nodes are clickable.
  const paintPointer = useCallback((node, color, ctx) => {
    const r = radiusForNode(node);
    ctx.fillStyle = color;
    ctx.beginPath();
    ctx.arc(node.x, node.y, r + 3, 0, 2 * Math.PI, false);
    ctx.fill();
  }, [radiusForNode]);

  const linkColor = useCallback((link) => {
    const s = typeof link.source === 'object' ? link.source.id : link.source;
    const t = typeof link.target === 'object' ? link.target.id : link.target;
    if (hoverId && hoverId !== s && hoverId !== t) {
      return cssVarA('--border', '#33333a', 0.25);
    }
    return link.kind === 'moc'
      ? cssVarA('--accent', '#a78bfa', 0.45)
      : cssVarA('--border', '#33333a', 0.7);
  }, [hoverId]);

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

  // ---------------------------------------------------------------- render ---
  return (
    <div style={S.root}>
      <style>{CSS}</style>

      <header style={S.header}>
        <div style={S.brand}>
          <span style={S.brandDot} />
          <div>
            <div style={S.title}>Memory Graph</div>
            <div style={S.subtitle}>What the agent knows</div>
          </div>
        </div>

        <div style={S.headerRight}>
          {problems.length > 0 && (
            <button
              style={{ ...S.healthBadge, ...(problems.some(p => p.severity === 'error') ? S.healthErr : {}) }}
              onClick={() => setShowHealth((v) => !v)}
              title="Graph health"
            >
              <span style={S.healthDot} />
              {problems.length} issue{problems.length === 1 ? '' : 's'}
            </button>
          )}
          <div style={S.toggle}>
            <button
              style={{ ...S.toggleBtn, ...(view === 'graph' ? S.toggleActive : {}) }}
              onClick={() => setView('graph')}
            >Graph</button>
            <button
              style={{ ...S.toggleBtn, ...(view === 'list' ? S.toggleActive : {}) }}
              onClick={() => setView('list')}
            >List</button>
          </div>
        </div>
      </header>

      {showHealth && problems.length > 0 && (
        <div style={S.healthPanel}>
          {problems.map((p, i) => (
            <div key={i} style={S.healthRow}>
              <span style={{ ...S.sevTag, ...(p.severity === 'error' ? S.sevErr : S.sevWarn) }}>
                {p.severity}
              </span>
              <span style={S.healthKind}>{p.kind}</span>
              <span style={S.healthDetail}>{p.detail}</span>
            </div>
          ))}
        </div>
      )}

      <main style={S.main}>
        {status === 'loading' && (
          <div style={S.center}>
            <div className="mg-spin" style={S.spinner} />
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
            <div style={S.emptyMark}>◍</div>
            <div style={S.centerTitle}>Your memory graph is empty</div>
            <div style={S.centerText}>It grows as you use Möbius.</div>
          </div>
        )}

        {status === 'ready' && view === 'graph' && (
          <div ref={wrapRef} style={S.graphWrap}>
            {FG && dims.w > 0 ? (
              <FG
                ref={fgRef}
                graphData={fgData}
                width={dims.w}
                height={dims.h}
                backgroundColor="rgba(0,0,0,0)"
                nodeRelSize={1}
                nodeVal={(n) => radiusForNode(n)}
                nodeLabel={(n) => n.title || n.id}
                nodeCanvasObject={paintNode}
                nodePointerAreaPaint={paintPointer}
                linkColor={linkColor}
                linkWidth={(l) => (l.kind === 'moc' ? 1.2 : 0.6)}
                cooldownTicks={120}
                onNodeClick={(n) => { setSelected(n); setHoverId(null); }}
                onNodeHover={(n) => setHoverId(n ? n.id : null)}
                onBackgroundClick={() => setSelected(null)}
                d3VelocityDecay={0.3}
              />
            ) : (
              <div style={S.center}>
                <div className="mg-spin" style={S.spinner} />
                <div style={S.centerText}>Laying out the graph…</div>
              </div>
            )}

            {legendItems.length > 0 && (
              <div style={S.legend}>
                <div style={S.legendTitle}>Maps of Content</div>
                <div style={S.legendRow}>
                  <span style={{ ...S.legendSwatch, background: cssVar('--accent', '#a78bfa') }} />
                  <span style={S.legendLabel}>MOC node</span>
                </div>
                {legendItems.slice(0, 12).map((it) => (
                  <div key={it.slug} style={S.legendRow}>
                    <span style={{ ...S.legendSwatch, background: it.color }} />
                    <span style={S.legendLabel}>{it.label}</span>
                  </div>
                ))}
              </div>
            )}
          </div>
        )}

        {status === 'ready' && view === 'list' && (
          <div style={S.listWrap}>
            <table style={S.table}>
              <thead>
                <tr>
                  <Th label="Title" active={sortKey === 'title'} dir={sortDir} onClick={() => toggleSort('title')} align="left" />
                  <Th label="Type" />
                  <Th label="Importance" active={sortKey === 'importance'} dir={sortDir} onClick={() => toggleSort('importance')} />
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
                        {n.title || n.id}
                      </td>
                      <td style={S.td}>
                        <span style={{ ...S.typeTag, ...(n.type === 'moc' ? S.typeMoc : {}) }}>
                          {n.type || 'note'}
                        </span>
                      </td>
                      <td style={{ ...S.td, ...S.tdNum }}>{n.importance || 1}</td>
                      <td style={S.tdBar}>
                        <Bar value={uN} label={String(n.access_count || 0)} hue="var(--green, #6ee7b7)" />
                      </td>
                      <td style={S.tdBar}>
                        <Bar value={sN} label={fmtBytes(n.size_bytes)} hue="var(--accent, #a78bfa)" />
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
          <div style={S.scrim} onClick={closePanel} />
          <aside style={S.panel} className="mg-panel">
            <div style={S.panelHead}>
              <div style={S.panelHeadMain}>
                <span style={{ ...S.rowDot, background: colorForNode(selected), width: 12, height: 12 }} />
                <div>
                  <div style={S.panelTitle}>{selected.title || selected.id}</div>
                  <div style={S.panelMeta}>
                    <span style={{ ...S.typeTag, ...(selected.type === 'moc' ? S.typeMoc : {}) }}>
                      {selected.type || 'note'}
                    </span>
                    <span style={S.metaItem}>{fmtBytes(selected.size_bytes)}</span>
                    <span style={S.metaItem}>★ {selected.importance || 1}</span>
                    <span style={S.metaItem}>used {selected.access_count || 0}×</span>
                  </div>
                </div>
              </div>
              <button style={S.closeBtn} onClick={closePanel} aria-label="Close">×</button>
            </div>

            {Array.isArray(selected.tags) && selected.tags.length > 0 && (
              <div style={S.tagRow}>
                {selected.tags.map((t) => <span key={t} style={S.tag}>#{t}</span>)}
              </div>
            )}

            <div style={S.panelBody} className="mg-md">
              {noteState.status === 'loading' && <div style={S.centerText}>Loading note…</div>}
              {noteState.status === 'missing' && <div style={S.centerText}>No note body on disk for this entry.</div>}
              {noteState.status === 'error' && <div style={S.centerText}>Couldn't load: {noteState.md}</div>}
              {noteState.status === 'ready' && (
                noteHtml != null
                  ? <div dangerouslySetInnerHTML={{ __html: noteHtml }} />
                  : <pre style={S.pre}>{noteState.md}</pre>
              )}
            </div>

            <div style={S.panelFoot}>
              <button style={S.discussBtn} onClick={() => discuss(selected)}>
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
    >
      {label}
      {active && <span style={S.sortCaret}>{dir === 'asc' ? '▲' : '▼'}</span>}
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

// ----------------------------------------------------------------- helpers ---

function escapeHtml(s) {
  return String(s)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
}

// Read a CSS custom property off :root (computed) with a fallback.
let _varCache = null;
function cssVar(name, fallback) {
  if (!_varCache) _varCache = getComputedStyle(document.documentElement);
  const v = _varCache.getPropertyValue(name).trim();
  return v || fallback;
}

// CSS var resolved to an rgba() with the given alpha (for canvas strokes).
function cssVarA(name, fallback, alpha) {
  const hex = cssVar(name, fallback);
  const m = hex.match(/^#?([0-9a-fA-F]{6})$/);
  if (m) {
    const int = parseInt(m[1], 16);
    return `rgba(${(int >> 16) & 255},${(int >> 8) & 255},${int & 255},${alpha})`;
  }
  return hex; // already rgb()/named — let the canvas use it as-is
}

// ------------------------------------------------------------------- styles ---

const S = {
  root: {
    height: '100%', overflow: 'hidden', background: 'var(--bg)', color: 'var(--text)',
    fontFamily: 'var(--font)', display: 'flex', flexDirection: 'column', position: 'relative',
  },
  header: {
    display: 'flex', alignItems: 'center', justifyContent: 'space-between',
    padding: '12px 16px', borderBottom: '1px solid var(--border)',
    background: 'var(--surface)', flexShrink: 0, gap: 12,
  },
  brand: { display: 'flex', alignItems: 'center', gap: 10, minWidth: 0 },
  brandDot: {
    width: 26, height: 26, borderRadius: '50%', flexShrink: 0,
    background: 'radial-gradient(circle at 32% 30%, var(--accent-hover, #c4b5fd), var(--accent))',
    boxShadow: '0 0 0 4px var(--accent-dim, rgba(167,139,250,0.12))',
  },
  title: { fontSize: 16, fontWeight: 700, lineHeight: 1.1, letterSpacing: '-0.01em' },
  subtitle: { fontSize: 11.5, color: 'var(--muted)', marginTop: 1 },
  headerRight: { display: 'flex', alignItems: 'center', gap: 8 },

  toggle: {
    display: 'flex', background: 'var(--surface2)', borderRadius: 8, padding: 2,
    border: '1px solid var(--border)',
  },
  toggleBtn: {
    border: 'none', background: 'transparent', color: 'var(--muted)',
    fontSize: 12.5, fontWeight: 600, padding: '5px 12px', borderRadius: 6,
    cursor: 'pointer', fontFamily: 'var(--font)',
  },
  toggleActive: { background: 'var(--accent)', color: '#fff' },

  healthBadge: {
    display: 'flex', alignItems: 'center', gap: 6, border: '1px solid var(--border)',
    background: 'var(--surface2)', color: 'var(--text)', borderRadius: 8,
    fontSize: 12, fontWeight: 600, padding: '5px 10px', cursor: 'pointer',
    fontFamily: 'var(--font)',
  },
  healthErr: { borderColor: 'var(--danger)', color: 'var(--danger)' },
  healthDot: {
    width: 7, height: 7, borderRadius: '50%', background: 'var(--danger)', flexShrink: 0,
  },
  healthPanel: {
    padding: '8px 16px', background: 'var(--surface2)',
    borderBottom: '1px solid var(--border)', maxHeight: 160, overflowY: 'auto', flexShrink: 0,
  },
  healthRow: { display: 'flex', alignItems: 'baseline', gap: 8, padding: '3px 0', fontSize: 12 },
  sevTag: {
    fontSize: 10, fontWeight: 700, textTransform: 'uppercase', borderRadius: 4,
    padding: '1px 5px', flexShrink: 0, letterSpacing: '0.03em',
  },
  sevWarn: { background: 'rgba(240,198,116,0.18)', color: 'var(--accent-hover, #f0c674)' },
  sevErr: { background: 'rgba(248,113,113,0.18)', color: 'var(--danger)' },
  healthKind: { fontWeight: 600, color: 'var(--text)' },
  healthDetail: { color: 'var(--muted)' },

  main: { flex: 1, position: 'relative', overflow: 'hidden', minHeight: 0 },

  center: {
    position: 'absolute', inset: 0, display: 'flex', flexDirection: 'column',
    alignItems: 'center', justifyContent: 'center', gap: 12, padding: 24, textAlign: 'center',
  },
  centerTitle: { fontSize: 16, fontWeight: 600, color: 'var(--text)' },
  centerText: { fontSize: 13.5, color: 'var(--muted)', maxWidth: 320, lineHeight: 1.5 },
  spinner: {
    width: 28, height: 28, borderRadius: '50%',
    border: '3px solid var(--border)', borderTopColor: 'var(--accent)',
  },
  emptyMark: { fontSize: 46, color: 'var(--accent)', opacity: 0.7, lineHeight: 1 },
  errIcon: {
    width: 40, height: 40, borderRadius: '50%', display: 'flex', alignItems: 'center',
    justifyContent: 'center', fontSize: 22, fontWeight: 800, color: 'var(--danger)',
    border: '2px solid var(--danger)',
  },

  graphWrap: { position: 'absolute', inset: 0 },

  legend: {
    position: 'absolute', left: 12, bottom: 12, background: 'var(--surface)',
    border: '1px solid var(--border)', borderRadius: 10, padding: '10px 12px',
    maxWidth: 200, maxHeight: '60%', overflowY: 'auto',
    boxShadow: '0 4px 18px rgba(0,0,0,0.25)',
  },
  legendTitle: {
    fontSize: 10.5, fontWeight: 700, textTransform: 'uppercase', letterSpacing: '0.06em',
    color: 'var(--muted)', marginBottom: 7,
  },
  legendRow: { display: 'flex', alignItems: 'center', gap: 8, padding: '2px 0' },
  legendSwatch: { width: 11, height: 11, borderRadius: 3, flexShrink: 0 },
  legendLabel: {
    fontSize: 12, color: 'var(--text)', whiteSpace: 'nowrap',
    overflow: 'hidden', textOverflow: 'ellipsis',
  },

  listWrap: { position: 'absolute', inset: 0, overflowY: 'auto', padding: '4px 0 24px' },
  table: { width: '100%', borderCollapse: 'collapse', fontSize: 13 },
  th: {
    position: 'sticky', top: 0, zIndex: 1, background: 'var(--surface)',
    color: 'var(--muted)', fontWeight: 600, fontSize: 11.5, textTransform: 'uppercase',
    letterSpacing: '0.04em', padding: '10px 12px', borderBottom: '1px solid var(--border)',
    whiteSpace: 'nowrap', userSelect: 'none',
  },
  sortCaret: { marginLeft: 4, fontSize: 9 },
  tr: { cursor: 'pointer', borderBottom: '1px solid var(--border-light, var(--border))' },
  td: { padding: '9px 12px', verticalAlign: 'middle', color: 'var(--text)' },
  tdNum: { textAlign: 'right', fontVariantNumeric: 'tabular-nums', color: 'var(--muted)' },
  tdTitle: {
    padding: '9px 12px', fontWeight: 600, display: 'flex', alignItems: 'center', gap: 8,
    maxWidth: 280, minWidth: 140,
  },
  tdBar: { padding: '9px 12px', minWidth: 110 },
  rowDot: { width: 9, height: 9, borderRadius: '50%', flexShrink: 0, display: 'inline-block' },
  typeTag: {
    fontSize: 10.5, fontWeight: 600, padding: '2px 7px', borderRadius: 5,
    background: 'var(--surface2)', color: 'var(--muted)', textTransform: 'lowercase',
  },
  typeMoc: { background: 'var(--accent-dim, rgba(167,139,250,0.15))', color: 'var(--accent)' },

  barCell: { display: 'flex', alignItems: 'center', gap: 8 },
  barTrack: {
    flex: 1, height: 6, background: 'var(--surface2)', borderRadius: 3,
    overflow: 'hidden', minWidth: 40,
  },
  barFill: { height: '100%', borderRadius: 3, transition: 'width 0.2s ease' },
  barLabel: {
    fontSize: 11.5, color: 'var(--muted)', fontVariantNumeric: 'tabular-nums',
    minWidth: 50, textAlign: 'right', whiteSpace: 'nowrap',
  },

  scrim: { position: 'absolute', inset: 0, background: 'rgba(0,0,0,0.45)', zIndex: 20 },
  panel: {
    position: 'absolute', zIndex: 21, background: 'var(--surface)',
    borderLeft: '1px solid var(--border)', display: 'flex', flexDirection: 'column',
    boxShadow: '-8px 0 30px rgba(0,0,0,0.3)',
  },
  panelHead: {
    display: 'flex', alignItems: 'flex-start', justifyContent: 'space-between',
    padding: '14px 16px 10px', gap: 10, borderBottom: '1px solid var(--border)',
  },
  panelHeadMain: { display: 'flex', gap: 10, minWidth: 0, alignItems: 'flex-start' },
  panelTitle: { fontSize: 16, fontWeight: 700, lineHeight: 1.25, letterSpacing: '-0.01em' },
  panelMeta: { display: 'flex', alignItems: 'center', gap: 8, marginTop: 5, flexWrap: 'wrap' },
  metaItem: { fontSize: 11.5, color: 'var(--muted)', fontVariantNumeric: 'tabular-nums' },
  closeBtn: {
    border: 'none', background: 'var(--surface2)', color: 'var(--muted)',
    width: 30, height: 30, borderRadius: 8, fontSize: 20, lineHeight: 1, cursor: 'pointer',
    flexShrink: 0, fontFamily: 'var(--font)',
  },
  tagRow: { display: 'flex', flexWrap: 'wrap', gap: 6, padding: '8px 16px 0' },
  tag: {
    fontSize: 11.5, color: 'var(--accent)', background: 'var(--accent-dim, rgba(167,139,250,0.12))',
    borderRadius: 5, padding: '2px 8px',
  },
  panelBody: { flex: 1, overflowY: 'auto', padding: '12px 16px', fontSize: 14, lineHeight: 1.6 },
  pre: { whiteSpace: 'pre-wrap', wordBreak: 'break-word', fontFamily: 'var(--mono)', fontSize: 12.5 },
  panelFoot: { padding: 14, borderTop: '1px solid var(--border)' },
  discussBtn: {
    width: '100%', border: 'none', borderRadius: 10, padding: '11px 16px',
    background: 'var(--accent)', color: '#fff', fontSize: 14, fontWeight: 600,
    cursor: 'pointer', fontFamily: 'var(--font)',
  },
};

const CSS = `
.mg-spin { animation: mg-spin 0.8s linear infinite; }
@keyframes mg-spin { to { transform: rotate(360deg); } }
.mg-row:hover { background: var(--surface2); }
.mg-panel { inset: 0 0 0 auto; width: min(440px, 92vw); }
@media (max-width: 640px) {
  .mg-panel { inset: auto 0 0 0; width: 100%; height: 78%; border-left: none; border-top: 1px solid var(--border); border-radius: 16px 16px 0 0; }
}
.mg-md h1, .mg-md h2, .mg-md h3 { margin: 14px 0 6px; line-height: 1.25; font-weight: 700; }
.mg-md h1 { font-size: 19px; } .mg-md h2 { font-size: 16px; } .mg-md h3 { font-size: 14px; }
.mg-md p { margin: 8px 0; }
.mg-md ul, .mg-md ol { margin: 8px 0; padding-left: 22px; }
.mg-md li { margin: 3px 0; }
.mg-md a { color: var(--accent); text-decoration: none; }
.mg-md a:hover { text-decoration: underline; }
.mg-md code { background: var(--surface2); border-radius: 4px; padding: 1px 5px; font-family: var(--mono); font-size: 0.86em; }
.mg-md pre { background: var(--surface2); border: 1px solid var(--border); border-radius: 8px; padding: 12px; overflow-x: auto; margin: 10px 0; }
.mg-md pre code { background: none; padding: 0; }
.mg-md blockquote { border-left: 3px solid var(--border); margin: 10px 0; padding: 2px 0 2px 12px; color: var(--muted); }
.mg-md table { border-collapse: collapse; margin: 10px 0; font-size: 13px; }
.mg-md th, .mg-md td { border: 1px solid var(--border); padding: 5px 9px; }
.mg-md img { max-width: 100%; border-radius: 6px; }
.mg-md hr { border: none; border-top: 1px solid var(--border); margin: 14px 0; }
`;
