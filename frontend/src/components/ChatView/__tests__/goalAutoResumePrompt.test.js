import assert from 'node:assert/strict'
import test from 'node:test'
import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'

import GoalAutoResumePrompt, {
  shouldOfferGoalAutoResume,
} from '../GoalAutoResumePrompt.jsx'
import {
  isGoalAutoResumeDismissed,
  rememberGoalAutoResumeDismissed,
} from '../goalAutoResumeDismissal.js'

const OPEN = { policyKnown: true, autoResumeEnabled: false }

function memoryStorage() {
  const values = new Map()
  return {
    getItem: (key) => (values.has(key) ? values.get(key) : null),
    setItem: (key, value) => { values.set(key, String(value)) },
  }
}

function withBrowserStorage(storage, run) {
  const previous = Object.getOwnPropertyDescriptor(globalThis, 'localStorage')
  Object.defineProperty(globalThis, 'localStorage', { value: storage, configurable: true })
  try {
    return run()
  } finally {
    if (previous) Object.defineProperty(globalThis, 'localStorage', previous)
    else delete globalThis.localStorage
  }
}

test('offers the policy only for Goals the server would continue automatically', () => {
  assert.equal(shouldOfferGoalAutoResume({ ...OPEN, goal: { status: 'active' } }), true)
  // An open Goal waiting between runs has no pause_reason.
  assert.equal(shouldOfferGoalAutoResume({ ...OPEN, goal: { status: 'paused' } }), true)
  assert.equal(shouldOfferGoalAutoResume({ ...OPEN, goal: { status: 'completed' } }), false)
  assert.equal(shouldOfferGoalAutoResume({ ...OPEN, goal: null }), false)
})

test('does not offer auto-continue for a held Goal, which the server never resumes', () => {
  for (const pause_reason of ['owner', 'agent', 'unknown', 'deferred']) {
    assert.equal(shouldOfferGoalAutoResume({
      ...OPEN, goal: { status: 'paused', pause_reason },
    }), false, `a Goal paused by ${pause_reason} stays held`)
  }
})

test('stays quiet when the policy is on, unknown, embedded, or a limit park offers it already', () => {
  const goal = { status: 'active' }
  assert.equal(shouldOfferGoalAutoResume({ ...OPEN, goal, autoResumeEnabled: true }), false)
  assert.equal(shouldOfferGoalAutoResume({ ...OPEN, goal, policyKnown: false }), false,
    'until chat info loads the policy reads off even when it is on')
  assert.equal(shouldOfferGoalAutoResume({ ...OPEN, goal, embedded: true }), false)
  assert.equal(shouldOfferGoalAutoResume({ ...OPEN, goal, limitParked: true }), false,
    'the parked card already carries the same switch')
})

test('states what enabling does: this chat, paid usage, new chats you start, standby, manual stops', () => {
  const html = renderToStaticMarkup(createElement(GoalAutoResumePrompt, {
    goalKey: 'goal-1',
    onEnable() {},
  }))

  assert.match(html, /Continue this chat after usage limits\?/)
  assert.match(html, /Auto-continue this chat/)
  assert.match(html, /even while this device sleeps/)
  assert.match(html, /around the provider&#x27;s reset time, or after a short wait if none is given/)
  assert.doesNotMatch(html, /at the provider&#x27;s reset time/,
    'the server retries at a bounded check time, not at the reset itself')
  assert.match(html, /Retries may use paid usage/)
  assert.match(html, /new chats you start will have this on/)
  assert.match(html, /Manual stops stay stopped/)
  assert.match(html, /Not now/)
})

test('does not render without an active goal or persistence owner', () => {
  assert.equal(renderToStaticMarkup(createElement(GoalAutoResumePrompt, {
    goalKey: '',
    onEnable() {},
  })), '')
  assert.equal(renderToStaticMarkup(createElement(GoalAutoResumePrompt, {
    goalKey: 'goal-1',
  })), '')
})

test('keeps save failures visible and the action pending', () => {
  const html = renderToStaticMarkup(createElement(GoalAutoResumePrompt, {
    goalKey: 'goal-1',
    saving: true,
    error: 'Could not save',
    onEnable() {},
  }))

  assert.match(html, /Enabling…/)
  assert.match(html, /role="alert"/)
  assert.match(html, /Could not save/)
})

test('a remembered Not now suppresses the prompt for that chat and Goal only', () => {
  const storage = memoryStorage()
  rememberGoalAutoResumeDismissed('chat-1', 'goal-1', storage)

  assert.equal(isGoalAutoResumeDismissed('chat-1', 'goal-1', storage), true)
  assert.equal(isGoalAutoResumeDismissed('chat-1', 'goal-2', storage), false)
  assert.equal(isGoalAutoResumeDismissed('chat-2', 'goal-1', storage), false)

  const render = (chatId, goalKey) => withBrowserStorage(storage, () => (
    renderToStaticMarkup(createElement(GoalAutoResumePrompt, {
      chatId, goalKey, onEnable() {},
    }))
  ))
  assert.equal(render('chat-1', 'goal-1'), '')
  assert.match(render('chat-1', 'goal-2'), /Auto-continue this chat/)
  assert.match(render('chat-2', 'goal-1'), /Auto-continue this chat/)
})

test('blocked storage leaves the prompt usable', () => {
  const blocked = {
    getItem() { throw new Error('blocked') },
    setItem() { throw new Error('blocked') },
  }
  rememberGoalAutoResumeDismissed('chat-1', 'goal-1', blocked)
  assert.equal(isGoalAutoResumeDismissed('chat-1', 'goal-1', blocked), false)
  assert.equal(isGoalAutoResumeDismissed('chat-1', 'goal-1', null), false)
  assert.match(withBrowserStorage(blocked, () => renderToStaticMarkup(
    createElement(GoalAutoResumePrompt, {
      chatId: 'chat-1', goalKey: 'goal-1', onEnable() {},
    }),
  )), /Auto-continue this chat/)
})
