/* Exact steer replay renders continuously without guessing or data loss. */

import test from 'node:test'
import assert from 'node:assert/strict'
import {
  projectSettledSteerContinuations,
  projectSteerContinuationMessage,
  projectActiveSteerPrefix,
  sealedAssistantBeforeSteer,
} from '../steerContinuity.js'
import { safeSteerMarkdownCut } from '../markdown/steerContinuation.js'

test('active formatting updates preserve unrelated settled reply identities', () => {
  const unrelated = [assistant('**Old pre', { id: 'old' }), steer(),
    assistant('**Old previous answer**', { id: 'old:assistant:1' })]
  const current = [assistant('**3. Don', { id: 'run' }), steer(),
    assistant('**3. Don’t', { id: 'run:assistant:1' }), steer()]
  const settled = projectSettledSteerContinuations([...unrelated, ...current])
  const original = JSON.stringify(settled)
  let prior = settled
  for (const text of ['**3. Don’t continue**', '**3. Don’t continue further**']) {
    const continuation = projectSteerContinuationMessage(current[2],
      assistant(text, { id: 'run:assistant:2' }), { active: true })
    const updated = projectActiveSteerPrefix(settled, { id: current[2].id, continuation })
    for (let index = 0; index < unrelated.length; index++) {
      assert.equal(updated[index], settled[index])
      assert.equal(updated[index], prior[index])
    }
    assert.equal(updated[3].blocks[0].markdown_range.source, text)
    assert.equal(updated[5].blocks[0].markdown_range.source, text)
    prior = updated
  }
  assert.equal(JSON.stringify(settled), original)
  assert.equal(projectActiveSteerPrefix(settled, null), settled)
})


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


function peerSteer(content = 'new collaborator context') {
  return {
    role: 'user',
    content,
    steered: true,
    hidden: true,
    kind: 'peer_message',
    cid: 'peer-steer:message-id',
  }
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


test('multiple sealed text blocks are never joined across activity', () => {
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


test('a repeated steer trims only the exact terminal text section', () => {
  const sealed = {
    role: 'assistant',
    content: 'Earlier answer\n\nLatest section',
    blocks: [
      { type: 'text', content: 'Earlier answer' },
      { type: 'tool', tool: 'Bash', status: 'done', output: 'ok' },
      { type: 'text', content: 'Latest section' },
    ],
  }
  const thinking = { type: 'thinking', content: 'Continuing' }
  const tool = { type: 'tool', tool: 'Bash', status: 'done', output: 'next' }
  const later = { type: 'text', content: 'Later result' }
  const continuation = {
    role: 'assistant',
    content: 'Latest section continues.\n\nLater result',
    blocks: [
      thinking,
      { type: 'text', content: 'Latest section continues.', source_text_offset: 4 },
      tool,
      later,
    ],
  }
  const before = JSON.stringify({ sealed, continuation })

  const projected = projectSteerContinuationMessage(sealed, continuation)

  assert.equal(projected.blocks[1].content, ' continues.')
  assert.equal(projected.blocks[1].source_text_offset, 4 + 'Latest section'.length)
  assert.equal(projected.blocks[0], thinking)
  assert.equal(projected.blocks[2], tool)
  assert.equal(projected.blocks[3], later)
  assert.equal(JSON.stringify({ sealed, continuation }), before,
    'stored messages and activity are never rewritten')
})


test('a chain of multi-section replies continues from each raw terminal section', () => {
  const first = assistant('First section')
  const second = assistant('First section continued.\n\nSecond section', {
    blocks: [
      { type: 'text', content: 'First section continued.' },
      { type: 'text', content: 'Second section' },
    ],
  })
  const third = assistant('Second section continued.\n\nThird section', {
    blocks: [
      { type: 'text', content: 'Second section continued.' },
      { type: 'text', content: 'Third section' },
    ],
  })

  const displayed = projectSettledSteerContinuations([
    first, steer('one'), second, steer('two'), third,
  ])

  assert.equal(displayed[2].blocks[0].content, ' continued.')
  assert.equal(displayed[2].blocks[1], second.blocks[1])
  assert.equal(displayed[4].blocks[0].content, ' continued.')
  assert.equal(displayed[4].blocks[1], third.blocks[1])
})


test('a terminal-section replay hides live partials but reveals divergence and settled short text', () => {
  const sealed = assistant('Earlier\n\nLatest section', {
    blocks: [
      { type: 'text', content: 'Earlier' },
      { type: 'text', content: 'Latest section' },
    ],
  })
  const partial = assistant('Latest')
  const mismatch = assistant('Latest change')
  const earlier = assistant('Earlier continued')

  assert.equal(projectSteerContinuationMessage(sealed, partial, { active: true }).blocks[0].content, '')
  assert.equal(projectSteerContinuationMessage(sealed, partial), partial)
  assert.equal(projectSteerContinuationMessage(sealed, mismatch, { active: true }), mismatch)
  assert.equal(projectSteerContinuationMessage(sealed, earlier), earlier)
})


test('terminal-section replay carries formatting context without hiding real content', () => {
  const sealed = assistant('Earlier\n\n**Plan', {
    blocks: [
      { type: 'text', content: 'Earlier' },
      { type: 'text', content: '**Plan' },
    ],
  })
  const continuation = assistant('**Planned** maintenance')

  const projected = projectSteerContinuationMessage(sealed, continuation)
  assert.equal(projected.blocks[0].content, 'ned** maintenance')
  assert.ok(projected.blocks[0].markdown_range)
  assert.equal(continuation.blocks[0].content, '**Planned** maintenance')
})


test('a sealed tool or question boundary prevents reaching back to earlier text', () => {
  for (const boundary of [
    { type: 'tool', tool: 'Bash', status: 'done', output: 'ok' },
    { type: 'question', question: 'Choose one' },
    { type: 'text', content: '' },
  ]) {
    const sealed = assistant('Answer', {
      blocks: [{ type: 'text', content: 'Answer' }, boundary],
    })
    const continuation = assistant('Answer continued.')

    assert.equal(projectSteerContinuationMessage(sealed, continuation), continuation)
  }
})


test('trailing thinking remains neutral when selecting the terminal text section', () => {
  const sealed = assistant('Earlier\n\nLatest section', {
    blocks: [
      { type: 'text', content: 'Earlier' },
      { type: 'text', content: 'Latest section' },
      { type: 'thinking', content: 'Replanning' },
    ],
  })

  assert.equal(projectSteerContinuationMessage(sealed,
    assistant('Latest section continued.')).blocks[0].content, ' continued.')
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


test('a hidden collaborator steer does not repaint an exact assistant prefix', () => {
  const messages = [
    assistant('I won\u2019t disable safeguards or force unsafe merges'),
    peerSteer(),
    assistant('I won\u2019t disable safeguards or force unsafe merges. Continuing safely.'),
  ]

  const displayed = projectSettledSteerContinuations(messages)

  assert.equal(displayed[0].blocks[0].content,
    'I won\u2019t disable safeguards or force unsafe merges')
  assert.equal(displayed[2].blocks[0].content, '. Continuing safely.')
  assert.equal(displayed[1], messages[1], 'the internal provider row stays hidden')
})


test('a live collaborator continuation stays hidden while replaying the sealed prefix', () => {
  const messages = [
    assistant('The guarded path remains active.'),
    peerSteer(),
  ]
  const sealed = sealedAssistantBeforeSteer(messages, messages.length)
  const replaying = assistant('The guarded path')

  const displayed = projectSteerContinuationMessage(
    sealed,
    replaying,
    { active: true },
  )

  assert.equal(displayed.blocks[0].content, '')
  assert.equal(replaying.blocks[0].content, 'The guarded path')
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


test('a formatted split preserves the parsed context instead of replaying its prefix', () => {
  const sealed = assistant('**Plan')
  const continuation = assistant('**Planned** maintenance')
  const projected = projectSteerContinuationMessage(sealed, continuation)
  assert.equal(projected.blocks[0].content, 'ned** maintenance')
  assert.equal(projected.blocks[0].source_text_offset, '**Plan'.length)
  assert.ok(projected.blocks[0].markdown_range)
  assert.ok(projected.steer_replay.prefixRange)
  assert.equal(continuation.content, '**Planned** maintenance')
})


test('unmappable code, links and math still preserve the complete response', () => {
  for (const [prefix, text] of [
    ['```js\nconst', '```js\nconst x = 1\n```'],
    ['[see](https://exa', '[see](https://example.com)'],
    ['$x', '$x + y$'],
  ]) {
    const continuation = assistant(text)
    assert.equal(projectSteerContinuationMessage(assistant(prefix), continuation), continuation)
  }
})

test('an unfinished emphasis replay remains lossless while catching up to the sealed text', () => {
  const sealed = assistant('**Plan')
  for (const [text, expected] of [['**Pl', ''], ['**Plan', ''], ['**Planned', 'ned']]) {
    const projected = projectSteerContinuationMessage(sealed, assistant(text), { active: true })
    assert.equal(projected.content, expected)
  }
  const shorter = assistant('**Pl')
  assert.equal(projectSteerContinuationMessage(sealed, shorter), shorter)
})
