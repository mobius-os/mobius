import { test } from 'node:test'
import assert from 'node:assert/strict'

import {
  composerHistoryNativeProbe,
  composerHistoryProbeReachedBoundary,
  composerHistoryFromMessages,
  resolveComposerHistoryMove,
} from '../composerHistory.js'

function key(keyName, {
  selectionStart = 0,
  selectionEnd = selectionStart,
  ...overrides
} = {}) {
  return {
    key: keyName,
    target: { selectionStart, selectionEnd },
    ...overrides,
  }
}

test('history contains only visible owner-authored text', () => {
  assert.deepEqual(composerHistoryFromMessages([
    { role: 'user', content: 'first' },
    { role: 'assistant', content: 'reply' },
    { role: 'user', content: 'hidden', hidden: true },
    { role: 'user', content: 'continue', kind: 'auto_continuation' },
    { role: 'user', content: '   ' },
    {
      role: 'user',
      content: [
        'review this',
        '',
        '[Files in this session:',
        '- brief.txt → /data/chats/private/uploads/brief.txt (text/plain, 1 KB)]',
      ].join('\n'),
    },
    { role: 'user', content: 'second' },
  ]), ['first', 'review this', 'second'])
})

test('Up walks older sent messages and Down returns toward newer ones', () => {
  const history = ['first', 'second', 'third']
  let state = resolveComposerHistoryMove(key('ArrowUp'), {
    history,
    value: '',
  })
  assert.deepEqual(state, { value: 'third', index: 2, draft: '' })

  state = resolveComposerHistoryMove(key('ArrowUp'), {
    history,
    ...state,
  })
  assert.deepEqual(state, { value: 'second', index: 1, draft: '' })

  state = resolveComposerHistoryMove(key('ArrowDown'), {
    history,
    ...state,
  })
  assert.deepEqual(state, { value: 'third', index: 2, draft: '' })
})

test('Down past the newest message restores an exact multiline draft', () => {
  const draft = 'unfinished first line\nand the second line'
  const history = ['previous message']
  const recalled = resolveComposerHistoryMove(key('ArrowUp', {
    selectionStart: 5,
  }), {
    history,
    value: draft,
    nativeBoundary: true,
  })
  assert.deepEqual(recalled, {
    value: 'previous message',
    index: 0,
    draft,
  })

  assert.deepEqual(resolveComposerHistoryMove(key('ArrowDown'), {
    history,
    ...recalled,
  }), {
    value: draft,
    index: null,
    draft: '',
  })
})

test('non-empty drafts leave the first ArrowUp to native visual-line movement', () => {
  const value = 'a long visually wrapped draft without source newlines'
  assert.equal(resolveComposerHistoryMove(key('ArrowUp', {
    selectionStart: value.length,
  }), {
    history: ['previous'],
    value,
  }), null)
})

test('a native probe recalls history only when ArrowUp leaves the caret unchanged', () => {
  const target = {
    value: 'visually wrapped draft',
    selectionStart: 18,
    selectionEnd: 18,
  }
  const probe = composerHistoryNativeProbe({
    key: 'ArrowUp',
    target,
  }, {
    history: ['previous'],
    value: target.value,
  })
  assert.ok(probe)
  assert.equal(composerHistoryProbeReachedBoundary(probe, target), true)

  target.selectionStart = 6
  target.selectionEnd = 6
  assert.equal(composerHistoryProbeReachedBoundary(probe, target), false)
})

test('selection, modifier chords, and IME composition remain native', () => {
  const options = { history: ['previous'], value: 'draft' }
  assert.equal(resolveComposerHistoryMove(key('ArrowUp', {
    selectionStart: 0,
    selectionEnd: 2,
  }), options), null)
  assert.equal(resolveComposerHistoryMove(key('ArrowUp', {
    ctrlKey: true,
  }), options), null)
  assert.equal(resolveComposerHistoryMove(key('ArrowUp', {
    isComposing: true,
  }), options), null)
})

test('Down does nothing until history browsing has started', () => {
  assert.equal(resolveComposerHistoryMove(key('ArrowDown'), {
    history: ['previous'],
    value: 'draft',
  }), null)
})
