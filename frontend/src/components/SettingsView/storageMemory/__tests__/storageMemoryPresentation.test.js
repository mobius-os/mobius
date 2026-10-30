// The Storage and Memory box speaks only when the owner should act, in plain sizes.
import assert from 'node:assert/strict'
import { test } from 'node:test'
import {
  CLEANUP_COPY, GROUP_COPY, boxStatus, formatBytes, keptDescription, keptTotal, memoryMeter,
  previewSentence, resultSentence, storageMeter, syncedLabel, memoryGroupLabel, runningTurnTitle, storedSize, resourceWarning,
} from '../storageMemoryPresentation.js'

test('sizes use the decimal units the storage alert and hosts use', () => {
  assert.equal(formatBytes(546e6), '546 MB')
  assert.equal(formatBytes(1_036_000_000), '1.04 GB')
  assert.equal(formatBytes(0), '0 MB')
  assert.equal(formatBytes(500), '1 KB')
})

test('a healthy server shows no status at all, never a green OK', () => {
  assert.equal(boxStatus({ storage: { level: null }, memory: { level: null } }), null)
  assert.equal(boxStatus(null), null)
})

test('the most urgent status wins and paused names what the owner feels', () => {
  assert.deepEqual(boxStatus({ storage: { level: 'getting_low' }, memory: { level: null } }),
    { label: 'Storage getting low', tone: 'warning' })
  assert.deepEqual(boxStatus({ storage: { level: 'low' }, memory: { level: 'paused' } }),
    { label: 'Agents paused', tone: 'danger' })
  assert.equal(boxStatus({ storage: { level: null }, memory: { level: 'low' } }).label, 'Memory is high')
})

test('the storage bar marks where agents pause and explains it', () => {
  const meter = storageMeter({ free_bytes: 546e6, total_bytes: 4682e6, pause_below_bytes: 234e6, level: 'getting_low' })
  assert.equal(meter.value, '4.14 GB / 4.68 GB used')
  assert.equal(meter.support, '546 MB free')
  assert.ok(Math.abs(meter.markPercent - 95) < 0.1)
  assert.equal(meter.note, 'Agents pause below 234 MB free.')
  const paused = storageMeter({ free_bytes: 200e6, total_bytes: 4682e6, pause_below_bytes: 234e6, level: 'paused' })
  assert.equal(paused.note, 'Agents are paused. They start again above 234 MB free.')
})

test('the memory bar shows use against the limit and the pause point', () => {
  const meter = memoryMeter({ used_bytes: 903e6, limit_bytes: 4e9, pause_at_bytes: 3.6e9, level: null })
  assert.equal(meter.value, '903 MB / 4.00 GB used')
  assert.equal(meter.markPercent, 90)
})

test('the confirm step states the size and count it will clear', () => {
  assert.equal(previewSentence('work_files', { bytes: 922e6, items: 17 }), 'This frees about 922 MB from 17 chats.')
  // A count of provider copies reads as a count of chats, so this row says chats stay instead.
  assert.equal(previewSentence('agent_sessions', { bytes: 684e6, items: 444 }), 'This frees about 684 MB. Your chats and messages stay.')
  assert.equal(previewSentence('agent_browsers', { bytes: 0, items: 0 }), 'Nothing to clear right now.')
})

test('the result says what was freed, what is free now, and any skip', () => {
  assert.equal(resultSentence({ bytes: 922e6 }, { free_bytes: 1468e6 }), 'Cleared 922 MB. 1.47 GB free now.')
  assert.equal(
    resultSentence({ bytes: 0, note: 'Codex is busy, so its old copies stay until it is idle.' }, null),
    'Nothing was cleared. Codex is busy, so its old copies stay until it is idle.',
  )
})

test('sync age reads as a short relative time', () => {
  const now = Date.parse('2026-10-26T15:30:00Z')
  assert.equal(syncedLabel({ measuredAt: '2026-10-26T15:30:00+00:00' }, now), 'Synced just now')
  assert.equal(syncedLabel({ measuredAt: '2026-10-26T15:29:00Z' }, now), 'Synced 1m ago')
  assert.equal(syncedLabel({ measuredAt: '2026-10-26T14:50:00Z' }, now), 'Synced 40m ago')
  assert.equal(syncedLabel({ measuredAt: '2026-10-26T12:30:00Z' }, now), 'Synced 3h ago')
  assert.equal(syncedLabel({ measuredAt: '2026-10-25T15:00:00Z' }, now), 'Synced 1d ago')
  assert.equal(syncedLabel({ measuredAt: null }, now), 'Not synced yet')
  assert.equal(syncedLabel({ measuredAt: '2026-10-26T15:00:00Z', measuring: true }, now), 'Syncing…')
  assert.equal(syncedLabel({ measuredAt: '2026-10-26T15:00:00Z', failed: true }, now), 'Sync failed')
})

test('every Clear group promises chats, apps and app data stay', () => {
  assert.match(GROUP_COPY.clear.helper, /Your chats, uploads, apps and app data stay\./)
  assert.match(CLEANUP_COPY.agent_sessions.description, /only ones older than 2 days clear/)
  assert.match(CLEANUP_COPY.agent_sessions.context, /for 14 days/)
  assert.match(CLEANUP_COPY.agent_sessions.after, /may remember fewer details/)
})

test('warnings send the owner to the matching resource and stay quiet when healthy', () => {
  assert.equal(resourceWarning({ storage: { level: null }, memory: { level: null } }), null)
  const disk = resourceWarning({ storage: { level: 'getting_low' }, memory: null })
  assert.deepEqual(disk.actions, [{ group: 'clear', label: 'See cleanup options' }])
  assert.equal(disk.tone, 'warning')
  const memory = resourceWarning({ storage: null, memory: { level: 'paused' } })
  assert.equal(memory.title, 'Agents cannot start')
  assert.deepEqual(memory.actions, [{ group: 'running', label: 'See running work' }])
  assert.match(memory.description, /Clearing files frees storage instead/)
  const both = resourceWarning({ storage: { level: 'paused' }, memory: { level: 'low' } })
  assert.deepEqual(both.actions.map(action => action.group), ['clear', 'running'])
})

test('group estimates use plain names, never per-chat attribution', () => {
  assert.equal(memoryGroupLabel('Agent turns'), 'Agent processes')
  assert.equal(memoryGroupLabel('Other'), 'Other processes')
  assert.equal(memoryGroupLabel('Möbius server'), 'Möbius server')
})

test('the kept summary totals its folders and explains each in plain words', () => {
  const kept = [{ id: 'database', bytes: 513e6 }, { id: 'apps', bytes: 187e6 }, { id: 'mystery', bytes: 1e6 }]
  assert.equal(formatBytes(keptTotal(kept)), '701 MB')
  assert.equal(keptDescription('database'), 'Your chats and messages, app records and settings.')
  assert.equal(keptDescription('mystery'), '')
})

test('helper chats get a readable name instead of their internal id', () => {
  assert.equal(runningTurnTitle('Delegation · review-baa7601b1a0eaf4f7a51458675b6345ba680f914'), 'Helper agent: review')
  assert.equal(runningTurnTitle('Delegation · 0123456789abcdef0123'), 'Helper agent')
  assert.equal(runningTurnTitle('PLAN: Evaluate memory'), 'PLAN: Evaluate memory')
  assert.equal(runningTurnTitle(''), 'Untitled chat')
})

test('a stored size says when it is unmeasured or only partly measured', () => {
  assert.deepEqual(storedSize({ bytes: 302e6, incomplete: false }), { value: '302 MB', note: 'stored' })
  assert.deepEqual(storedSize({ bytes: 302e6, incomplete: true }), { value: '302 MB+', note: 'partly measured' })
  assert.deepEqual(storedSize({ bytes: null }), { value: 'Not measured', note: 'yet' })
})
