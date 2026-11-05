// Shared theme constants and helpers used by Shell (auto-detect) and SettingsView (toggle).

// Palette neutralized 2026-05: dropped the slight blue tint so the
// dark stack reads as true charcoal. --muted bumped twice:
//   2026-05-26: #6b6b76 (~3.8:1, failed WCAG AA) → #9b9b9b (~6.4:1)
//   2026-05-27: #9b9b9b → #a8a8a8 (~6.1:1 on --surface-2 #212121, was
//     ~5.2:1 — comfortable AA on raised surfaces for the small text
//     used in section labels and provider status indicators).
// Base colours: what a light/dark toggle writes into theme.css. --accent,
// --danger and --green are the legacy all-purpose tokens (dots, borders,
// washes, focus rings); text and fills use the role tokens below.
// Must stay in sync with backend/app/theme.py DEFAULT_THEME and the
// app-frame.html fallbacks (backend/tests/test_theme_roles.py enforces it).
export const DARK_COLORS = {
  '--bg': '#0d0d0d',
  '--surface': '#171717',
  '--surface-2': '#212121',
  '--border': '#2a2a2a',
  '--border-light': '#1f1f1f',
  '--text': '#ececec',
  '--muted': '#a8a8a8',
  '--accent': '#8b6cf7',
  '--accent-hover': '#7c5ce6',
  '--accent-dim': 'rgba(139, 108, 247, 0.14)',
  '--danger': '#f87171',
  '--green': '#10b981',
  '--warning': '#d8922f',
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
  '--surface-2': '#e8e6e2',
  '--border': '#d4d1cc',
  '--border-light': '#e2dfdb',
  '--text': '#1c1b1a',
  '--muted': '#6b6864',
  '--accent': '#8b6cf7',
  '--accent-hover': '#7c5ce6',
  '--accent-dim': 'rgba(139, 108, 247, 0.08)',
  '--danger': '#ef4444',
  '--green': '#059669',
  '--warning': '#8f5a00',
}

// Role tokens for the shipped bases. *-text (and --warning) read at >= 4.5:1
// on --bg, --surface and --surface-2 of their mode; *-fill and
// --accent-fill-hover carry --accent-fg (white) at >= 4.5:1. The brand
// --accent keeps its purple; the action fill is the slightly deeper
// #7c5ce6. A theme that changes a base gets these derived from its own
// colour by `themeRoleVars`. Roles are painted, never written into
// theme.css by a toggle, so they stay derived after a later base edit.
export const DARK_ROLES = {
  '--accent-text': '#957cf8',
  '--accent-fill': '#7c5ce6',
  '--accent-fill-hover': '#7053cf',
  '--danger-text': '#f87171',
  '--danger-fill': '#dc2626',
  '--green-text': '#10b981',
  '--green-fill': '#047857',
  '--warning-text': '#d8922f',
}

export const LIGHT_ROLES = {
  '--accent-text': '#6548d4',
  '--accent-fill': '#7c5ce6',
  '--accent-fill-hover': '#7053cf',
  '--danger-text': '#b91c1c',
  '--danger-fill': '#dc2626',
  '--green-text': '#046c4e',
  '--green-fill': '#047857',
  '--warning-text': '#8f5a00',
}

// Which base each role is derived from. --warning is itself text-safe, so
// --warning-text exists for symmetry with the other *-text roles.
const ROLE_SOURCES = [
  { base: '--accent', text: '--accent-text', fill: '--accent-fill', fillHover: '--accent-fill-hover' },
  { base: '--danger', text: '--danger-text', fill: '--danger-fill' },
  { base: '--green', text: '--green-text', fill: '--green-fill' },
  { base: '--warning', text: '--warning-text' },
]

export const THEME_ROLE_KEYS = ['--accent-fg', ...Object.keys(DARK_ROLES)]

// Shipped typography, the rest of the generated defaults block. Must match
// backend/app/theme.py DEFAULT_THEME (test_theme_roles.py enforces it).
export const THEME_TYPOGRAPHY = {
  '--font': "'Inter', 'Inter Fallback', system-ui, -apple-system, sans-serif",
  '--mono': "'JetBrains Mono', 'JetBrains Mono Fallback', ui-monospace, 'SF Mono', monospace",
}

// Comment above the generated defaults block (backend/app/theme.py
// INJECTED_DEFAULTS_MARKER).
export const INJECTED_DEFAULTS_MARKER = 'Möbius: injected defaults for variables the theme omitted'

// The cascade layer holding every generated default. It is declared before
// any owner layer, so it is the weakest layer: owner rules in any layer, and
// unlayered owner rules, beat it. app-frame.html's static fallbacks and
// index.css's typography defaults use the same layer.
export const DEFAULTS_LAYER = 'mobius-defaults'

const escapeRe = (text) => text.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')
// A generated block in the current layered form (matched byte-exactly so
// regenerating is idempotent), its layer-order statement, or either older
// form (leading `:where(:root)`, trailing `:root`).
const INJECTED_DEFAULTS_BLOCK_RE = new RegExp(
  `/\\* ${escapeRe(INJECTED_DEFAULTS_MARKER)} \\*/\\n@layer ${DEFAULTS_LAYER} \\{\\n:where\\(:root\\) \\{\\n[^}]*\\}\\n\\}\\n`
  + `|@layer ${DEFAULTS_LAYER};\\n`
  + `|/\\*\\s*${escapeRe(INJECTED_DEFAULTS_MARKER)}\\s*\\*/\\s*(?::where\\(:root\\)|:root)\\s*\\{[^}]*\\}\\s*`,
  'g')

const MIN_TEXT_CONTRAST = 4.5
const WHITE = [255, 255, 255]
const BLACK = [0, 0, 0]
const SURFACE_KEYS = ['--bg', '--surface', '--surface-2']

// --- The generated defaults block ---------------------------------------------
//
// The effective theme is the owner's CSS plus ONE generated block of every
// core variable in the `mobius-defaults` cascade layer, declared before any
// owner layer so it is the weakest (layout and guarantee: `withThemeDefaults`
// below). Any normal owner declaration applying to the root -- unlayered or
// in any owner layer, in any rule or condition -- beats a default, and a
// variable the owner never declared still resolves. The owner's CSS is only
// read to TAILOR the defaults: its winning plain-:root bases feed role
// derivation and its winning --bg picks the mode.
//
// A base the theme also declares anywhere else (an `@layer`, an `html` or
// component rule, an `@media`/`@supports`/`[data-theme]` condition, a nested
// rule) is not resolved here, so its roles are `var(<base>)`: buttons and
// links follow the owner's base, uncertified, as they did before roles. A
// theme that wants certified text/fill roles puts its bases only in a plain
// top-level `:root`, or declares the matching roles beside them. The block
// holds only hex colours, `var()` of core variables and the shipped
// constants, never owner text.
//
// Mirrors backend/app/theme.py `_ensure_core_vars` byte for byte; the shared
// table themeCssCases.json pins both.

const URL_OPEN_RE = /url\(\s*(?=[^\s"'])/iy
// CSS whitespace and an ASCII CSS <number>/<percentage>, exactly as in
// backend/app/theme.py (`_CSS_WS`, `_CSS_NUMBER_RE`), so both sides read
// malformed input (`rgb(12px, ...)`, a leading BOM) the same way.
const CSS_WS = '[ \\t\\n\\r\\f]'
const cssTrim = (text) => text.replace(new RegExp(`^${CSS_WS}+|${CSS_WS}+$`, 'g'), '')
const CSS_NUMBER_RE = /^[+-]?(?:[0-9]+|[0-9]*\.[0-9]+)(?:[eE][+-]?[0-9]+)?%?$/
const DECLARATION_RE = new RegExp(`^${CSS_WS}*(--[A-Za-z_][A-Za-z0-9_-]*)${CSS_WS}*:([\\s\\S]*)$`)
const IMPORTANT_RE = new RegExp(`!${CSS_WS}*important$`, 'i')
const ROOT_SELECTOR_RE = new RegExp(`^${CSS_WS}*:root${CSS_WS}*$`)

// The comment-blanked text plus a same-length mask in which string and
// unquoted url() contents and escaped characters are neutralised, so
// structure is read from the mask while values are sliced from the text.
// `ok` is false for syntax the lexer cannot follow.
function lexThemeCss(css) {
  let text = ''
  let mask = ''
  let ok = true
  let i = 0
  while (i < css.length) {
    const ch = css[i]
    URL_OPEN_RE.lastIndex = i
    const url = (ch === 'u' || ch === 'U') ? URL_OPEN_RE.exec(css) : null
    if (url) {
      const open = i + url[0].length
      // An unquoted url() ends at its first UNESCAPED `)`: `url(a\);b)` is one
      // token, so its `;` must never read as structure.
      let j = open
      while (j < css.length && css[j] !== ')') j += css[j] === '\\' ? 2 : 1
      if (j >= css.length) { ok = false; j = css.length }
      text += css.slice(i, j)
      mask += css.slice(i, open) + 'x'.repeat(j - open)
      i = j
    } else if (ch === '/' && css[i + 1] === '*') {
      let j = css.indexOf('*/', i + 2)
      if (j < 0) { ok = false; j = css.length } else j += 2
      const blank = ' '.repeat(j - i)
      text += blank
      mask += blank
      i = j
    } else if (ch === '"' || ch === "'") {
      text += ch
      mask += ch
      i++
      let closed = false
      while (i < css.length) {
        const c = css[i]
        if (c === '\\') {
          const pair = css.slice(i, i + 2)
          text += pair
          mask += 'x'.repeat(pair.length)
          i += pair.length
        } else if (c === ch) {
          text += c
          mask += c
          i++
          closed = true
          break
        } else if (c === '\n') {
          break
        } else {
          text += c
          mask += 'x'
          i++
        }
      }
      if (!closed) ok = false
    } else if (ch === '\\') {
      const pair = css.slice(i, i + 2)
      text += pair
      mask += 'x'.repeat(pair.length)
      i += pair.length
    } else {
      text += ch
      mask += ch
      i++
    }
  }
  return { text, mask, ok }
}

/**
 * `{ root, elsewhere }`: the winning custom properties of the theme's
 * unconditional top-level `:root { }` rules (last wins; !important beats
 * later normal ones) and the names of custom properties it declares anywhere
 * else (another rule, condition, layer or nesting level), or null when the
 * CSS uses syntax this tokenizer cannot follow. Only used to tailor the
 * generated defaults, never to decide what is emitted. Mirrors
 * `_scan_theme_css` in backend/app/theme.py.
 */
function scanThemeCss(css) {
  const { text, mask, ok } = lexThemeCss(String(css || '').replace(INJECTED_DEFAULTS_BLOCK_RE, ''))
  const root = {}
  const elsewhere = new Set()
  const important = new Set()
  const record = (decl, inRoot) => {
    const m = DECLARATION_RE.exec(decl)
    if (!m) return
    const name = m[1]
    if (!inRoot) { elsewhere.add(name); return }
    let value = cssTrim(m[2])
    const isImportant = IMPORTANT_RE.test(value)
    if (isImportant) value = cssTrim(value.replace(IMPORTANT_RE, ''))
    if (important.has(name) && !isImportant) return
    if (isImportant) important.add(name)
    root[name] = value
  }
  let depth = 0
  let segment = 0
  let rootRule = false
  for (let i = 0; i < mask.length; i++) {
    const ch = mask[i]
    if (ch === '{') {
      if (depth === 0) rootRule = ROOT_SELECTOR_RE.test(text.slice(segment, i))
      depth++
      segment = i + 1
    } else if (ch === '}') {
      if (depth === 0) return null
      record(text.slice(segment, i), depth === 1 && rootRule)
      depth--
      if (depth === 0) rootRule = false
      segment = i + 1
    } else if (ch === ';') {
      record(text.slice(segment, i), depth === 1 && rootRule)
      segment = i + 1
    }
  }
  return depth || !ok ? null : { root, elsewhere }
}

/** The winning plain-:root custom properties (see `scanThemeCss`), or null. */
export function scanRootDeclarations(css) {
  return scanThemeCss(css)?.root ?? null
}

// The first hex value declared for `name` anywhere in `css` (any rule,
// condition, layer or nesting) outside comments, strings and url(), or null.
// Reads even CSS the scanner cannot follow. Mirrors `_first_hex_anywhere`.
function firstHexAnywhere(css, name) {
  const { mask } = lexThemeCss(String(css || '').replace(INJECTED_DEFAULTS_BLOCK_RE, ''))
  const re = new RegExp(`(?<![A-Za-z0-9_-])${escapeRe(name)}${CSS_WS}*:${CSS_WS}*(#[0-9a-fA-F]{3,8})(?![0-9A-Za-z_-])`)
  return re.exec(mask)?.[1] ?? null
}

/**
 * 'light' or 'dark' from a theme's winning plain-:root --bg, else the first
 * hex --bg declared anywhere; mirrors `_infer_theme_mode`.
 */
export function inferThemeMode(css) {
  let bg = (scanRootDeclarations(css) || {})['--bg'] || ''
  if (!/^#[0-9a-fA-F]{3,8}$/.test(bg)) bg = firstHexAnywhere(css, '--bg') || ''
  const hex = /^#([0-9a-f]{3,8})$/i.exec(bg)?.[1]
  if (!hex || ![3, 4, 6, 8].includes(hex.length)) return 'dark'
  const rgb = hex.length <= 4
    ? [...hex.slice(0, 3)].map((c) => parseInt(c + c, 16))
    : [0, 2, 4].map((k) => parseInt(hex.slice(k, k + 2), 16))
  return (rgb[0] + rgb[1] + rgb[2]) / 3 < 128 ? 'dark' : 'light'
}

/** Every core variable's default for `css`; mirrors `theme_defaults_for_css`. */
export function themeDefaultsForCss(css) {
  const mode = inferThemeMode(css)
  const { root, elsewhere } = scanThemeCss(css)
    || { root: {}, elsewhere: new Set(ROLE_SOURCES.map((source) => source.base)) }
  const accent = elsewhere.has('--accent') ? firstHexAnywhere(css, '--accent') : null
  return {
    ...(mode === 'light' ? LIGHT_COLORS : DARK_COLORS),
    ...THEME_TYPOGRAPHY,
    ...themeRoleVars(root, mode, elsewhere, accent),
  }
}

// (end of a leading @charset, end of the leading statement prelude). CSS
// allows only @charset, @import and @layer statements before other rules;
// their ends are found with the lexer, so a `;` or `{` inside a string, url()
// or comment never ends a statement. Mirrors `_prelude_bounds`.
function preludeBounds(css) {
  const { text, mask } = lexThemeCss(css)
  let charsetEnd = 0
  let preludeEnd = 0
  let i = 0
  for (;;) {
    while (i < mask.length && ' \t\n\r\f'.includes(mask[i])) i++
    const m = /^@(charset|import|layer)\b/i.exec(text.slice(i, i + 8))
    if (!m) break
    let j = i
    while (j < mask.length && mask[j] !== ';' && mask[j] !== '{') j++
    if (j >= mask.length || mask[j] === '{') break // an @layer block is a rule
    i = preludeEnd = j + 1
    if (m[1].toLowerCase() === 'charset' && charsetEnd === 0) charsetEnd = i
  }
  return [charsetEnd, preludeEnd]
}

/**
 * The effective theme (mirrors backend `_ensure_core_vars` byte for byte):
 * a leading @charset stays first, then `@layer mobius-defaults;` so the
 * defaults layer precedes every owner layer, then the owner's @import/@layer
 * statements, then the generated `@layer mobius-defaults { :where(:root) }`
 * block of every core variable, then the rest of the owner's CSS. Any normal
 * owner declaration applying to the root -- unlayered or in any owner layer
 * -- beats a default; a variable the owner never declared still resolves.
 */
export function withThemeDefaults(css) {
  const owner = String(css || '').replace(INJECTED_DEFAULTS_BLOCK_RE, '')
  const defaults = themeDefaultsForCss(owner)
  const lines = Object.keys(defaults).sort().map((k) => `  ${k}: ${defaults[k]};`).join('\n')
  const block = `/* ${INJECTED_DEFAULTS_MARKER} */\n@layer ${DEFAULTS_LAYER} {\n:where(:root) {\n${lines}\n}\n}\n`
  const [charsetEnd, preludeEnd] = preludeBounds(owner)
  return owner.slice(0, charsetEnd) + `@layer ${DEFAULTS_LAYER};\n`
    + owner.slice(charsetEnd, preludeEnd) + block + owner.slice(preludeEnd)
}

/**
 * A pre-paint HINT for an effective theme's background: the owner's winning
 * plain-:root hex --bg, else the first hex --bg declared anywhere (the rule
 * `inferThemeMode` uses), else the generated default. It may not be what a
 * condition or layer resolves, so appliers use this only for chrome painted
 * before the theme stylesheet applies and read the cascade afterwards.
 * Mirrors `_theme_bg` in backend/app/theme.py.
 */
export function themeBg(effectiveCss) {
  const bg = (scanRootDeclarations(effectiveCss) || {})['--bg'] || ''
  if (/^#[0-9a-fA-F]{3,8}$/.test(bg)) return bg
  const anywhere = firstHexAnywhere(effectiveCss, '--bg')
  if (anywhere) return anywhere
  return String(effectiveCss || '').match(/--bg:\s*(#[0-9a-fA-F]{3,8})/)?.[1]
}

// --- Contrast ----------------------------------------------------------------

function relativeLuminance(rgb) {
  const [r, g, b] = rgb.map((c) => {
    const s = c / 255
    return s <= 0.04045 ? s / 12.92 : ((s + 0.055) / 1.055) ** 2.4
  })
  return 0.2126 * r + 0.7152 * g + 0.0722 * b
}

function rgbContrast(a, b) {
  const la = relativeLuminance(a)
  const lb = relativeLuminance(b)
  return (Math.max(la, lb) + 0.05) / (Math.min(la, lb) + 0.05)
}

/** WCAG contrast ratio of two opaque CSS colour literals, or 0 when either is unsupported. */
export function contrastRatio(a, b) {
  const ra = parseCssRgb(a)
  const rb = parseCssRgb(b)
  return ra && rb ? rgbContrast(ra, rb) : 0
}

function toHex(rgb) {
  return '#' + rgb.map((c) => c.toString(16).padStart(2, '0')).join('')
}

// Half-up rounding, identical to backend `_round_half_up` (Python's round()
// is bankers' rounding, so both sides spell it out).
function roundHalfUp(x) {
  return Math.floor(x + 0.5)
}

function mixToward(rgb, target, fraction) {
  return rgb.map((c, k) => roundHalfUp(c + (target[k] - c) * fraction))
}

// The smallest 1% sRGB mix of `rgb` toward black or white that satisfies
// `meets`, trying `preferred` first and keeping it on a tie. Mixing toward
// black or white keeps the hue recognisable. Returns null when neither
// endpoint can satisfy `meets` (for example a mid-grey label): the caller
// must not present any colour as passing.
function nearestPassingMix(rgb, preferred, meets) {
  let best = null
  for (const target of [preferred, preferred === BLACK ? WHITE : BLACK]) {
    for (let i = 0; i <= 100 && (best === null || i < best.step); i++) {
      const mixed = mixToward(rgb, target, i / 100)
      if (meets(mixed)) { best = { step: i, hex: toHex(mixed) }; break }
    }
  }
  return best ? best.hex : null
}

// Keeps white unless it drops below 3:1 on the accent -- the point past which
// deepening the fill would stop looking like the accent -- and otherwise picks
// black. The 3:1 rule only picks the label colour; small-text contrast comes
// from pairing that label with --accent-fill, which `themeRoleVars` deepens
// until the label reaches 4.5:1. So the brand purple keeps white labels on a
// slightly deeper action fill. Mirrors `_contrasting_accent_fg` in
// backend/app/theme.py.
export function contrastingAccentFg(accent) {
  const rgb = parseCssRgb(accent)
  if (!rgb) return null
  return rgbContrast(WHITE, rgb) >= 3 ? '#ffffff' : '#000000'
}

function normalizedColor(value) {
  const text = String(value ?? '').toLowerCase().replace(/[ \t\n\r\f]+/g, '')
  const short = /^#([0-9a-f])([0-9a-f])([0-9a-f])$/.exec(text)
  return short ? `#${short[1]}${short[1]}${short[2]}${short[2]}${short[3]}${short[3]}` : text
}

/** True when `value` is the shipped default for `key` in either mode. */
export function isShippedThemeValue(key, value) {
  const v = normalizedColor(value)
  return [DARK_COLORS, LIGHT_COLORS, DARK_ROLES, LIGHT_ROLES]
    .some((palette) => palette[key] !== undefined && normalizedColor(palette[key]) === v)
}

/**
 * Every role token, plus --accent-fg, for a theme whose plain-:root custom
 * properties are `declared`. `unfollowed` names the custom properties the
 * theme also declares somewhere else, where their value is not known here;
 * `unfollowedAccent` is the owner's accent found there, which picks the
 * --accent-fg label for the `var(--accent)` fill.
 *
 * - A base in `unfollowed` gets `var(<base>)` roles (the pre-role
 *   behaviour), so buttons and links follow the theme's own base.
 * - A shipped base (of either mode) gets the shipped role when that meets
 *   its target on this theme's surfaces / label.
 * - Otherwise the role is the nearest black/white mix of the theme's base
 *   that meets 4.5:1. Guaranteed only for opaque hex/rgb() literals: a
 *   translucent or other colour syntax, or a target no mix can reach, gets
 *   `var(<base>)` -- passed through, not certified.
 * - A label or fill the theme declared is the input for the fills and hover
 *   that pair with it.
 *
 * Every value is a hex colour or a `var()` of a core variable: the owner's
 * text never enters the generated block.
 *
 * These are DEFAULTS in the leading generated block, so any role the owner
 * declares anywhere outranks them. Mirrors `theme_role_vars` in
 * backend/app/theme.py (cross-checked by themeRoleCases.json).
 */
export function themeRoleVars(declared, mode, unfollowed = new Set(), unfollowedAccent = null) {
  const palette = mode === 'light' ? LIGHT_COLORS : DARK_COLORS
  const shippedRoles = mode === 'light' ? LIGHT_ROLES : DARK_ROLES
  const value = (key) => declared[key] ?? palette[key]
  const surfaces = SURFACE_KEYS.map((key) => parseCssRgb(value(key)) || parseCssRgb(palette[key]))
  const lightSurfaces = relativeLuminance(surfaces[0]) > 0.179

  const labelOn = unfollowed.has('--accent') && unfollowedAccent ? unfollowedAccent : value('--accent')
  const out = { '--accent-fg': contrastingAccentFg(labelOn) ?? '#ffffff' }
  const fgRgb = parseCssRgb(declared['--accent-fg'] ?? out['--accent-fg'])

  const readsOnSurfaces = (rgb) => surfaces.every((s) => rgbContrast(rgb, s) >= MIN_TEXT_CONTRAST)
  const carriesFg = (rgb) => fgRgb !== null && rgbContrast(fgRgb, rgb) >= MIN_TEXT_CONTRAST
  // Prefer moving away from the label: deeper under a light label, lighter under a dark one.
  const awayFromFg = (rgb) => (fgRgb && relativeLuminance(fgRgb) < relativeLuminance(rgb) ? WHITE : BLACK)

  for (const source of ROLE_SOURCES) {
    const base = value(source.base)
    const unknown = unfollowed.has(source.base)
    const baseRgb = unknown ? null : parseCssRgb(base)
    const shippedBase = !unknown && isShippedThemeValue(source.base, base)
    const pick = (role, meets, preferred) => {
      if (shippedBase && meets(parseCssRgb(shippedRoles[role]))) return shippedRoles[role]
      return (baseRgb && nearestPassingMix(baseRgb, preferred(baseRgb), meets)) || `var(${source.base})`
    }
    out[source.text] = pick(source.text, readsOnSurfaces, () => (lightSurfaces ? BLACK : WHITE))
    if (source.fill) out[source.fill] = pick(source.fill, carriesFg, awayFromFg)
    if (source.fillHover) {
      const fillRgb = parseCssRgb(declared[source.fill] ?? out[source.fill])
      out[source.fillHover] = fillRgb
        ? toHex(mixToward(fillRgb, awayFromFg(fillRgb), 0.1))
        : 'var(--accent-hover)'
    }
  }
  return out
}

// Opaque colour literals only: #rgb, #rrggbb, rgb()/rgba() whose alpha is
// absent or 1. Translucent colours composite over an unknown backdrop, so
// they are unsupported (null) rather than read as their opaque channels.
function parseCssRgb(value) {
  const text = cssTrim(String(value || '')).toLowerCase()
  const hex = /^#([0-9a-f]{3}|[0-9a-f]{6})$/.exec(text)
  if (hex) {
    const d = hex[1].length === 3 ? [...hex[1]].map((c) => c + c).join('') : hex[1]
    return [0, 2, 4].map((i) => parseInt(d.slice(i, i + 2), 16))
  }
  const fn = /^rgba?\(([^)]*)\)$/.exec(text)
  if (fn) {
    const parts = fn[1].replace(/\//g, ' ').split(/[, \t\n\r\f]+/).filter(Boolean)
    if (parts.length !== 3 && parts.length !== 4) return null
    if (!parts.every((p) => CSS_NUMBER_RE.test(p))) return null
    if (parts.length === 4) {
      const a = parts[3]
      const alpha = a.endsWith('%') ? parseFloat(a) / 100 : parseFloat(a)
      if (!(alpha >= 1)) return null
    }
    const channels = parts.slice(0, 3).map((p) => (p.endsWith('%')
      ? roundHalfUp((parseFloat(p) * 255) / 100)
      : roundHalfUp(parseFloat(p))))
    if (channels.some((c) => !Number.isFinite(c))) return null
    return channels.map((c) => Math.max(0, Math.min(255, c)))
  }
  return null
}

/**
 * The toggle editor's view of a theme: its @imports, fonts and size, the
 * custom properties of its first top-level plain `:root` rule (`colors`),
 * and every other byte of owner CSS (`extras`), preserved verbatim across a
 * mode toggle. The rule is found with the same lexer as the generated
 * defaults, so braces, semicolons and `--name:` text inside strings, url()
 * or comments are never read as structure or declarations. CSS the lexer
 * cannot follow (an unterminated string or comment, unbalanced braces) is
 * not edited: it is all kept as extras and the toggle rebuilds only the
 * leading `:root`. This is a toggle editor for ordinary root palettes, not a
 * lossless CSS rewriter: comments inside that first `:root` are dropped.
 */
export function parseThemeMeta(css) {
  const imports = []
  let rest = css
  // Strip @imports (captured separately)
  rest = rest.replace(/@import\s+url\(\s*['"]([^'"]+)['"]\s*\)\s*;[^\S\n]*\n?/g, (_, url) => {
    imports.push(`@import url('${url}');`)
    return ''
  })
  // The server's generated defaults block is regenerated from the theme on
  // every read; carrying it into a rebuilt theme.css would freeze it as if
  // the owner had declared it.
  rest = rest.replace(INJECTED_DEFAULTS_BLOCK_RE, '')
  const root = firstRootRule(rest)
  const declared = root ? root.declarations : {}
  // Only custom properties (and the root font-size) count: rules outside
  // :root can legally include class names like
  // `.settings__section--compact:last-child`, which a whole-stylesheet scan
  // misread as a bogus `--compact` variable.
  const colors = {}
  for (const [key, value] of Object.entries(declared)) {
    if (key.startsWith('--') && key !== '--font' && key !== '--mono') colors[key] = value
  }
  const font = declared['--font'] || "'Inter', system-ui, sans-serif"
  const mono = declared['--mono'] || "'JetBrains Mono', ui-monospace, monospace"
  const fontSize = declared['font-size'] || '15px'
  const extras = (root ? rest.slice(0, root.start) + rest.slice(root.end) : rest).trim()
  return { imports, font, mono, fontSize, colors, extras }
}

// The first top-level rule whose selector is exactly `:root`, as its byte
// span [start, end) in `css` and its declarations (last wins), or null when
// there is none or the lexer cannot follow the CSS.
function firstRootRule(css) {
  const { text, mask, ok } = lexThemeCss(css)
  if (!ok) return null
  let depth = 0
  let segment = 0
  for (let i = 0; i < mask.length; i++) {
    const ch = mask[i]
    if (ch === ';' && depth === 0) segment = i + 1
    else if (ch === '}') {
      if (depth === 0) return null
      depth--
      if (depth === 0) segment = i + 1
    } else if (ch === '{') {
      const selector = cssTrim(text.slice(segment, i))
      if (depth === 0 && ROOT_SELECTOR_RE.test(selector)) {
        const close = matchingBrace(mask, i)
        if (close < 0) return null
        const start = segment + text.slice(segment, i).indexOf(selector)
        return { start, end: close + 1, declarations: ruleDeclarations(text, mask, i + 1, close) }
      }
      depth++
    }
  }
  return null
}

function matchingBrace(mask, open) {
  let depth = 0
  for (let i = open; i < mask.length; i++) {
    if (mask[i] === '{') depth++
    else if (mask[i] === '}' && --depth === 0) return i
  }
  return -1
}

// The winning `name: value` pair per name directly inside one rule body
// (nested rules skipped): the last declaration wins, except that an
// !important one beats later normal ones. The value keeps its `!important`,
// so rebuilding the rule preserves the owner's importance.
function ruleDeclarations(text, mask, from, to) {
  const out = {}
  let depth = 0
  let segment = from
  const record = (decl) => {
    const m = /^\s*(-?-?[A-Za-z_][\w-]*)\s*:([\s\S]*)$/.exec(decl)
    if (!m) return
    const value = m[2].trim()
    if (IMPORTANT_RE.test(out[m[1]] ?? '') && !IMPORTANT_RE.test(value)) return
    out[m[1]] = value
  }
  for (let i = from; i < to; i++) {
    const ch = mask[i]
    if (ch === '{') depth++
    else if (ch === '}') { depth--; segment = i + 1 }
    else if (ch === ';' && depth === 0) { record(text.slice(segment, i)); segment = i + 1 }
  }
  if (depth === 0) record(text.slice(segment, to))
  return out
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
