// Shared theme constants and helpers used by Shell (auto-detect) and SettingsView (toggle).

// Palette neutralized 2026-05: dropped the slight blue tint so the
// dark stack reads as true charcoal. --muted bumped twice:
//   2026-05-26: #6b6b76 (~3.8:1, failed WCAG AA) → #9b9b9b (~6.4:1)
//   2026-05-27: #9b9b9b → #a8a8a8 (~6.1:1 on --surface2 #212121, was
//     ~5.2:1 — comfortable AA on raised surfaces for the small text
//     used in section labels and provider status indicators).
// Must stay in sync with backend/app/theme.py DEFAULT_THEME.
export const DARK_COLORS = {
  '--bg': '#0d0d0d',
  '--surface': '#171717',
  '--surface2': '#212121',
  '--border': '#2a2a2a',
  '--border-light': '#1f1f1f',
  '--text': '#ececec',
  '--muted': '#a8a8a8',
  '--accent': '#8b6cf7',
  '--accent-hover': '#7c5ce6',
  '--accent-dim': 'rgba(139, 108, 247, 0.14)',
  '--danger': '#f87171',
  '--green': '#10b981',
}

// Light palette tightened 2026-05-27: --muted #7a7772 was 4.6:1 on
// --bg #f0eeeb (knife-edge AA fail for small text); bumped to
// #6b6864 (~5.4:1). --surface widened from #f8f7f5 to #ffffff so
// cards read by contrast rather than relying on box-shadow alone
// — the earlier 3-LCh-step ramp made cards almost invisible
// without elevation. Must stay in sync with backend/app/theme.py.
export const LIGHT_COLORS = {
  '--bg': '#f0eeeb',
  '--surface': '#ffffff',
  '--surface2': '#e8e6e2',
  '--border': '#d4d1cc',
  '--border-light': '#e2dfdb',
  '--text': '#1c1b1a',
  '--muted': '#6b6864',
  '--accent': '#8b6cf7',
  '--accent-hover': '#7c5ce6',
  '--accent-dim': 'rgba(139, 108, 247, 0.08)',
  '--danger': '#ef4444',
  '--green': '#059669',
}

// Mirrors `_contrasting_accent_fg` in backend/app/theme.py. `--accent-fg` is the
// one core var whose correct value is a function of another var the theme owns,
// so it must never be a fixed palette entry: a constant here would be spread
// back over the server-derived value on every toggle, and `persistTheme` would
// write it into the first `:root` where `_ensure_core_vars` stops treating it as
// missing -- permanently disabling the derivation for that theme.
// Deliberately narrow: keep white unless it drops below the 3:1 WCAG AA floor
// for UI components, so the shipped accent keeps its white.
export function contrastingAccentFg(accent) {
  const rgb = parseCssRgb(accent)
  if (!rgb) return null
  const channel = (c) => {
    const s = c / 255
    return s <= 0.04045 ? s / 12.92 : ((s + 0.055) / 1.055) ** 2.4
  }
  const [r, g, b] = rgb.map(channel)
  const luminance = 0.2126 * r + 0.7152 * g + 0.0722 * b
  return 1.05 / (luminance + 0.05) >= 3 ? '#ffffff' : '#000000'
}

function parseCssRgb(value) {
  const text = String(value || '').trim().toLowerCase()
  const hex = /^#([0-9a-f]{3}|[0-9a-f]{6})$/.exec(text)
  if (hex) {
    const d = hex[1].length === 3 ? [...hex[1]].map((c) => c + c).join('') : hex[1]
    return [0, 2, 4].map((i) => parseInt(d.slice(i, i + 2), 16))
  }
  const fn = /^rgba?\(([^)]*)\)$/.exec(text)
  if (fn) {
    const parts = fn[1].replace(/\//g, ' ').split(/[,\s]+/).filter(Boolean).slice(0, 3)
    if (parts.length !== 3) return null
    const channels = parts.map((p) => p.endsWith('%')
      ? Math.round((parseFloat(p) * 255) / 100)
      : Math.round(parseFloat(p)))
    if (channels.some((c) => !Number.isFinite(c))) return null
    return channels.map((c) => Math.max(0, Math.min(255, c)))
  }
  return null
}

export function parseThemeMeta(css) {
  const imports = []
  let rest = css
  // Strip @imports (captured separately)
  rest = rest.replace(/@import\s+url\(\s*['"]([^'"]+)['"]\s*\)\s*;[^\S\n]*\n?/g, (_, url) => {
    imports.push(`@import url('${url}');`)
    return ''
  })
  const rootBlock = extractRootBlock(rest)
  const font = (rootBlock.match(/--font:\s*([^;]+);/) || [])[1]?.trim() || "'Inter', system-ui, sans-serif"
  const mono = (rootBlock.match(/--mono:\s*([^;]+);/) || [])[1]?.trim() || "'JetBrains Mono', ui-monospace, monospace"
  const fontSize = (rootBlock.match(/font-size:\s*([^;]+);/) || [])[1]?.trim() || '15px'
  // Extract CSS custom properties only from the first top-level :root block.
  // Rules outside :root can legally include class names like
  // `.settings__section--compact:last-child`; scanning the whole stylesheet
  // misread that selector as a bogus custom property (`--compact`) and a
  // later theme toggle rebuilt theme.css with the selector folded into :root.
  const colors = {}
  rootBlock.replace(/--([\w-]+):\s*([^;]+);/g, (_, name, value) => {
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

function extractRootBlock(css) {
  const m = css.match(/:root\s*\{/)
  if (!m) return ''
  const start = m.index
  let depth = 0
  const open = css.indexOf('{', start)
  for (let i = open; i < css.length; i++) {
    const c = css[i]
    if (c === '{') depth++
    else if (c === '}') {
      depth--
      if (depth === 0) return css.slice(open + 1, i)
    }
  }
  return ''
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
