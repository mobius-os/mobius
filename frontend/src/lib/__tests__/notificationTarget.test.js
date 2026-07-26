import { test } from 'node:test'
import assert from 'node:assert/strict'
import { parseNotificationTarget } from '../notificationTarget.js'

// ── Valid forms round-trip ────────────────────────────────────────────────────

test('in-scope shell app target parses (id and slug, with and without intent)', () => {
  assert.deepEqual(parseNotificationTarget('/shell/?app=7'),
    { view: 'canvas', app: '7', intent: null })
  assert.deepEqual(parseNotificationTarget('/shell/?app=tip-calculator'),
    { view: 'canvas', app: 'tip-calculator', intent: null })
  assert.deepEqual(parseNotificationTarget('/shell/?app=7&intent=artifact:tip-7f3a'),
    { view: 'canvas', app: '7', intent: 'artifact:tip-7f3a' })
})

test('a malformed intent is dropped, not rejected with the whole target', () => {
  assert.deepEqual(parseNotificationTarget('/shell/?app=7&intent=<script>'),
    { view: 'canvas', app: '7', intent: null })
})

test('in-scope shell chat target parses', () => {
  assert.deepEqual(parseNotificationTarget('/shell/?chat=abc-123'),
    { view: 'chat', chatId: 'abc-123' })
})

test('legacy /app/:id and /chat/:id still parse (old rows)', () => {
  assert.deepEqual(parseNotificationTarget('/app/42'),
    { view: 'canvas', app: '42', intent: null })
  assert.deepEqual(parseNotificationTarget('/chat/abc123'),
    { view: 'chat', chatId: 'abc123' })
})

test('a same-origin absolute URL parses like its path form', () => {
  const prev = globalThis.location
  globalThis.location = { origin: 'https://mobius.example' }
  try {
    assert.deepEqual(
      parseNotificationTarget('https://mobius.example/shell/?chat=c1'),
      { view: 'chat', chatId: 'c1' },
    )
  } finally {
    if (prev === undefined) delete globalThis.location
    else globalThis.location = prev
  }
})

// ── The malicious-target class fails CLOSED (pre-flight §1: app tokens can
//    write `target` free-form, so every unknown shape must parse to null) ─────

test('javascript: and other scheme smuggling parses to null', () => {
  assert.equal(parseNotificationTarget('javascript:alert(1)'), null)
  assert.equal(parseNotificationTarget('data:text/html,<h1>x</h1>'), null)
  assert.equal(parseNotificationTarget('vbscript:msgbox'), null)
})

test('cross-origin and protocol-relative URLs parse to null', () => {
  const prev = globalThis.location
  globalThis.location = { origin: 'https://mobius.example' }
  try {
    assert.equal(parseNotificationTarget('https://evil.com/shell/?app=1'), null)
    assert.equal(parseNotificationTarget('http://mobius.example.evil.com/shell/?app=1'), null)
    assert.equal(parseNotificationTarget('//evil.com/shell/?app=1'), null)
  } finally {
    if (prev === undefined) delete globalThis.location
    else globalThis.location = prev
  }
})

test('an absolute URL with no known own-origin fails closed', () => {
  const prev = globalThis.location
  delete globalThis.location
  try {
    assert.equal(parseNotificationTarget('https://anywhere.example/shell/?app=1'), null)
  } finally {
    if (prev !== undefined) globalThis.location = prev
  }
})

test('id charset violations parse to null', () => {
  assert.equal(parseNotificationTarget('/shell/?app=<script>'), null)
  assert.equal(parseNotificationTarget('/shell/?chat=../../etc'), null)
  assert.equal(parseNotificationTarget('/app/not-a-number'), null)
  assert.equal(parseNotificationTarget('/chat/a b'), null)
})

test('unknown views, paths, and junk parse to null', () => {
  assert.equal(parseNotificationTarget('/shell/?view=notifications'), null)
  assert.equal(parseNotificationTarget('/shell/?view=settings'), null)
  assert.equal(parseNotificationTarget('/shell/?view=evil'), null)
  assert.equal(parseNotificationTarget('/shell/'), null)
  assert.equal(parseNotificationTarget('/admin'), null)
  assert.equal(parseNotificationTarget(''), null)
  assert.equal(parseNotificationTarget(null), null)
  assert.equal(parseNotificationTarget(42), null)
})
