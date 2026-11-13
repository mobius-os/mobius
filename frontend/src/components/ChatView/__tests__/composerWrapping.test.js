/** Layout space follows the draft without becoming editable or sendable content. */
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { EditorState } from '@codemirror/state'
import { composerActionSpace } from '../composerWrapping.js'

function assertSpace(state, text) {
  const positions = []
  state.field(composerActionSpace).between(0, state.doc.length, (from, to, value) => {
    if (value.spec.widget) {
      positions.push([from, to])
      assert.equal(value.spec.side, 1)
    }
  })
  assert.deepEqual(positions, [[text.length, text.length]])
  assert.equal(state.doc.toString(), text)
}

test('empty, wrapped, Markdown and trailing-newline drafts have one space after the exact source', () => {
  for (const text of ['', 'short', 'a long message '.repeat(100), '**bold**\n', '\n\n']) {
    assertSpace(EditorState.create({ doc: text, extensions: [composerActionSpace] }), text)
  }
})
test('typing and deleting move only the presentation space, never modify the draft or selection', () => {
  let state = EditorState.create({ doc: '', extensions: [composerActionSpace] })
  state = state.update({ changes: { from: 0, insert: 'hello\nworld' }, selection: { anchor: 11 } }).state
  assertSpace(state, 'hello\nworld')
  assert.equal(state.selection.main.head, 11)
  state = state.update({ changes: { from: 0, to: 11 }, selection: { anchor: 0 } }).state
  assertSpace(state, '')
  assert.equal(state.selection.main.head, 0)
})

test('only an empty final source line uses padding instead of a caret-stretching inline box', () => {
  for (const text of ['', '\n', 'hello\n', 'hello', 'hello\nworld']) {
    const state = EditorState.create({ doc: text, extensions: [composerActionSpace] })
    const emptyTails = []
    state.field(composerActionSpace).between(0, text.length, (from, to, value) => {
      if (value.spec.class === 'cm-composer-empty-tail') emptyTails.push(from)
    })
    assert.deepEqual(emptyTails, !text || text.endsWith('\n') ? [text.length] : [])
  }
})
