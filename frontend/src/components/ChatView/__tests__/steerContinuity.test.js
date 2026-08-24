/* Exact steer replay renders continuously without guessing or data loss. */

import test from 'node:test'
import assert from 'node:assert/strict'
import {
  projectSettledSteerContinuations,
  projectSteerContinuationMessage,
  sealedAssistantBeforeSteer,
} from '../steerContinuity.js'
import { safeSteerMarkdownCut } from '../markdown/steerContinuation.js'


function assistant(text, extras = {}) {
  return {
    role: 'assistant',
    content: text,
    blocks: [{ type: 'text', content: text }],
    ...extras,
  }
}


function steer(content = 'change course') {
  return { role: 'user', content, steered: true }
}


test('an exact post-steer replay renders only its unseen suffix', () => {
  const sealed = assistant('The key is')
  const continuation = assistant('The key is preserving the boundary.')

  const projected = projectSteerContinuationMessage(sealed, continuation)

  assert.equal(projected.blocks[0].content, ' preserving the boundary.')
  assert.equal(projected.content, ' preserving the boundary.')
  assert.equal(continuation.blocks[0].content,
    'The key is preserving the boundary.', 'durable source stays untouched')
})


test('a plain word may continue across the steered user row', () => {
  const messages = [
    assistant('The frame'),
    steer('also cover reconnects'),
    assistant('The framework survives reconnects.'),
  ]

  const displayed = projectSettledSteerContinuations(messages)

  assert.equal(displayed[0].blocks[0].content, 'The frame')
  assert.equal(displayed[1], messages[1])
  assert.equal(displayed[2].blocks[0].content, 'work survives reconnects.')
})


test('a normal user row never authorizes prefix suppression', () => {
  const messages = [
    assistant('Repeat this'),
    { role: 'user', content: 'say it again' },
    assistant('Repeat this exactly.'),
  ]

  assert.deepEqual(projectSettledSteerContinuations(messages), messages)
})


test('a mismatch and a settled short response both fail closed', () => {
  const sealed = assistant('Planned maintenance preserves active work.')
  const mismatch = assistant('Unexpected crashes remain conservative.')
  const shorter = assistant('Planned maintenance')

  assert.equal(projectSteerContinuationMessage(sealed, mismatch), mismatch)
  assert.equal(projectSteerContinuationMessage(sealed, shorter), shorter)
})


test('a matching live partial stays hidden until it catches up', () => {
  const sealed = assistant('Planned maintenance preserves active work.')
  const partial = assistant('Planned maintenance')
  const projected = projectSteerContinuationMessage(
    sealed,
    partial,
    { active: true },
  )

  assert.equal(projected.blocks[0].content, '')
  assert.equal(projected.content, '')
  assert.equal(partial.blocks[0].content, 'Planned maintenance')
})


test('a live partial reveals its complete text on the first divergence', () => {
  const sealed = assistant('Planned maintenance preserves active work.')
  const diverged = assistant('Planned replacement')

  assert.equal(
    projectSteerContinuationMessage(sealed, diverged, { active: true }),
    diverged,
  )
})


test('thinking is preserved while only the first continuation text is trimmed', () => {
  const thinking = { type: 'thinking', content: 'Replanning', thinking_id: 't2' }
  const tool = { type: 'tool', tool: 'Bash', status: 'done', output: 'ok' }
  const continuation = {
    role: 'assistant',
    content: 'Answer continued.',
    blocks: [
      thinking,
      { type: 'text', content: 'Answer continued.' },
      tool,
      { type: 'text', content: 'New result.' },
    ],
  }

  const projected = projectSteerContinuationMessage(
    assistant('Answer'),
    continuation,
  )

  assert.equal(projected.blocks[0], thinking)
  assert.equal(projected.blocks[1].content, ' continued.')
  assert.equal(projected.blocks[2], tool)
  assert.equal(projected.blocks[3].content, 'New result.')
})


test('a tool before the continuation text fails closed', () => {
  const continuation = {
    role: 'assistant',
    content: 'Answer continued.',
    blocks: [
      { type: 'tool', tool: 'Bash', status: 'done', output: 'ok' },
      { type: 'text', content: 'Answer continued.' },
    ],
  }

  assert.equal(
    projectSteerContinuationMessage(assistant('Answer'), continuation),
    continuation,
  )
})


test('multiple sealed text blocks fail closed instead of crossing activity', () => {
  const sealed = {
    role: 'assistant',
    content: 'First\n\nSecond',
    blocks: [
      { type: 'text', content: 'First' },
      { type: 'thinking', content: 'Between' },
      { type: 'text', content: 'Second' },
    ],
  }
  const continuation = assistant('First\n\nSecond plus more')

  assert.equal(projectSteerContinuationMessage(sealed, continuation), continuation)
})


test('consecutive steered rows share the same sealed assistant', () => {
  const messages = [
    assistant('Prefix'),
    steer('first steer'),
    steer('second steer'),
    assistant('Prefix suffix'),
  ]

  assert.equal(sealedAssistantBeforeSteer(messages, 3), messages[0])
  assert.equal(
    projectSettledSteerContinuations(messages)[3].blocks[0].content,
    ' suffix',
  )
})


test('each steer in a chain compares against the raw preceding response', () => {
  const messages = [
    assistant('A'),
    steer('one'),
    assistant('AB'),
    steer('two'),
    assistant('ABC'),
  ]

  const displayed = projectSettledSteerContinuations(messages)
  assert.equal(displayed[2].blocks[0].content, 'B')
  assert.equal(displayed[4].blocks[0].content, 'C')
})


test('Markdown cuts allow plain words and complete constructs only', () => {
  assert.equal(safeSteerMarkdownCut('The framework continues', 9), true)
  assert.equal(safeSteerMarkdownCut('**Planned', 6), false)
  assert.equal(safeSteerMarkdownCut('**Planned', '**Planned'.length), false)
  assert.equal(safeSteerMarkdownCut('**Planned**', '**Planned**'.length), true)
  assert.equal(safeSteerMarkdownCut('**Planned** maintenance', 6), false)
  assert.equal(safeSteerMarkdownCut('**Planned** maintenance', 11), true)
  assert.equal(safeSteerMarkdownCut('Read https://exa', 'Read https://'.length), false)
  assert.equal(safeSteerMarkdownCut('Read https://exa', 'Read https://exa'.length), false)
  assert.equal(
    safeSteerMarkdownCut(
      '[Read](https://example.com)',
      '[Read](https://example.com)'.length,
    ),
    true,
  )
  assert.equal(safeSteerMarkdownCut('- framework continues', 7), false)
  assert.equal(safeSteerMarkdownCut('```js\nconst x = 1\n```', 12), false)
})


test('plain-text cuts preserve graphemes and character references', () => {
  assert.equal(safeSteerMarkdownCut('woman 👩‍💻 works', 'woman 👩'.length), false)
  assert.equal(safeSteerMarkdownCut('cafe\u0301 continues', 'cafe'.length), false)
  assert.equal(safeSteerMarkdownCut('A &amp; B', 'A &'.length), false)
  assert.equal(safeSteerMarkdownCut('A &amp; B', 'A &amp;'.length), true)
  assert.equal(safeSteerMarkdownCut('A &amp', 'A &amp'.length), false)
  assert.equal(safeSteerMarkdownCut('The framework continues', 'The frame'.length), true)
})


test('an unsafe Markdown split keeps the full post-steer response', () => {
  const sealed = assistant('**Plan')
  const continuation = assistant('**Planned** maintenance')
  const activePartial = assistant('**Planned')
  const activeExact = assistant('**Plan')

  assert.equal(
    projectSteerContinuationMessage(sealed, continuation),
    continuation,
  )
  assert.equal(
    projectSteerContinuationMessage(sealed, activePartial, { active: true }),
    activePartial,
  )
  assert.equal(
    projectSteerContinuationMessage(sealed, activeExact, { active: true }),
    activeExact,
  )
})
