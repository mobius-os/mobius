/**
 * Regression guard: ChatEmbed must apply the theme on mount.
 *
 * Run with:
 *   cd frontend && node --test src/lib/__tests__/chatEmbedTheme.test.js
 *
 * The embed branch (App.jsx) renders ChatEmbed OUTSIDE Shell, which is the
 * only other useTheme() caller. Without ChatEmbed calling useTheme() itself,
 * the embed inherits no theme: it paints with the unstyled default tokens
 * (black-on-black in dark mode, black composer in light mode) — exactly the
 * bug this guards against. useTheme() reads the effective theme through React
 * Query and runs applyThemeToDom, so the embed self-themes correctly in BOTH
 * modes and live-updates when the theme query is invalidated.
 *
 * A full component render of ChatEmbed pulls in ChatView + React Query and is
 * not feasible under the repo's lightweight node:test loaders, so we assert the
 * wiring at the source level (same approach as shellReloadExport.test.js and
 * swNavigationDenylist.test.js). The two load-bearing facts are: (1) the hook
 * is imported, and (2) it is invoked unconditionally at the top of the
 * component body — i.e. on every mount, before any early return.
 */
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'

const SOURCE = readFileSync(
  new URL('../../components/ChatEmbed/ChatEmbed.jsx', import.meta.url),
  'utf8',
)

test('ChatEmbed imports the shared useTheme hook', () => {
  assert.match(
    SOURCE,
    /import\s+useTheme\s+from\s+['"][^'"]*hooks\/useTheme(?:\.js)?['"]/,
    'ChatEmbed must import useTheme from hooks/useTheme.js (same hook Shell uses)',
  )
})

test('ChatEmbed invokes useTheme() unconditionally on mount', () => {
  // The hook call must appear inside the component, before the readiness early
  // return, so it runs on every mount regardless of chat/token state.
  const componentStart = SOURCE.indexOf('export default function ChatEmbed(')
  assert.ok(componentStart !== -1, 'ChatEmbed component declaration exists')

  const earlyReturn = SOURCE.indexOf('if (!chatId || !tokenReady)', componentStart)
  assert.ok(earlyReturn !== -1, 'ChatEmbed has the chat/token readiness return')

  const body = SOURCE.slice(componentStart, earlyReturn)
  assert.match(
    body,
    /\buseTheme\(\)/,
    'useTheme() must be called at the top of ChatEmbed (before the early return), '
      + 'so the embed self-themes on every mount',
  )
})
