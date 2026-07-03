import { MAX_LOCAL_DEPTH, WIKILINK_RE } from './constants.js'

export function hashStr(s) {
  let h = 0;
  for (let i = 0; i < s.length; i++) {
    h = (h * 31 + s.charCodeAt(i)) | 0;
  }
  return Math.abs(h);
}

// Strip a leading YAML frontmatter block (between the first two '---' lines).
export function stripFrontmatter(md) {
  if (!md) return '';
  const m = md.match(/^---\s*\n([\s\S]*?)\n---\s*\n?/);
  return m ? md.slice(m[0].length) : md;
}

// Pull a few useful fields out of frontmatter for the panel header.
export function parseFrontmatter(md) {
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

export function fmtBytes(n) {
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
export function relDate(s) {
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

export function escapeHtml(s) {
  return String(s)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
}

export function escapeMarkdownLinkText(s) {
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
export function restrictNoteHtml(html) {
  const tpl = document.createElement('template');
  tpl.innerHTML = html;
  for (const a of tpl.content.querySelectorAll('a[href]')) {
    if (!a.getAttribute('href').startsWith('#memory-node-')) a.removeAttribute('href');
  }
  return tpl.innerHTML;
}

export function clamp(v, min, max) {
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
export function cssVar(name, fallback) {
  const v = getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  return v || fallback;
}

// Parse a CSS color string to [r,g,b]. Handles #rgb, #rrggbb, and rgb()/rgba().
export function parseRGB(c) {
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
