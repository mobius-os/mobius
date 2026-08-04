import test from 'node:test'
import assert from 'node:assert/strict'

import {
  BUILDER_CHAT_WORLD,
  FOCUSED_BUILDER_CHAT_SURFACE,
  STANDARD_CHAT_WORLD,
  chatSurfaceKey,
  deriveChatSurfaceLayers,
  deriveChatSurfaceOwners,
} from '../chatSurfaceModel.js'
import { SINGLE_SLOT_PANE } from '../paneModel.js'

function workspace({ slot = 'a', left = 'a', right = 'b' } = {}) {
  return {
    singleScreen: slot == null ? null : { kind: 'chat', id: slot },
    panes: {
      left: {
        activeTabKey: `chat:${left}`,
        tabs: [{ kind: 'chat', id: left }],
      },
      right: {
        activeTabKey: `chat:${right}`,
        tabs: [{ kind: 'chat', id: right }],
      },
    },
  }
}

const projection = { visibleLeaves: ['left', 'right'] }

function focusedBuilderLayers(presentedFocusedId) {
  const owners = deriveChatSurfaceOwners({
    workspace: workspace({ slot: 'a', left: 'a', right: 'b' }),
    baseProjection: projection,
    projection: {
      visibleLeaves: ['right'],
      focusedPaneView: true,
    },
  })
  return deriveChatSurfaceLayers(owners, new Map([
    ['left', 'a'],
    ['right', 'b'],
    [FOCUSED_BUILDER_CHAT_SURFACE, presentedFocusedId],
  ]), {
    focusedBuilderPaneId: 'right',
  })
}

test('a legacy absent slot mounts NO Standard ChatView — it never borrows Builder focus', () => {
  const legacy = workspace()
  legacy.focusedPaneId = 'right'
  delete legacy.singleScreen

  const owners = deriveChatSurfaceOwners({
    workspace: legacy,
    baseProjection: projection,
    projection,
  })

  // Two-worlds: an uninitialized Standard is the empty home, so it retains no
  // Standard-world ChatView (it never borrows the focused Builder chat).
  assert.equal(
    owners.some(owner => (
      owner.world === STANDARD_CHAT_WORLD && owner.paneId === SINGLE_SLOT_PANE
    )),
    false,
  )
})

test('an explicit null slot remains the New Chat landing', () => {
  const owners = deriveChatSurfaceOwners({
    workspace: workspace({ slot: null }),
    baseProjection: projection,
    projection,
  })

  assert.equal(
    owners.some(owner => owner.world === STANDARD_CHAT_WORLD),
    false,
  )
})

test('the same chat gets independent Standard and Builder surface owners', () => {
  const owners = deriveChatSurfaceOwners({
    workspace: workspace(),
    baseProjection: projection,
    projection,
  })

  const chatA = owners.filter(owner => String(owner.chatId) === 'a')
  assert.equal(chatA.length, 2)
  assert.deepEqual(
    new Set(chatA.map(owner => owner.world)),
    new Set([STANDARD_CHAT_WORLD, BUILDER_CHAT_WORLD]),
  )
  assert.equal(
    chatA.find(owner => owner.world === STANDARD_CHAT_WORLD).paneId,
    SINGLE_SLOT_PANE,
  )
  assert.notEqual(chatA[0].surfaceKey, chatA[1].surfaceKey)
})

test('a Builder chat keeps its surface key when its tab moves panes', () => {
  const before = deriveChatSurfaceOwners({
    workspace: workspace(),
    baseProjection: projection,
    projection,
  }).find(owner => owner.world === BUILDER_CHAT_WORLD && owner.chatId === 'a')

  const movedWorkspace = workspace({ left: 'b', right: 'a' })
  const after = deriveChatSurfaceOwners({
    workspace: movedWorkspace,
    baseProjection: projection,
    projection,
  }).find(owner => owner.world === BUILDER_CHAT_WORLD && owner.chatId === 'a')

  assert.equal(before.surfaceKey, chatSurfaceKey(BUILDER_CHAT_WORLD, 'a'))
  assert.equal(after.surfaceKey, before.surfaceKey)
  assert.notEqual(after.paneId, before.paneId)
})

test('a Standard duplicate does not suppress Builder chat handoff coverage', () => {
  const owners = deriveChatSurfaceOwners({
    workspace: workspace({ slot: 'a', left: 'b', right: 'c' }),
    baseProjection: projection,
    projection,
  })
  const presented = new Map([['left', 'a']])
  const layers = deriveChatSurfaceLayers(owners, presented)

  assert.ok(layers.some(layer => (
    layer.world === BUILDER_CHAT_WORLD
      && layer.chatId === 'a'
      && layer.role === 'held'
  )))
  assert.ok(layers.some(layer => (
    layer.world === STANDARD_CHAT_WORLD
      && layer.chatId === 'a'
      && layer.role === 'active'
  )))
})

test('focused Builder presentation holds the outgoing chat across pane focus changes', () => {
  const layers = focusedBuilderLayers('a')

  const outgoing = layers.filter(layer => (
    layer.world === BUILDER_CHAT_WORLD && layer.chatId === 'a'
  ))
  assert.equal(outgoing.length, 1, 'the retained ChatView is moved, never duplicated')
  assert.equal(outgoing[0].role, 'held')
  assert.equal(outgoing[0].paneId, 'left', 'runtime ownership stays with its pane')
  assert.equal(outgoing[0].presentationPaneId, 'right',
    'the cover occupies the newly focused presentation rectangle')

  const incoming = layers.find(layer => (
    layer.world === BUILDER_CHAT_WORLD && layer.chatId === 'b'
  ))
  assert.equal(incoming.role, 'staging')
  assert.ok(layers.some(layer => (
    layer.world === STANDARD_CHAT_WORLD
      && layer.chatId === 'a'
      && layer.role === 'active'
  )), 'the parked Standard duplicate remains independent')
})

test('focused Builder presentation releases the cover after destination readiness', () => {
  const layers = focusedBuilderLayers('b')

  assert.equal(layers.some(layer => layer.role !== 'active'), false)
  assert.equal(layers.some(layer => layer.presentationPaneId), false)
})

test('focused Builder presentation never manufactures a stale outgoing cover', () => {
  const layers = focusedBuilderLayers('departed')

  assert.equal(layers.some(layer => layer.chatId === 'departed'), false)
  assert.equal(layers.every(layer => layer.role === 'active'), true)
})
