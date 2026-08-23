import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'

import { PIN_BOTTOM_ROOM, PIN_OFFSET } from '../chatContract.js'


test('ChatView only consumes methods returned by the scroll controller', () => {
  const chatView = readFileSync(new URL('../ChatView.jsx', import.meta.url), 'utf8')
  const scrollController = readFileSync(new URL('../useScrollMode.js', import.meta.url), 'utf8')

  const useEnd = chatView.indexOf('} = useScrollMode({')
  const useStart = chatView.lastIndexOf('const {', useEnd)
  assert.ok(useStart >= 0 && useEnd > useStart, 'ChatView useScrollMode destructure exists')

  const returnStart = scrollController.lastIndexOf('\n  return {')
  const returnEnd = scrollController.indexOf('\n  }', returnStart)
  assert.ok(returnStart >= 0 && returnEnd > returnStart,
    'useScrollMode has a final returned controller object')

  const identifiers = source => [...source.matchAll(/^\s*([A-Za-z_$][\w$]*)\s*,?\s*$/gm)]
    .map(match => match[1])
  const consumed = identifiers(chatView.slice(useStart + 'const {'.length, useEnd))
  const returned = new Set(identifiers(scrollController.slice(returnStart, returnEnd)))
  const missing = consumed.filter(name => !returned.has(name))

  assert.deepEqual(missing, [],
    `ChatView consumes missing useScrollMode members: ${missing.join(', ')}`)
})

test('owner contract freezes question answers without locking keyboard movement', () => {
  const architecture = readFileSync(
    new URL('../../../../../ARCHITECTURE.md', import.meta.url),
    'utf8',
  )
  assert.match(architecture, /Owner-authoritative contract — v1\.20 \(2026-08-15\)/)
  assert.match(
    architecture,
    /In-process question is answered \| any \| transient `ANCHOR_AT` over the prior mode; same active assistant row/,
    'question submission must freeze the reader while preserving the R6 row',
  )
  assert.match(
    architecture,
    /question-submit hold is the sole calculation exception: it may reserve only the\s+exact tail deficit required for a stable card handoff while the viewport size is\s+unchanged/,
    'question submission may reserve only its same-viewport reachability deficit',
  )
  assert.match(
    architecture,
    /Viewport\/keyboard changes after question submission \| transient question anchor \| pre-submit unanswered-card mode/,
    'keyboard movement must return to the unanswered card baseline',
  )
  assert.match(
    architecture,
    /Focused Q&A custom answer grows or its keyboard viewport changes \| ordinary hold \| current caret-visible `ANCHOR_AT`/,
    'editing may adopt native caret movement without weakening stronger scroll modes',
  )
  assert.match(
    architecture,
    /One keyboard geometry signal; reservation-responsive resize/,
    'keyboard layout must flow from Shell into the actual chat scroll box once',
  )
})

test('an empty chat initializes scroll identity before its first transcript mounts', () => {
  const scrollController = readFileSync(new URL('../useScrollMode.js', import.meta.url), 'utf8')
  const layoutOwner = scrollController.indexOf('// Single layout effect:')
  const identityReset = scrollController.indexOf(
    'if (modeChatIdRef.current !== chatId)',
    layoutOwner,
  )
  const missingSurfaceReturn = scrollController.indexOf(
    'if (!scrollEl || !spacerEl) return',
    layoutOwner,
  )

  assert.ok(layoutOwner >= 0 && identityReset > layoutOwner)
  assert.ok(identityReset < missingSurfaceReturn,
    'the empty-state early return must not defer chat identity until after the first send arms its pin')
})

test('mirrored constants stay in sync with useScrollMode.js (sync obligation)', () => {
  // Read as TEXT, never import — importing useScrollMode.js would drag React
  // and its module-load sessionStorage read into this suite.
  const src = readFileSync(new URL('../useScrollMode.js', import.meta.url), 'utf8')
  assert.ok(
    src.includes(`PIN_OFFSET = ${PIN_OFFSET}`),
    `useScrollMode.js no longer declares PIN_OFFSET = ${PIN_OFFSET}. The value `
    + 'is mirrored in chatContract.js (see its header CONSTANTS-SYNC note) — '
    + 'update BOTH files together.',
  )
  assert.ok(
    src.includes(`PIN_BOTTOM_ROOM = ${PIN_BOTTOM_ROOM}`),
    `useScrollMode.js no longer declares PIN_BOTTOM_ROOM = ${PIN_BOTTOM_ROOM}. `
    + 'The value is mirrored in chatContract.js (see its header CONSTANTS-SYNC '
    + 'note) — update BOTH files together.',
  )
})

test('the transcript reveal safety deadline cannot be reset by message churn', () => {
  const src = readFileSync(new URL('../useScrollMode.js', import.meta.url), 'utf8')
  const deadline = src.indexOf('Absolute reveal deadline for this mounted chat')
  const messagesEffect = src.indexOf('Single layout effect: spacer sizing')
  assert.ok(deadline >= 0 && deadline < messagesEffect,
    'the safety deadline is owned outside the messages-dependent layout effect')
  assert.match(src.slice(deadline, messagesEffect), /\}, \[chatId\]\)/,
    'the safety deadline resets only when the mounted chat changes')
  assert.doesNotMatch(src.slice(messagesEffect), /clearTimeout\(safetyReveal\)/,
    'message/layout cleanup cannot cancel and restart the absolute deadline')
})
