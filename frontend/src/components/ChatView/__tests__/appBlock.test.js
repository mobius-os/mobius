import test from 'node:test'
import assert from 'node:assert/strict'
import { APP_BLOCK_LIMITS, appBlockFromToken, transcriptHostRequest } from '../markdown/appBlock.js'
import { safeSourceUrl } from '../messageSources.js'

const token = value => ({ type: 'code', lang: 'mobius-app', text: JSON.stringify(value) })
const base = { app: 'example', intent: 'open:item-42', title: 'Item 42' }

test('a block is a destination and a bounded snapshot, never authority', () => {
  const block = appBlockFromToken(token({ ...base, approved: true, height: 9999,
    facts: [{ label: 'Files', value: '4' }], badges: [{ label: 'Ready', tone: 'success' }] }))
  assert.equal(block.approved, undefined)
  assert.equal(block.height, 640)
  assert.equal(block.inline, true)
  assert.deepEqual(block.facts, [{ label: 'Files', value: '4' }])
  assert.deepEqual(block.badges, [{ label: 'Ready', tone: 'success' }])
  assert.equal(new URL(block.href, 'https://mobius.test').searchParams.get('intent'), 'open:item-42')
  assert.equal(new URL(block.href, 'https://mobius.test').searchParams.get('app'), 'example')
})

test('invalid or incomplete root fields leave an ordinary code block', () => {
  for (const value of [
    { ...base, app: '../auth' }, { ...base, intent: 'javascript: alert(1)' }, { ...base, title: '' },
    { ...base, title: 'x'.repeat(APP_BLOCK_LIMITS.title + 1) }, { app: 'example', intent: 'open:x' }, null, [],
  ]) assert.equal(appBlockFromToken(token(value)), null)
  assert.equal(appBlockFromToken({ type: 'code', lang: 'mobius-app', text: '{' }), null)
  assert.equal(appBlockFromToken({ type: 'code', lang: 'json', text: JSON.stringify(base) }), null)
  assert.equal(appBlockFromToken({ type: 'code', lang: 'mobius-app', text: ' '.repeat(APP_BLOCK_LIMITS.json + 1) }), null)
})

test('root, action, item and item-action intents share one exact payload bound', () => {
  const at = n => `open:${'x'.repeat(n)}`
  const limit = APP_BLOCK_LIMITS.intentPayload
  assert.equal(appBlockFromToken(token({ ...base, intent: at(limit) })).intent, at(limit))
  assert.equal(appBlockFromToken(token({ ...base, intent: at(limit + 1) })), null)
  const block = appBlockFromToken(token({ ...base,
    action: { label: 'Too long', intent: at(limit + 1) },
    items: [
      { title: 'Kept', intent: at(limit), action: { label: 'Too long', intent: at(limit + 1) } },
      { title: 'Dropped', intent: at(limit + 1) },
    ] }))
  assert.equal(block.action, null)
  assert.deepEqual(block.items.map(item => [item.title, item.action]), [['Kept', null]])
  // Whitespace, non-ASCII, empty payloads and over-long kinds are never intents.
  for (const intent of ['open:', 'open:two words', 'open:line\nbreak', ':item', 'open:é', `${'k'.repeat(33)}:x`]) {
    assert.equal(appBlockFromToken(token({ ...base, intent })), null, intent)
  }
  assert.ok(appBlockFromToken(token({ ...base, intent: `${'k'.repeat(32)}:x` })))
})

test('a twelve-record id list does not fit; a short app-owned handle does', () => {
  const ids = Array.from({ length: 12 }, (_, i) => `00000000-0000-4000-8000-${String(i).padStart(12, '0')}`)
  const list = appBlockFromToken(token({ ...base, action: { label: 'Contribute all', intent: `send-batch:${ids.join(',')}` } }))
  assert.equal(list.action, null)
  const handle = appBlockFromToken(token({ ...base, action: { label: 'Contribute all', intent: 'send-batch:sel-7f3a' } }))
  assert.deepEqual(handle.action, { label: 'Contribute all', intent: 'send-batch:sel-7f3a' })
})

test('items keep up to twelve valid generic rows, each its own destination', () => {
  const items = Array.from({ length: 14 }, (_, i) => ({ title: `Item ${i}`, intent: `open:rec-${i}`,
    facts: [{ label: 'Repository', value: 'owner/repo', href: 'https://github.com/owner/repo' }, ...Array(5).fill({ label: 'x', value: 'y' })],
    badges: [{ label: 'All clear', tone: 'success' }], action: { label: 'Review', intent: `review:rec-${i}` } }))
  const block = appBlockFromToken(token({ ...base, items: [{ title: '', intent: 'open:x' }, ...items] }))
  assert.equal(block.items.length, APP_BLOCK_LIMITS.items)
  assert.equal(block.items[0].title, 'Item 0')
  assert.equal(block.items[0].facts.length, APP_BLOCK_LIMITS.itemFacts)
  assert.deepEqual(block.items[0].action, { label: 'Review', intent: 'review:rec-0' })
  assert.match(block.items[0].href, /intent=open%3Arec-0/)
})

test('link-only blocks keep their facts and offer no app action', () => {
  const block = appBlockFromToken(token({ ...base, inline: false, action: { label: 'Go', intent: 'go:1' },
    items: [{ title: 'Row', intent: 'open:1', action: { label: 'Go', intent: 'go:1' } }] }))
  assert.equal(block.inline, false)
  assert.equal(block.action, null)
  assert.equal(block.items[0].action, null)
})

test('badges are bounded generic tones, not app colours', () => {
  const block = appBlockFromToken(token({ ...base, badges: [{ label: 'One', tone: 'neon' }, { label: '' },
    { label: 'Two', tone: 'danger', color: 'ff0000' }, { label: 'Three' }, { label: 'Four' }] }))
  assert.deepEqual(block.badges, [{ label: 'One', tone: 'neutral' }, { label: 'Two', tone: 'danger' }, { label: 'Three', tone: 'neutral' }])
})

test('fact links use the shared policy, HTTPS without credentials only', () => {
  const block = appBlockFromToken(token({ ...base, facts: [
    { label: 'Unsafe', value: 'a', href: 'javascript:alert(1)' },
    { label: 'Plain', value: 'b', href: 'http://example.com/' },
    { label: 'Credentials', value: 'c', href: 'https://user:pass@example.com/' },
    { label: 'Safe', value: 'd', href: 'https://example.com/x' },
  ] }))
  assert.deepEqual(block.facts.map(fact => fact.href ?? null), [null, null, null, 'https://example.com/x'])
  // The ordinary chat-source policy is unchanged.
  assert.equal(safeSourceUrl('http://example.com/'), 'http://example.com/')
  assert.equal(safeSourceUrl('https://user:pass@example.com/'), 'https://user:pass@example.com/')
  assert.equal(safeSourceUrl('https://user:pass@example.com/', { httpsOnly: true }), '')
})

test('an app may name its expand control, bounded and optional', () => {
  assert.equal(appBlockFromToken(token({ ...base, expand_label: '  Review here  ' })).expandLabel, 'Review here')
  assert.equal(appBlockFromToken(token({ ...base, expand_label: 'x'.repeat(90) })).expandLabel, '', 'over-limit falls back, never shortened')
  assert.equal(appBlockFromToken(token({ ...base, expand_label: 7 })).expandLabel, '')
})

test('an embedded transcript view may only open a conversation or an app', () => {
  const seen = []
  const navigate = target => seen.push(target)
  transcriptHostRequest({ type: 'moebius:open-chat', chatId: 'c1' }, { navigate })
  transcriptHostRequest({ type: 'moebius:open-app', appId: 3, intent: 'x:1' }, { navigate })
  assert.deepEqual(seen, ['/shell/?chat=c1', '/shell/?app=3&intent=x%3A1'])
  for (const request of [{ type: 'moebius:new-chat' }, { type: 'moebius:open-settings' }, { type: 'moebius:open-chat' }]) {
    assert.throws(() => transcriptHostRequest(request, { navigate }), /only open conversations and apps/)
  }
  assert.equal(seen.length, 2)
})

test('inline sessions are opt-in and never apply to link-only blocks', () => {
  assert.equal(appBlockFromToken(token({ ...base, interaction: 'inline' })).session, true)
  assert.equal(appBlockFromToken(token(base)).session, false)
  assert.equal(appBlockFromToken(token({ ...base, inline: false, interaction: 'inline' })).session, false)
})

test('invalid facts never take a valid fact\'s slot', () => {
  const block = appBlockFromToken(token({ ...base, facts: [...Array(8).fill({ label: '', value: 'x' }), { label: 'Kept', value: 'yes' }] }))
  assert.deepEqual(block.facts, [{ label: 'Kept', value: 'yes' }])
})

test('an over-limit item title drops the item, like the root rule, never truncated', () => {
  const long = 'x'.repeat(APP_BLOCK_LIMITS.title + 1)
  const block = appBlockFromToken(token({ ...base, items: [{ title: long, intent: 'open:a' }, { title: '  Kept  ', intent: 'open:b' }] }))
  assert.deepEqual(block.items.map(item => item.title), ['Kept'])
})

test('repeated item destinations and badge labels collapse to their first entry', () => {
  const row = { title: 'Same', intent: 'open:same', badges: [{ label: 'Dup', tone: 'success' }, { label: 'Dup', tone: 'danger' }, { label: 'Other' }] }
  const block = appBlockFromToken(token({ ...base, items: [row, { ...row, title: 'Again' }, { title: 'Next', intent: 'open:next' }],
    badges: [{ label: 'A' }, { label: 'A' }, { label: 'B' }, { label: 'C' }] }))
  assert.deepEqual(block.items.map(item => item.title), ['Same', 'Next'])
  assert.deepEqual(block.items[0].badges, [{ label: 'Dup', tone: 'success' }, { label: 'Other', tone: 'neutral' }])
  assert.deepEqual(block.badges.map(badge => badge.label), ['A', 'B', 'C'])
})

test('over-limit fact, badge and action text is dropped, never truncated', () => {
  const block = appBlockFromToken(token({ ...base,
    facts: [{ label: 'L'.repeat(81), value: 'x' }, { label: 'Value', value: 'v'.repeat(241) }, { label: 'Kept', value: 'v'.repeat(240) }],
    badges: [{ label: 'b'.repeat(41) }, { label: 'Kept' }],
    action: { label: 'a'.repeat(41), intent: 'send:1' } }))
  assert.deepEqual(block.facts, [{ label: 'Kept', value: 'v'.repeat(240) }])
  assert.deepEqual(block.badges, [{ label: 'Kept', tone: 'neutral' }])
  assert.equal(block.action, null)
})

test('a malformed block never throws; wrong-typed fields fall back to a code block or are dropped', () => {
  const hostile = [{ toString: null }, { toString: null, valueOf: null }, ['pages'], null, 7, true, {}, []]
  const fields = ['app', 'intent', 'title', 'height', 'inline', 'interaction', 'expand_label', 'facts', 'badges', 'items', 'action']
  for (const bad of hostile) {
    for (const field of fields) {
      const value = { ...base, [field]: bad }
      assert.doesNotThrow(() => appBlockFromToken(token(value)), `${field}: ${JSON.stringify(bad)}`)
      for (const nested of [
        { facts: [{ label: bad, value: bad, href: bad }] }, { badges: [{ label: bad, tone: bad }], items: [bad] },
        { items: [{ title: bad, intent: bad, facts: bad, badges: bad, action: bad }] }, { action: { label: bad, intent: bad } },
      ]) assert.doesNotThrow(() => appBlockFromToken(token({ ...base, ...nested })), JSON.stringify(nested))
    }
    for (const required of ['app', 'intent', 'title']) {
      assert.equal(appBlockFromToken(token({ ...base, [required]: bad })), null, `${required} ${JSON.stringify(bad)} stays code`)
    }
    if (typeof bad !== 'number') assert.equal(appBlockFromToken(token({ ...base, height: bad })).height, 480)
  }
  assert.equal(appBlockFromToken(token({ ...base, app: ['example'] })), null, 'an array app is not a slug')
  assert.equal(appBlockFromToken(token({ ...base, height: 300 })).height, 300)
})

test('only a JSON false for expand turns the inline view off; sessions are unaffected', () => {
  assert.equal(appBlockFromToken(token(base)).expand, true)
  assert.equal(appBlockFromToken(token({ ...base, expand: false })).expand, false)
  for (const value of ['false', 0, null, {}, [], 'no']) {
    assert.equal(appBlockFromToken(token({ ...base, expand: value })).expand, true, JSON.stringify(value))
  }
  const session = appBlockFromToken(token({ ...base, expand: false, interaction: 'inline', action: { label: 'Publish', intent: 'send:all' } }))
  assert.equal(session.session, true)
  assert.deepEqual(session.action, { label: 'Publish', intent: 'send:all' })
})
