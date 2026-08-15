// The idle shell must stop asking the browser for frames. Three separate
// sources kept the frame pipeline alive with the app doing nothing, and each
// one is pinned below:
//   1. useFileUpload handed the composer a fresh callback identity on every
//      render, re-allocating ChatView's `doSend` and re-rendering the whole
//      transcript on each keystroke.
//   2. the pane-resize correction ran post-paint, so a geometry change showed
//      one frame at the stale scroll position before the fix landed.
//   3. the drawer's streaming dot animated on an `infinite` loop, and the
//      drawer is always mounted.
//
// Run with:
//   cd frontend && node --loader=./src/lib/__tests__/vite-env-loader.mjs \
//     --test src/components/ChatView/__tests__/framePipelineQuiescence.test.js
//
// The loader aliases `react` -> react-hook-shim for useFileUpload.js so the
// hook can be driven from node without a renderer. See react-hook-shim.mjs.

import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { renderHook } from '../hooks/__tests__/react-hook-shim.mjs'
import useFileUpload from '../useFileUpload.js'

const chatView = readFileSync(new URL('../ChatView.jsx', import.meta.url), 'utf8')
// Comments stripped: the rules below are documented by prose that names the
// very properties and keywords being asserted against ("the animation never
// ended", "never `infinite`"), so a whole-file scan has to look at declarations
// only or it reads the warning as the violation.
const drawerCss = readFileSync(
  new URL('../../Drawer/Drawer.css', import.meta.url),
  'utf8',
).replace(/\/\*[\s\S]*?\*\//g, '')

const ACTIONS = ['addFiles', 'removeFile', 'releaseFiles', 'clearFiles', 'restoreFiles']

// ChatView rebuilds this argument object — and a fresh `onFilesChange` arrow —
// on every render, which is exactly the churn the memoization has to absorb.
function props(chatId = 'chat-1') {
  return { chatId, onFilesChange: () => {} }
}

test('attachment actions keep their identity when the composer re-renders', () => {
  const { result, rerender } = renderHook(useFileUpload, props())
  const first = { ...result.current }

  rerender(props())
  rerender(props())

  for (const action of ACTIONS) {
    assert.equal(
      result.current[action],
      first[action],
      `${action} must not acquire a new identity on every composer render`,
    )
  }
})

test('attachment actions keep their identity while files are being staged', () => {
  const { result, rerender } = renderHook(useFileUpload, props())
  const first = { ...result.current }

  // A state change inside the hook, not just a parent re-render: restoreFiles
  // commits new files, which is the path a draft restore takes.
  result.current.restoreFiles([{ id: 'f1', name: 'a.png', status: 'done' }])
  rerender(props())

  assert.equal(result.current.files.length, 1, 'guard: the commit actually landed')
  for (const action of ACTIONS) {
    assert.equal(result.current[action], first[action], `${action} churned on a file commit`)
  }
})

test('attachment actions that talk to a chat are reissued when the chat changes', () => {
  // The other half of the contract: memoization that never invalidates would
  // leave addFiles/removeFile calling the previous chat's upload endpoint.
  const { result, rerender } = renderHook(useFileUpload, props('chat-1'))
  const first = { ...result.current }

  rerender(props('chat-2'))

  assert.notEqual(result.current.addFiles, first.addFiles)
  assert.notEqual(result.current.removeFile, first.removeFile)
  // These three never read chatId, so they legitimately stay put.
  for (const action of ['releaseFiles', 'clearFiles', 'restoreFiles']) {
    assert.equal(result.current[action], first[action])
  }
})

// Whitespace- and formatting-independent: find the call that encloses the
// scroll write rather than matching one specific line layout.
function enclosingEffectHook(source, needle) {
  const at = source.indexOf(needle)
  assert.notEqual(at, -1, `${needle} not found in ChatView.jsx`)
  const before = source.slice(0, at)
  // 'useLayoutEffect(' does not contain 'useEffect(' as a substring, so the
  // later of the two indexes is unambiguously the enclosing hook.
  return before.lastIndexOf('useLayoutEffect(') > before.lastIndexOf('useEffect(')
    ? 'useLayoutEffect'
    : 'useEffect'
}

test('pane resize correction runs before paint', () => {
  assert.equal(
    enclosingEffectHook(chatView, 'if (paneContentHeight != null) paneResized()'),
    'useLayoutEffect',
    'a post-paint pane-resize correction shows one frame at the stale scroll position',
  )
})

function ruleBody(selector) {
  const rule = drawerCss.match(new RegExp(`\\${selector}\\s*\\{([^}]*)\\}`))
  assert.ok(rule, `${selector} rule must remain present in Drawer.css`)
  return rule[1].trim()
}

test('the retired drawer pulse does not return', () => {
  assert.doesNotMatch(ruleBody('.drawer__streaming-dot'), /animation\s*:/)
  assert.doesNotMatch(drawerCss, /@keyframes\s+drawer-streaming-pulse/)
})

test('streaming and attention rows stay tellable apart without motion', () => {
  // Removing the pulse left these two rulesets byte-identical, which collapsed
  // "the agent is working" and "it finished while you were away" into one
  // visual. The distinction must stay static — re-adding an infinite animation
  // to always-mounted chrome is the bug this file exists to prevent.
  const streaming = ruleBody('.drawer__streaming-dot')
  const attention = ruleBody('.drawer__attention-dot')
  assert.notEqual(streaming, attention, 'the two drawer row states render identically')
  // Streaming is a filled disc in --accent; the finished/attention dot is a
  // hollow ring (transparent fill + coloured border) so the two stay tellable
  // apart on a non-colour channel for colour-vision-deficient users.
  assert.match(streaming, /background:\s*var\(--accent\)/)
  assert.match(attention, /background:\s*transparent/)
  assert.match(attention, /border:[^;]*var\(--green\)/)
})
