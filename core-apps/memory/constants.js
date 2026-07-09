export const NOTE_BASE = '/api/storage/shared/memory/';
// Self-hosted under /vendor (frontend/public/vendor/, precached by sw.js).
// Prod CSP (script-src 'self' 'unsafe-inline' https://esm.sh) blocks
// cdn.jsdelivr.net, which silently degraded the graph to the list view.
// Same dist files the jsdelivr URLs served; both expose classic-script
// globals (window.d3 / window.PIXI).
export const D3_URL = '/vendor/d3@7.9.0/d3.min.js';
export const PIXI_URL = '/vendor/pixi.js@8.19.0/pixi.min.js';
export const MAX_LOCAL_DEPTH = 4;
export const WIKILINK_RE = /\[\[\s*([^\]|#]+?)\s*(?:#[^\]|]*)?(?:\|\s*([^\]]+?)\s*)?\]\]/g;

// Stable, theme-agnostic accent palette for primary-MOC color coding.
// Chosen for distinguishability in both light and dark mode; MOC nodes
// themselves render in the theme accent so they read as "hubs".
export const PALETTE = [
  '#6ea8fe', '#f59e8b', '#7dd3a8', '#c7a3f0', '#f0c674',
  '#5fc8d8', '#ef9bc4', '#9bd065', '#f08c5a', '#8ea0ec',
  '#d99bef', '#5bbf9e',
];

// ------------------------------------------------------------------- styles ---

export const S = {
  root: {
    height: '100%', width: '100%', maxWidth: '100%', overflow: 'hidden',
    background: 'var(--bg)', color: 'var(--text)',
    fontFamily: 'var(--font)', display: 'flex', flexDirection: 'column', position: 'relative',
    WebkitTapHighlightColor: 'transparent',
  },
  header: {
    display: 'flex', alignItems: 'center', justifyContent: 'space-between',
    padding: '11px 14px', borderBottom: '1px solid var(--border)',
    background: 'var(--surface)', flexShrink: 0, gap: 12, position: 'relative', zIndex: 5,
  },
  brand: { display: 'flex', alignItems: 'center', gap: 11, minWidth: 0 },
  brandDot: {
    width: 34, height: 34, borderRadius: 8, flexShrink: 0,
    display: 'flex', alignItems: 'center', justifyContent: 'center',
    background: 'color-mix(in srgb, var(--accent) 16%, transparent)',
    boxShadow: 'none',
  },
  brandDotCore: {
    width: 7, height: 7, borderRadius: '50%', background: 'var(--accent)',
    boxShadow: 'none',
  },
  brandIcon: {
    width: 34, height: 34, borderRadius: 8, flexShrink: 0, display: 'block',
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
    fontSize: 10, fontWeight: 650, textTransform: 'none', borderRadius: 4,
    padding: '1px 5px', flexShrink: 0, letterSpacing: 0,
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
  centerTitle: { fontSize: 17, fontWeight: 600, color: 'var(--text)', letterSpacing: 0 },
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
    fontSize: 11, fontWeight: 650, textTransform: 'none', letterSpacing: 0,
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
    color: 'var(--muted)', fontWeight: 650, fontSize: 11.5, textTransform: 'none',
    letterSpacing: 0, padding: '11px 12px', borderBottom: '1px solid var(--border)',
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
    background: 'var(--surface2)', color: 'var(--muted)', letterSpacing: 0,
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
  panelTitle: { fontSize: 18, fontWeight: 700, lineHeight: 1.18, letterSpacing: 0 },
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
    fontSize: 11, fontWeight: 650, textTransform: 'none', letterSpacing: 0,
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
