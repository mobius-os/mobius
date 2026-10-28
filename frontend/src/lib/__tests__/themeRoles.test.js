/**
 * Theme colour roles: the text-safe and fill-safe tokens that sit beside the
 * legacy --accent / --danger / --green, and the derivation that fills them in
 * for a theme that omitted them.
 *
 * The contract under test: every shipped text role reads at >= 4.5:1 on each
 * surface of its mode, every shipped fill carries --accent-fg at >= 4.5:1
 * (rest and hover), the brand purple itself never changes, and a theme with
 * its own base colours gets roles derived from THOSE colours rather than the
 * shipped purple/red/green.
 *
 * The cross-language cases in themeRoleCases.json are asserted here and in
 * backend/tests/test_theme_roles.py, so the shell's local toggle paint and
 * the server's augmentation cannot drift apart.
 */
import { test } from 'node:test'
import assert from 'node:assert/strict'
import cases from './themeRoleCases.json' with { type: 'json' }
import cssCases from './themeCssCases.json' with { type: 'json' }
import {
  DARK_COLORS,
  LIGHT_COLORS,
  DARK_ROLES,
  LIGHT_ROLES,
  contrastRatio,
  inferThemeMode,
  parseThemeMeta,
  scanRootDeclarations,
  themeRoleVars,
  withThemeDefaults,
} from '../../theme.js'

const SURFACES = ['--bg', '--surface', '--surface-2']
const TEXT_ROLES = ['--accent-text', '--danger-text', '--green-text', '--warning', '--warning-text']
const FILL_ROLES = ['--accent-fill', '--accent-fill-hover', '--danger-fill', '--green-fill']
const MODES = [
  ['dark', DARK_COLORS, DARK_ROLES],
  ['light', LIGHT_COLORS, LIGHT_ROLES],
]

for (const [mode, colors, roles] of MODES) {
  const palette = { ...colors, ...roles }

  test(`${mode}: every shipped text role reads at >= 4.5:1 on bg, surface and surface-2`, () => {
    for (const role of TEXT_ROLES) {
      for (const surface of SURFACES) {
        const ratio = contrastRatio(palette[role], palette[surface])
        assert.ok(ratio >= 4.5,
          `${mode} ${role} ${palette[role]} on ${surface} ${palette[surface]} is ${ratio.toFixed(2)}:1`)
      }
    }
  })

  test(`${mode}: white --accent-fg reads at >= 4.5:1 on every shipped fill, rest and hover`, () => {
    for (const role of FILL_ROLES) {
      const ratio = contrastRatio('#ffffff', palette[role])
      assert.ok(ratio >= 4.5, `${mode} white on ${role} ${palette[role]} is ${ratio.toFixed(2)}:1`)
    }
  })

  test(`${mode}: the brand accent stays the shipped purple`, () => {
    assert.equal(palette['--accent'], '#8b6cf7')
  })

  test(`${mode}: shipped bases resolve to exactly the shipped roles`, () => {
    assert.deepEqual(themeRoleVars(colors, mode), { '--accent-fg': '#ffffff', ...roles })
  })
}

test('the light status colours carried into a dark theme still get dark-safe roles', () => {
  // A pre-fix toggle left light --danger/--green in many dark theme.css files.
  // Shipped values of either mode are recognised as shipped, so the dark roles
  // apply instead of a derivation from the light red.
  const carried = { ...DARK_COLORS, '--danger': LIGHT_COLORS['--danger'], '--green': LIGHT_COLORS['--green'] }
  const roles = themeRoleVars(carried, 'dark')
  assert.equal(roles['--danger-text'], DARK_ROLES['--danger-text'])
  assert.equal(roles['--green-fill'], DARK_ROLES['--green-fill'])
})

test('a custom accent derives readable roles from its own hue, not the shipped purple', () => {
  for (const [mode, colors] of [['dark', DARK_COLORS], ['light', LIGHT_COLORS]]) {
    const roles = themeRoleVars({ ...colors, '--accent': '#0ea5e9' }, mode)
    assert.notEqual(roles['--accent-text'], DARK_ROLES['--accent-text'])
    assert.notEqual(roles['--accent-text'], LIGHT_ROLES['--accent-text'])
    for (const surface of SURFACES) {
      assert.ok(contrastRatio(roles['--accent-text'], colors[surface]) >= 4.5,
        `${mode} derived accent text ${roles['--accent-text']} on ${surface}`)
    }
    for (const fill of ['--accent-fill', '--accent-fill-hover']) {
      assert.ok(contrastRatio(roles['--accent-fg'], roles[fill]) >= 4.5,
        `${mode} ${roles['--accent-fg']} on derived ${fill} ${roles[fill]}`)
    }
  }
})

test('a light accent flips its foreground dark and keeps the fill readable', () => {
  const roles = themeRoleVars({ ...LIGHT_COLORS, '--accent': '#fde047' }, 'light')
  assert.equal(roles['--accent-fg'], '#000000')
  assert.ok(contrastRatio('#000000', roles['--accent-fill']) >= 4.5)
  // Danger/green fills follow the same foreground the theme pairs them with.
  assert.ok(contrastRatio('#000000', roles['--danger-fill']) >= 4.5)
  assert.ok(contrastRatio('#000000', roles['--green-fill']) >= 4.5)
})

test('an unparseable custom base keeps its own value instead of the shipped colour', () => {
  const roles = themeRoleVars({ ...DARK_COLORS, '--accent': 'oklch(70% 0.2 150)' }, 'dark')
  assert.equal(roles['--accent-text'], 'oklch(70% 0.2 150)')
  assert.equal(roles['--accent-fill'], 'oklch(70% 0.2 150)')
})

test('cross-language role cases match the backend', () => {
  assert.ok(cases.length >= 10, 'the shared case table must not be empty')
  for (const { name, mode, declared, expected } of cases) {
    assert.deepEqual(themeRoleVars(declared, mode), expected, name)
  }
})

test('whole-stylesheet cases: the shell paints byte-for-byte what the server serves', () => {
  // Expected values are backend _ensure_core_vars(css); test_theme_roles.py
  // resolves the same bytes through a cascade oracle to prove owner
  // declarations win and every core variable resolves.
  assert.ok(cssCases.length >= 20)
  for (const { name, css, expected } of cssCases) {
    assert.equal(withThemeDefaults(css), expected, name)
  }
})

test('the defaults layer is declared first; its block follows the owner imports', () => {
  const css = '@import url("https://fonts.example.test/a.css");\n:root { --bg: #f0eeeb; }\n'
  const out = withThemeDefaults(css)
  assert.ok(out.startsWith('@layer mobius-defaults;\n@import url("https://fonts.example.test/a.css");/* '))
  assert.ok(out.endsWith(':root { --bg: #f0eeeb; }\n'))
  assert.equal(withThemeDefaults(out), out, 'regenerating is idempotent')
})

test('only plain top-level :root rules feed derivation, read past quotes and url()', () => {
  const root = scanRootDeclarations(`
    :root { --a: 1; --b: 2 !important; --q: "}"; --u: url(x}y); }
    :root { --a: 3; --b: 4; .nested { --e: 7; } }
    @media (min-width: 1px) { :root { --c: 5; } }
    .x--y:hover { --d: 6; }
    /* :root { --f: 8; } */
  `)
  assert.deepEqual(root, { '--a': '3', '--b': '2', '--q': '"}"', '--u': 'url(x}y)' })
  assert.equal(scanRootDeclarations(':root { --x: "never closed; }'), null)
})

test('the root editor keeps a winning !important declaration and its importance', () => {
  const meta = parseThemeMeta(':root { --accent: #00ffff !important; --accent: #ff0000; --bg: #0d0d0d; --bg: #111111; }\n')
  assert.equal(meta.colors['--accent'], '#00ffff !important')
  assert.equal(meta.colors['--bg'], '#111111', 'without importance the last declaration still wins')
})

test('the root editor reads an escaped ) in a url() value as one value', () => {
  const meta = parseThemeMeta(':root { --img: url(a\\);b); --accent: #00ffff; }\n')
  assert.equal(meta.colors['--img'], 'url(a\\);b)')
  assert.equal(meta.colors['--accent'], '#00ffff')
})

test('mode comes from the winning :root --bg, never from a comment or string', () => {
  assert.equal(inferThemeMode('/* --bg: #ffffff; */ :root { --note: "--bg: #fff"; }'), 'dark')
  assert.equal(inferThemeMode(':root { --bg: #0d0d0d; } :root { --bg: #f0eeeb; }'), 'light')
})

test('a fill under a grey label moves toward whichever endpoint can pass', () => {
  // Deriving away from the label alone chose white here, which a #888888
  // label cannot reach 4.5:1 on.
  const roles = themeRoleVars({ ...LIGHT_COLORS, '--accent': '#999999', '--accent-fg': '#888888' }, 'light')
  for (const fill of ['--accent-fill', '--accent-fill-hover', '--danger-fill', '--green-fill']) {
    assert.ok(contrastRatio('#888888', roles[fill]) >= 4.5, `${fill} ${roles[fill]}`)
  }
})

test('an unreachable text target keeps the theme base instead of certifying a colour', () => {
  const surfaces = { '--bg': '#777777', '--surface': '#ffffff', '--surface-2': '#000000' }
  const roles = themeRoleVars({ ...DARK_COLORS, ...surfaces, '--danger': '#ff2d55' }, 'dark')
  assert.equal(roles['--danger-text'], '#ff2d55')
})

test('translucent colours are unsupported rather than read as opaque', () => {
  assert.equal(contrastRatio('rgba(255, 255, 255, 0.5)', '#000000'), 0)
  assert.equal(contrastRatio('rgb(255 255 255 / 100%)', '#000000') > 20, true)
})

// Composed states (text over the tints and translucent fills the shell really
// paints) are asserted in backend/tests/test_theme_roles.py
// `test_composed_states_read_at_4_5_on_their_real_backgrounds`, which reads
// each colour and background from its consuming CSS rule so the recipes
// cannot drift from the stylesheet. The disabled-control exemption is
// recorded there.
