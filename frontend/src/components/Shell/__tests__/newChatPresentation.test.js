import { test } from 'node:test'
import assert from 'node:assert/strict'

import {
  newChatPresentationBridgedKey,
  newChatPresentationSuperseded,
} from '../newChatPresentation.js'
import { EMPTY_SINGLE_SURFACE_KEY as EMPTY_SINGLE } from '../workspaceView.js'

const tapped = (originKey) => ({ chatId: null, originKey })
const allocated = (originKey, chatId) => ({ chatId, originKey })

test('no cover means nothing to supersede', () => {
  assert.equal(newChatPresentationBridgedKey(null), null)
  assert.equal(newChatPresentationSuperseded(null, 'chat:7'), false)
  assert.equal(newChatPresentationSuperseded(undefined, null), false)
})

test('the cover holds over its origin surface for the whole allocation', () => {
  const presentation = tapped('chat:7')
  assert.equal(newChatPresentationBridgedKey(presentation), 'chat:7')
  assert.equal(newChatPresentationSuperseded(presentation, 'chat:7'), false)
})

test('the cover holds over its destination once the row is allocated', () => {
  const presentation = allocated('chat:7', '9')
  assert.equal(newChatPresentationBridgedKey(presentation), 'chat:9')
  assert.equal(newChatPresentationSuperseded(presentation, 'chat:9'), false)
})

test('navigating away during allocation supersedes the cover', () => {
  const presentation = tapped('chat:7')
  // Another chat, an app, Apps, and the Settings takeover (which owns no
  // full-bleed key at all) are all somewhere the owner deliberately went.
  assert.equal(newChatPresentationSuperseded(presentation, 'chat:8'), true)
  assert.equal(newChatPresentationSuperseded(presentation, 'app:3'), true)
  assert.equal(newChatPresentationSuperseded(presentation, 'apps'), true)
  assert.equal(newChatPresentationSuperseded(presentation, null), true)
})

test('navigating away after allocation supersedes the cover before display-ready', () => {
  // The regression this guards: display-ready is the cover's only completion
  // signal and it belongs to chat 9, so leaving 9 first — even back to the
  // chat the tap came from — used to strand the landing over that surface.
  const presentation = allocated('chat:7', '9')
  assert.equal(newChatPresentationSuperseded(presentation, 'chat:7'), true)
  assert.equal(newChatPresentationSuperseded(presentation, 'chat:8'), true)
  assert.equal(newChatPresentationSuperseded(presentation, EMPTY_SINGLE), true)
})

test('a tap from the empty single slot bridges from that landing', () => {
  const presentation = tapped(EMPTY_SINGLE)
  assert.equal(newChatPresentationSuperseded(presentation, EMPTY_SINGLE), false)
  assert.equal(newChatPresentationSuperseded(presentation, 'chat:8'), true)
})

test('a keyless origin is superseded only by a real surface', () => {
  // A tap taken under the Settings takeover has no full-bleed key to bridge
  // from; absence must compare as absence rather than supersede itself.
  const presentation = tapped(null)
  assert.equal(newChatPresentationSuperseded(presentation, null), false)
  assert.equal(newChatPresentationSuperseded(presentation, undefined), false)
  assert.equal(newChatPresentationSuperseded(presentation, 'chat:8'), true)
})

test('chat ids compare as the surface keys the workspace publishes', () => {
  // Shell stores the resolved id as a string; the painted key is built from
  // the same value, so numeric ids must not read as a supersession.
  assert.equal(newChatPresentationSuperseded(allocated('chat:7', '9'), 'chat:9'), false)
  assert.equal(newChatPresentationBridgedKey(allocated(null, 9)), 'chat:9')
})
