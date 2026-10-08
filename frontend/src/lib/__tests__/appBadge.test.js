import test from 'node:test'
import assert from 'node:assert/strict'
import { appBadgeLabel, drawerRowUnread } from '../../components/Drawer/appBadge.js'

test('no pill for an empty, cleared, or malformed count', () => {
  for (const count of [0, -3, undefined, null, '', 'abc', Number.NaN, 0.5,
    Number.POSITIVE_INFINITY]) {
    assert.equal(appBadgeLabel(count), null, `count ${String(count)}`)
  }
})

test('counts show as whole numbers up to the cap, then 99+', () => {
  assert.equal(appBadgeLabel(1), '1')
  assert.equal(appBadgeLabel('7'), '7')
  assert.equal(appBadgeLabel(99), '99')
  assert.equal(appBadgeLabel(100), '99+')
  assert.equal(appBadgeLabel(2 ** 53), '99+')
})

test('an app with unread items shows the pill instead of the activity dot', () => {
  assert.deepEqual(drawerRowUnread('app', { badge_count: 3 }, true),
    { badgeLabel: '3', attentionDot: false })
})

test('an app without a count keeps the activity dot for new activity', () => {
  assert.deepEqual(drawerRowUnread('app', { badge_count: 0 }, true),
    { badgeLabel: null, attentionDot: true })
  assert.deepEqual(drawerRowUnread('app', {}, false),
    { badgeLabel: null, attentionDot: false })
})

test('chat and project rows never show an app pill', () => {
  assert.deepEqual(drawerRowUnread('chat', { badge_count: 5 }, true),
    { badgeLabel: null, attentionDot: true })
  assert.deepEqual(drawerRowUnread('project', { badge_count: 5 }, false),
    { badgeLabel: null, attentionDot: false })
})
