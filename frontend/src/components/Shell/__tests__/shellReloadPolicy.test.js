import { test } from 'node:test'
import assert from 'node:assert/strict'

import {
  hasActiveChatTurn,
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

test('active chat turns defer shell reloads only for the visible chat', () => {
  assert.equal(hasActiveChatTurn({
    activeView: 'chat',
    activeChatId: 'c1',
    streamingChatIds: new Set(['c1']),
  }), true)
  assert.equal(hasActiveChatTurn({
    activeView: 'chat',
    activeChatId: 'c2',
    streamingChatIds: new Set(['c1']),
  }), false)
  assert.equal(hasActiveChatTurn({
    activeView: 'canvas',
    activeChatId: 'c1',
    streamingChatIds: new Set(['c1']),
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

test('hidden pages can reload without disrupting focus', () => {
  assert.equal(shouldDeferShellReload({
    activeElement: el('textarea'),
    visibilityState: 'hidden',
    lastUserInteractionAt: 10000,
    now: 10001,
  }), false)
})
