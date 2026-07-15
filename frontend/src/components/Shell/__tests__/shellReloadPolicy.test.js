import { test } from 'node:test'
import assert from 'node:assert/strict'

import {
  hasActiveChatTurn,
  hasProtectedEditingContent,
  isTextEditingElement,
  shouldDeferShellReload,
} from '../shellReloadPolicy.js'

const el = (tagName, props = {}) => ({ tagName, ...props })

test('text-editing elements defer shell reloads', () => {
  assert.equal(isTextEditingElement(el('textarea')), true)
  assert.equal(isTextEditingElement(el('input', { type: 'text' })), true)
  assert.equal(isTextEditingElement(el('input', { type: 'checkbox' })), false)
  assert.equal(isTextEditingElement(el('iframe')), true)
  assert.equal(isTextEditingElement(el('div', { isContentEditable: true })), true)
})

test('any active chat turn defers shell reloads', () => {
  assert.equal(hasActiveChatTurn({
    activeView: 'chat',
    activeChatId: 'c1',
    streamingChatIds: new Set(['c1']),
  }), true)
  assert.equal(hasActiveChatTurn({
    activeView: 'chat',
    activeChatId: 'c2',
    streamingChatIds: new Set(['c1']),
  }), true)
  assert.equal(hasActiveChatTurn({
    activeView: 'canvas',
    activeChatId: 'c1',
    streamingChatIds: new Set(['c1']),
  }), true)
  assert.equal(hasActiveChatTurn({
    activeView: 'chat',
    activeChatId: 'c1',
    streamingChatIds: new Set(),
  }), false)
})

test('recent interaction defers visible shell reloads', () => {
  assert.equal(shouldDeferShellReload({
    activeElement: el('body'),
    activeView: 'chat',
    activeChatId: 'c1',
    streamingChatIds: new Set(),
    lastUserInteractionAt: 9000,
    now: 10000,
  }), true)
  assert.equal(shouldDeferShellReload({
    activeElement: el('body'),
    activeView: 'chat',
    activeChatId: 'c1',
    streamingChatIds: new Set(),
    lastUserInteractionAt: 7000,
    now: 10000,
  }), false)
})

test('passive watcher rebuilds coalesce while an idle chat is visible', () => {
  const base = {
    activeElement: el('body'),
    activeView: 'chat',
    activeChatId: 'c1',
    streamingChatIds: new Set(),
    lastUserInteractionAt: 0,
    now: 10000,
  }
  assert.equal(shouldDeferShellReload({
    ...base,
    passiveRebuild: true,
  }), true)
  assert.equal(shouldDeferShellReload({
    ...base,
    passiveRebuild: false,
  }), false, 'a deliberate apply-now still uses normal idle policy')
  assert.equal(shouldDeferShellReload({
    ...base,
    activeView: 'canvas',
    passiveRebuild: true,
  }), false, 'leaving the chat releases a queued watcher rebuild')
  assert.equal(shouldDeferShellReload({
    ...base,
    passiveRebuild: true,
    visibilityState: 'hidden',
  }), false, 'a hidden page is a safe apply boundary')
})

test('hidden pages can reload without disrupting focus', () => {
  assert.equal(shouldDeferShellReload({
    activeElement: el('textarea'),
    visibilityState: 'hidden',
    lastUserInteractionAt: 10000,
    now: 10001,
  }), false)
})

test('only editors holding content block an apply-on-idle reload', () => {
  // The regression case: an EMPTY composer that kept focus after send is idle,
  // not "composing" — a reload would destroy nothing.
  assert.equal(hasProtectedEditingContent(el('textarea', { value: '' })), false)
  assert.equal(hasProtectedEditingContent(el('input', { type: 'text', value: '' })), false)
  assert.equal(hasProtectedEditingContent(el('div', { isContentEditable: true, textContent: '' })), false)
  // A draft in progress IS protected — a reload mid-typing would lose it.
  assert.equal(hasProtectedEditingContent(el('textarea', { value: 'half typed' })), true)
  assert.equal(hasProtectedEditingContent(el('input', { type: 'text', value: 'x' })), true)
  assert.equal(hasProtectedEditingContent(el('div', { isContentEditable: true, textContent: 'note' })), true)
  // Opaque / non-text focus targets stay protected regardless.
  assert.equal(hasProtectedEditingContent(el('iframe')), true)
  assert.equal(hasProtectedEditingContent(el('select')), true)
  // Not an editing surface at all — nothing to protect.
  assert.equal(hasProtectedEditingContent(el('body')), false)
  assert.equal(hasProtectedEditingContent(el('input', { type: 'checkbox' })), false)
})

test('a focused but empty composer is idle enough to apply at turn-end', () => {
  // Exactly the shell-update-idle case 2: turn is done (streaming set empty),
  // the composer keeps focus on desktop but is empty → do NOT defer.
  assert.equal(shouldDeferShellReload({
    activeElement: el('textarea', { value: '' }),
    activeView: 'chat',
    activeChatId: 'c1',
    streamingChatIds: new Set(),
    lastUserInteractionAt: 0,
    now: 10000,
  }), false)
  // A non-empty composer still defers (protect the draft).
  assert.equal(shouldDeferShellReload({
    activeElement: el('textarea', { value: 'draft' }),
    activeView: 'chat',
    activeChatId: 'c1',
    streamingChatIds: new Set(),
    lastUserInteractionAt: 0,
    now: 10000,
  }), true)
  // A live turn still defers even with an empty composer (never during stream).
  assert.equal(shouldDeferShellReload({
    activeElement: el('textarea', { value: '' }),
    activeView: 'chat',
    activeChatId: 'c1',
    streamingChatIds: new Set(['c1']),
    lastUserInteractionAt: 0,
    now: 10000,
  }), true)
})

test('voice dictation defers shell reloads even with an empty composer', () => {
  assert.equal(shouldDeferShellReload({
    activeElement: el('textarea', { value: '' }),
    activeView: 'chat',
    activeChatId: 'c1',
    streamingChatIds: new Set(),
    voiceDictationActive: true,
    lastUserInteractionAt: 0,
    now: 10000,
  }), true)
  // Idle mic → no deferral on that account.
  assert.equal(shouldDeferShellReload({
    activeElement: el('textarea', { value: '' }),
    activeView: 'chat',
    activeChatId: 'c1',
    streamingChatIds: new Set(),
    voiceDictationActive: false,
    lastUserInteractionAt: 0,
    now: 10000,
  }), false)
})
