// Resource views preserve actionable order, honest measurements and safe confirmation boundaries.
import assert from 'node:assert/strict'
import { after, test } from 'node:test'
import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { createSsrRenderServer } from '../../../../lib/__tests__/ssrRenderServer.mjs'

const vite = await createSsrRenderServer()
const { StorageMemorySectionView } = await vite.ssrLoadModule('/src/components/SettingsView/storageMemory/StorageMemorySection.jsx')
const { default: Details } = await vite.ssrLoadModule('/src/components/SettingsView/storageMemory/StorageMemoryDetails.jsx')
after(() => vite.close())

const overview = {
  storage: { free_bytes: 1060e6, total_bytes: 4690e6, pause_below_bytes: 234e6, level: null },
  memory: { used_bytes: 1410e6, limit_bytes: 4e9, pause_at_bytes: 3.6e9, level: null,
    groups: [{ label: 'Other', bytes: 511e6 }, { label: 'Agent turns', bytes: 331e6 }] },
  measured_at: '2026-10-27T07:50:55Z', cleanable: [{ target: 'agent_sessions', bytes: 918e6 }],
  kept: [{ id: 'apps', bytes: 190e6, label: 'Apps' }],
  running: [{ chat_id: 'example', title: 'A running example' }],
}
const resources = { overview, measuring: false, error: '', stopTurn: () => {} }
const render = (Component, extra = {}) => renderToStaticMarkup(createElement(Component, { resources, ...extra }))

test('running work leads the optional memory accounting and has explicit navigation and Stop', () => {
  const html = render(Details, { onOpenChat: () => {} })
  assert.ok(html.indexOf('A running example') < html.indexOf('What is using memory?'))
  const kept = html.indexOf('Kept to run Möbius')
  assert.ok(kept >= 0 && kept < html.indexOf('What is using memory?'), 'the memory breakdown closes the details')
  assert.match(html, /<details class="storage-memory__group storage-memory__memory-details">/)
  assert.match(html, /1 turn/)
  assert.match(html, /Open chat: A running example/)
  assert.match(html, /Stop turn: A running example/)
  assert.match(html, /Estimates by group, not what Stop will free/)
  const turn = html.match(/class="storage-memory__turn-main"[\s\S]*?<\/li>/)[0]
  assert.doesNotMatch(turn, /331 MB|511 MB/)
})

test('closed confirmations are inert and their buttons are not approval', () => {
  const html = render(Details)
  assert.equal((html.match(/data-open="false" inert=""/g) || []).length, 2)
  assert.match(html, /918 MB <small>stored<\/small>/)
  assert.doesNotMatch(html, /This frees about 918 MB/)
  assert.match(html, /only ones older than 2 days clear/)
})

test('empty work shows one quiet line and leaves the breakdown available', () => {
  const html = render(Details, { resources: { ...resources, overview: { ...overview, running: [] } } })
  assert.match(html, /0 turns/)
  assert.match(html, /<p class="storage-memory__empty">No agents running<\/p>/)
  assert.doesNotMatch(html, /No agent turns running|still use memory/)
  assert.match(html, /What is using memory/)
  assert.doesNotMatch(html, /Stop turn:/)
})

test('the details toggle and refresh share one always-visible row', () => {
  const collapsed = render(StorageMemorySectionView)
  const row = collapsed.match(/<div class="storage-memory__controls">[\s\S]*?<\/button><\/div><\/div>/)[0]
  assert.match(row, /Show details/)
  assert.match(row, /<span class="storage-memory__sync-label">Synced /)
  assert.match(row, /aria-label="Refresh storage and memory"/)
  assert.doesNotMatch(render(Details), /Measure again|storage-memory__footer/)
  const busy = render(StorageMemorySectionView, { resources: { ...resources, measuring: true } })
  assert.match(busy, /Syncing…/)
  assert.match(busy, /aria-label="Refresh storage and memory"[^>]*disabled=""|disabled=""[^>]*aria-label="Refresh storage and memory"/)
})

test('both collapsed meters express used capacity and expose pause information', () => {
  const html = render(StorageMemorySectionView)
  assert.equal((html.match(/role="meter"/g) || []).length, 2)
  assert.match(html, /3.63 GB \/ 4.69 GB used/)
  assert.match(html, /1.06 GB free/)
  assert.match(html, /1.41 GB \/ 4.00 GB used/)
  assert.doesNotMatch(html, /storage-memory__notice--/)
})

test('a failed update keeps the earlier meters and exposes an actionable retry', () => {
  const html = render(StorageMemorySectionView, { resources: { ...resources, error: 'Reading unavailable' } })
  assert.match(html, /role="alert"/)
  assert.match(html, /Showing previous readings/)
  assert.match(html, /Reading unavailable/)
  assert.match(html, /Retry measurement/)
  assert.equal((html.match(/role="meter"/g) || []).length, 2)
})

test('a failed first reading offers Retry instead of an empty or stuck loading view', () => {
  const html = render(StorageMemorySectionView, { resources: { ...resources, overview: null, error: 'Reading unavailable' } })
  assert.match(html, /Retry measurement/)
  assert.doesNotMatch(html, /Checking storage|Showing previous readings|role="meter"/)
})

test('a chat with queued messages is stopped from the chat, so its queue is kept', () => {
  const html = render(Details, { resources: { ...resources, overview: { ...overview,
    running: [{ chat_id: 'busy', title: 'Queued example', queued: 2, helpers: 0 }] } }, onOpenChat: () => {} })
  assert.doesNotMatch(html, /Stop turn: Queued example/)
  assert.match(html, /Open chat to stop/)
  assert.match(html, /Has queued messages/)
})

test('the Stop confirmation names helper agents that stop with the turn', () => {
  const html = render(Details, { resources: { ...resources, overview: { ...overview,
    running: [{ chat_id: 'lead', title: 'Lead example', queued: 0, helpers: 2 }] } } })
  assert.match(html, /Stop turn: Lead example/)
  assert.match(html, /Its 2 helper agents stop too/)
})

test('unmeasured cleanups stay clearable and missing readings say so', () => {
  const details = render(Details, { resources: { ...resources, overview: { ...overview,
    cleanable: [{ target: 'work_files', bytes: null, incomplete: false }] } } })
  assert.match(details, /Not measured/)
  assert.doesNotMatch(details, /aria-label="Clear chat work files"[^>]*disabled/)
  const section = render(StorageMemorySectionView, { resources: { ...resources, overview: { ...overview, storage: null } } })
  assert.match(section, /Storage reading unavailable/)
  assert.equal((section.match(/role="meter"/g) || []).length, 1)
})
