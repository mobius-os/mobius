import { test } from 'node:test'
import assert from 'node:assert/strict'

import {
  composerFormattingMarker,
  isInlineEditorSubmit,
  isPlainTextPasteShortcut,
  linkPasteText,
  resolveComposerEnterAction,
  wrapComposerSelection,
} from '../composerShortcuts.js'
import { findShellShortcut, resolveShellCommands } from '../../../lib/keyboardShortcuts.js'
import { Marked } from 'marked'

const enter = (overrides = {}) => ({ key: 'Enter', ...overrides })

test('Cmd+Enter steers composer text when a live turn can accept it', () => {
  assert.equal(
    resolveComposerEnterAction(enter({ metaKey: true }), {
      hasInput: true,
      canSteer: true,
      canSubmitSteer: true,
      isTouchPrimary: false,
    }),
    'submit-steer',
  )
})

test('Ctrl+Enter steers composer text when a live turn can accept it', () => {
  assert.equal(
    resolveComposerEnterAction(enter({ ctrlKey: true }), {
      hasInput: true,
      canSteer: false,
      canSubmitSteer: true,
      isTouchPrimary: false,
    }),
    'submit-steer',
  )
})

test('Cmd/Ctrl+Enter submits normally when there is no steerable live turn', () => {
  assert.equal(
    resolveComposerEnterAction(enter({ metaKey: true }), {
      hasInput: true,
      canSubmitSteer: false,
      isTouchPrimary: false,
    }),
    'submit',
  )
})

test('Cmd/Ctrl+Enter steers when the composer is empty and steering is available', () => {
  assert.equal(
    resolveComposerEnterAction(enter({ metaKey: true }), {
      hasInput: false,
      canSteer: true,
      isTouchPrimary: false,
    }),
    'steer',
  )
  assert.equal(
    resolveComposerEnterAction(enter({ ctrlKey: true }), {
      hasInput: false,
      canSteer: true,
      isTouchPrimary: true,
    }),
    'steer',
  )
})

test('Cmd/Ctrl+Enter can request steer before the visible fast-forward gate is ready', () => {
  assert.equal(
    resolveComposerEnterAction(enter({ metaKey: true }), {
      hasInput: false,
      canSteer: false,
      canRequestSteer: true,
      isTouchPrimary: false,
    }),
    'steer',
  )
})

test('Cmd/Ctrl+Enter with no text and no steer affordance consumes the shortcut without acting', () => {
  assert.equal(
    resolveComposerEnterAction(enter({ metaKey: true }), {
      hasInput: false,
      canSteer: false,
      isTouchPrimary: false,
    }),
    'noop',
  )
})

test('plain Enter submits composer text on desktop', () => {
  assert.equal(
    resolveComposerEnterAction(enter(), {
      hasInput: true,
      isTouchPrimary: false,
    }),
    'submit',
  )
})

test('plain Enter steers queued text on desktop when the composer is empty', () => {
  assert.equal(
    resolveComposerEnterAction(enter(), {
      hasInput: false,
      canRequestSteer: true,
      isTouchPrimary: false,
    }),
    'steer',
  )
})

test('plain Enter remains a newline on touch-primary devices', () => {
  assert.equal(
    resolveComposerEnterAction(enter(), {
      hasInput: true,
      canRequestSteer: true,
      isTouchPrimary: true,
    }),
    null,
  )
})

test('Shift+Enter always stays a newline chord', () => {
  assert.equal(
    resolveComposerEnterAction(enter({ shiftKey: true, metaKey: true }), {
      hasInput: true,
      canSteer: true,
      isTouchPrimary: false,
    }),
    null,
  )
})

test('non-Enter keys do not trigger composer shortcuts', () => {
  assert.equal(
    resolveComposerEnterAction({ key: 'a', metaKey: true }, {
      hasInput: true,
      canSteer: true,
      isTouchPrimary: false,
    }),
    null,
  )
})

test('inline editor: plain Enter sends on desktop, matching the composer', () => {
  assert.equal(isInlineEditorSubmit(enter(), { isTouchPrimary: false }), true)
})

test('inline editor: plain Enter stays a newline on touch', () => {
  assert.equal(isInlineEditorSubmit(enter(), { isTouchPrimary: true }), false)
})

test('inline editor: Shift+Enter is always a newline', () => {
  assert.equal(isInlineEditorSubmit(enter({ shiftKey: true }), { isTouchPrimary: false }), false)
})

test('inline editor: Cmd/Ctrl+Enter always sends, even on touch', () => {
  assert.equal(isInlineEditorSubmit(enter({ metaKey: true }), { isTouchPrimary: true }), true)
  assert.equal(isInlineEditorSubmit(enter({ ctrlKey: true }), { isTouchPrimary: false }), true)
})

test('inline editor: non-Enter keys never send', () => {
  assert.equal(isInlineEditorSubmit({ key: 'a' }, { isTouchPrimary: false }), false)
})

test('Cmd/Ctrl+Shift+V requests paste without Markdown formatting', () => {
  assert.equal(isPlainTextPasteShortcut({ key: 'v', metaKey: true, shiftKey: true }), true)
  assert.equal(isPlainTextPasteShortcut({ key: 'V', ctrlKey: true, shiftKey: true }), true)
  assert.equal(isPlainTextPasteShortcut({ key: 'v', metaKey: true, shiftKey: false }), false)
  assert.equal(isPlainTextPasteShortcut({ key: 'v', shiftKey: true }), false)
})


const chord = (key, overrides = {}) => ({ key, metaKey: true, ...overrides })

test('Cmd/Ctrl+B, I and E ask for bold, italic and code in the chat box', () => {
  assert.equal(composerFormattingMarker(chord('b')), '**')
  assert.equal(composerFormattingMarker(chord('I')), '*')
  assert.equal(composerFormattingMarker({ key: 'e', ctrlKey: true }), '`')
  assert.equal(composerFormattingMarker({ key: 'b' }), null, 'a plain letter types itself')
})

test('formatting chords leave Shift, Alt, IME composition and shell-claimed keys alone', () => {
  assert.equal(composerFormattingMarker(chord('b', { shiftKey: true })), null)
  assert.equal(composerFormattingMarker(chord('b', { altKey: true })), null)
  assert.equal(composerFormattingMarker(chord('b', { isComposing: true })), null)
  assert.equal(composerFormattingMarker(chord('b', { nativeEvent: { isComposing: true } })), null)
  assert.equal(composerFormattingMarker(chord('b', { defaultPrevented: true })), null,
    'an owner-rebound shell shortcut claims the key first and must not also format')
})

test('Cmd+K still opens global search and never formats the chat box', () => {
  const commands = resolveShellCommands({})
  assert.equal(findShellShortcut(chord('k'), commands)?.id, 'search.open')
  assert.equal(findShellShortcut({ key: 'k', ctrlKey: true }, commands)?.id, 'search.open')
  assert.equal(composerFormattingMarker(chord('k')), null)
})

test('formatting chords do not collide with a default shell shortcut', () => {
  const commands = resolveShellCommands({})
  for (const key of ['b', 'i', 'e']) {
    assert.equal(findShellShortcut(chord(key), commands), null, `Cmd+${key.toUpperCase()}`)
  }
})

test('a formatting shortcut wraps the selection and keeps it selected', () => {
  const edit = wrapComposerSelection('say hello now', 4, 9, '**')
  assert.equal(edit.value, 'say **hello** now')
  assert.equal(edit.text, '**hello**')
  assert.equal(edit.value.slice(edit.selectionStart, edit.selectionEnd), 'hello')
})

test('a formatting shortcut with nothing selected inserts a selected placeholder', () => {
  for (const [marker, expected] of [['**', 'a**text**b'], ['*', 'a*text*b'], ['`', 'a`text`b']]) {
    const edit = wrapComposerSelection('ab', 1, 1, marker)
    assert.equal(edit.value, expected)
    assert.equal(edit.value.slice(edit.selectionStart, edit.selectionEnd), 'text',
      'typing replaces the placeholder')
  }
})

test('whitespace at the selection edges stays outside the markers', () => {
  const edit = wrapComposerSelection('x word y', 1, 7, '**')
  assert.equal(edit.value, 'x **word** y')
  assert.equal(edit.value.slice(edit.selectionStart, edit.selectionEnd), 'word')
})

test('code across several lines becomes a fenced block on its own lines', () => {
  const edit = wrapComposerSelection('see: a\nb then', 5, 8, '`')
  assert.equal(edit.value, 'see: \n```\na\nb\n```\n then')
  assert.equal(edit.value.slice(edit.selectionStart, edit.selectionEnd), 'a\nb')
  assert.equal(wrapComposerSelection('a\nb', 0, 3, '`').value, '```\na\nb\n```')
})

test('an indented multiline code selection keeps its indentation inside the fence', () => {
  // Hoisting `    if x:`'s spaces before the fence would change
  // whitespace-significant code relative to its shallower lines.
  const value = '    if x:\n        run()'
  const edit = wrapComposerSelection(value, 0, value.length, '`')
  assert.equal(edit.value, '```\n    if x:\n        run()\n```')
  assert.equal(edit.value.slice(edit.selectionStart, edit.selectionEnd), value)
  // Whole blank lines around the code still stay outside the fence.
  const padded = wrapComposerSelection('\n  a\nb\n\n', 0, 8, '`')
  assert.equal(padded.value, '\n```\n  a\nb\n```\n\n')
})

test('code formatting keeps backticks inside the selection in one code span', () => {
  const md = new Marked()
  for (const selected of ['run `npm test` now', '`x`', 'a ``b`` c']) {
    const edit = wrapComposerSelection(selected, 0, selected.length, '`')
    const [paragraph] = md.lexer(edit.value)
    assert.deepEqual(paragraph.tokens.map(token => [token.type, token.text]), [['codespan', selected]],
      `one code span for ${selected}`)
    assert.equal(edit.value.slice(edit.selectionStart, edit.selectionEnd), selected)
  }
})

test('code formatting around a selection that holds a fence uses a longer fence', () => {
  const selected = 'a\n```js\nx()\n```'
  const edit = wrapComposerSelection(`${selected}\nafter`, 0, selected.length, '`')
  const [block] = new Marked().lexer(edit.value)
  assert.equal(block.type, 'code')
  assert.equal(block.text, selected)
  assert.equal(edit.value.slice(edit.selectionStart, edit.selectionEnd), selected)
})

test('pasting a web address over selected text makes a Markdown link', () => {
  assert.equal(linkPasteText('the spec', 'https://example.com/spec'), '[the spec](https://example.com/spec)')
  assert.equal(linkPasteText(' the spec ', ' https://example.com '), ' [the spec](https://example.com) ')
  assert.equal(linkPasteText('see [1]', 'https://en.wikipedia.org/wiki/A_(b)'),
    '[see \\[1\\]](<https://en.wikipedia.org/wiki/A_(b)>)')
})

test('every other paste stays ordinary', () => {
  assert.equal(linkPasteText('', 'https://example.com'), null, 'nothing selected pastes the address')
  assert.equal(linkPasteText('https://old.example', 'https://new.example'), null,
    'an address over an address replaces it')
  assert.equal(linkPasteText('word', 'not an address'), null)
  assert.equal(linkPasteText('word', 'https://a.example and more'), null)
  assert.equal(linkPasteText('two\nlines', 'https://example.com'), null)
})

test('Enter in an empty box sends a held queue, and text still wins', () => {
  assert.equal(resolveComposerEnterAction(enter(), { canSendQueue: true }), 'send-queue')
  assert.equal(
    resolveComposerEnterAction(enter(), { hasInput: true, canSendQueue: true }),
    'submit',
  )
  assert.equal(resolveComposerEnterAction(enter(), {}), 'noop')
  assert.equal(
    resolveComposerEnterAction(enter(), { canSendQueue: true, isTouchPrimary: true }),
    null,
    'touch Enter stays a newline',
  )
})
