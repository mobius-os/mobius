// The shell's confirmation of an app's sensitive action wears the key cards'
// answer guard (useKeyAnswerGuard.js). Every press on the sheet restarts the
// guard's wait, so a confirm button that is really `disabled` while asleep
// switches itself off under the owner's own held press and the browser drops
// the click. Asleep is aria-disabled only; guard.counts decides the click.
import assert from 'node:assert/strict'
import { test } from 'node:test'
import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'

import OwnerConfirmSheet from '../../AppCanvas/OwnerConfirmSheet.jsx'

const preview = { headline: 'Post your prepared response on owner/repo#1', facts: [], verb: 'Post to GitHub', tone: 'caution' }
const render = request => renderToStaticMarkup(createElement(OwnerConfirmSheet, {
  appName: 'Contribute', request, onCancel: () => {}, onConfirm: () => {},
}))
const verbButton = (html, verb) => html.match(new RegExp(`<button[^>]*>${verb}</button>`))?.[0]

test('a ready sheet starts asleep without disabling its verb, so a held press keeps its click', () => {
  const button = verbButton(render({ phase: 'ready', preview }), 'Post to GitHub')
  assert.ok(button)
  assert.match(button, /aria-disabled="true"/)
  assert.doesNotMatch(button, /\sdisabled=""/)
})

test('while the server is checking or confirming, the verb is really disabled', () => {
  assert.match(verbButton(render({ phase: 'checking' }), 'Checking…'), /\sdisabled=""/)
  assert.match(verbButton(render({ phase: 'confirming', preview }), 'Confirming…'), /\sdisabled=""/)
})
