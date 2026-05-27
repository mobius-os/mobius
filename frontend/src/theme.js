// Shared theme constants and helpers used by Shell (auto-detect) and SettingsView (toggle).

// Palette neutralized 2026-05: dropped the slight blue tint so the
// dark stack reads as true charcoal; --muted bumped from #6b6b76
// (~3.8:1 — fails WCAG AA) to #9b9b9b (~6.4:1). Must stay in sync
// with backend/app/theme.py DEFAULT_THEME.
export const DARK_COLORS = {
  '--bg': '#0d0d0d',
  '--surface': '#171717',
  '--surface2': '#212121',
  '--border': '#2a2a2a',
  '--border-light': '#1f1f1f',
  '--text': '#ececec',
  '--muted': '#9b9b9b',
  '--accent': '#8b6cf7',
  '--accent-hover': '#7c5ce6',
  '--accent-dim': 'rgba(139, 108, 247, 0.14)',
  '--danger': '#f87171',
  '--green': '#10b981',
}

export const LIGHT_COLORS = {
  '--bg': '#f0eeeb',
  '--surface': '#f8f7f5',
  '--surface2': '#e8e6e2',
  '--border': '#d4d1cc',
  '--border-light': '#e2dfdb',
  '--text': '#1c1b1a',
  '--muted': '#7a7772',
  '--accent': '#8b6cf7',
  '--accent-hover': '#7c5ce6',
  '--accent-dim': 'rgba(139, 108, 247, 0.08)',
  '--danger': '#ef4444',
  '--green': '#059669',
}

export function parseThemeMeta(css) {
  const imports = []
  let rest = css
  // Strip @imports (captured separately)
  rest = rest.replace(/@import\s+url\(\s*['"]([^'"]+)['"]\s*\)\s*;[^\S\n]*\n?/g, (_, url) => {
    imports.push(`@import url('${url}');`)
    return ''
  })
  const font = (css.match(/--font:\s*([^;]+);/) || [])[1]?.trim() || "'Inter', system-ui, sans-serif"
  const mono = (css.match(/--mono:\s*([^;]+);/) || [])[1]?.trim() || "'JetBrains Mono', ui-monospace, monospace"
  const fontSize = (css.match(/font-size:\s*([^;]+);/) || [])[1]?.trim() || '15px'
  // Extract all CSS custom properties so agent-set colors survive toggles.
  const colors = {}
  css.replace(/--([\w-]+):\s*([^;]+);/g, (_, name, value) => {
    const key = `--${name}`
    if (key !== '--font' && key !== '--mono') colors[key] = value.trim()
  })
  // Capture everything OUTSIDE the first top-level :root {...} block so
  // arbitrary extra CSS (scrollbar rules, animations, user tweaks) is
  // preserved across theme toggles. We strip the first :root block by
  // counting brace depth so nested rules don't confuse us.
  const extras = stripRootBlock(rest).trim()
  return { imports, font, mono, fontSize, colors, extras }
}

function stripRootBlock(css) {
  const m = css.match(/:root\s*\{/)
  if (!m) return css
  const start = m.index
  let depth = 0
  let i = css.indexOf('{', start)
  for (; i < css.length; i++) {
    const c = css[i]
    if (c === '{') depth++
    else if (c === '}') {
      depth--
      if (depth === 0) return css.slice(0, start) + css.slice(i + 1)
    }
  }
  return css.slice(0, start)  // unclosed block — drop the rest
}

export function buildThemeCss(colors, meta, mode) {
  const importBlock = meta.imports.length ? meta.imports.join('\n') + '\n\n' : ''
  const vars = Object.entries(colors)
    .map(([k, v]) => `  ${k}: ${v};`)
    .join('\n')
  const extrasBlock = meta.extras ? '\n' + meta.extras + '\n' : ''
  return `${importBlock}:root {
  /* Colors - ${mode} theme */
${vars}

  /* Typography */
  --font: ${meta.font};
  --mono: ${meta.mono};
  font-size: ${meta.fontSize};
  color-scheme: ${mode};
}
${extrasBlock}`
}
