// Memory — thin app shell. The module tree is declared in mobius.json's
// source_files; the multi-file installer fetches each path and esbuild bundles
// from this entry, resolving the relative imports below at compile time.
//
//   constants.js  — shared storage URLs, graph-runtime URLs, palette, and style table
//   theme.js      — the single app stylesheet (CSS)
//   domain.js     — pure + DOM-level graph, markdown, sanitization, and formatting helpers; no React/network
//   storage.js    — shared-memory read-through cache and subscribe store
//   graph/render.jsx — d3/Pixi runtime loader, renderer component, and renderer math
//   ui/*.jsx      — one React component per file
//
// Only App lives here: it owns top-level graph/note state, persistence wiring,
// shell navigation state, and mounts the graph, list, and note-panel UI.
import { useState, useEffect, useRef, useMemo, useCallback } from 'react'
import { D3_URL, PALETTE, PIXI_URL, S } from './constants.js'
import { CSS } from './theme.js'
import { makeSharedMemoryStore } from './storage.js'
import {
  MEMORY_SANITIZE_OPTIONS,
  buildLocalGraphData,
  cssVar,
  escapeHtml,
  fmtBytes,
  hashStr,
  neutralizeMemoryMarkdown,
  nodeRadius,
  parseFrontmatter,
  relDate,
  renderWikiLinks,
  restrictNoteHtml,
  safeMemoryPath,
  stripFrontmatter,
} from './domain.js'
import { MemoryGraphRenderer, loadScriptOnce } from './graph/render.jsx'
import { Th } from './ui/Th.jsx'
import { ImportanceDots } from './ui/ImportanceDots.jsx'
import { EmptyConstellation } from './ui/EmptyConstellation.jsx'
import { GraphGlyph } from './ui/GraphGlyph.jsx'
import { ListGlyph } from './ui/ListGlyph.jsx'
import { ChatGlyph } from './ui/ChatGlyph.jsx'
import { TextGlyph } from './ui/TextGlyph.jsx'
import { NetworkGlyph } from './ui/NetworkGlyph.jsx'

export { makeSharedMemoryStore } from './storage.js'
export {
  MEMORY_SANITIZE_OPTIONS,
  buildLocalGraphData,
  neutralizeMemoryMarkdown,
  nodeRadius,
  renderWikiLinks,
  safeMemoryPath,
  shouldShowScreenLabel,
  shouldShowNodeLabel,
} from './domain.js'
export {
  computeRendererFitTransform,
  normalizeRendererGraphData,
} from './graph/render.jsx'

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
  // graph.json is rewritten by the chat + reflection agents while this app sits
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
  // the owner (the chat appends to a note, reflection reorganizes it), so it
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
          import('marked'),
          import('dompurify'),
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
    <div className="mg-root" style={S.root}>
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
