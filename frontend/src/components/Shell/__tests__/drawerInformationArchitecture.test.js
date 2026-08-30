import { test } from 'node:test'
import assert from 'node:assert/strict'

import {
  buildDrawerSections,
  findDrawerMenuItem,
} from '../../Drawer/drawerInformationArchitecture.js'

test('projects join Recents only after they have been opened', () => {
  const unopened = { id: 'p-unopened', name: 'Unopened', updated_at: '2026-08-26T08:00:00' }
  const opened = {
    id: 'p-opened',
    name: 'Opened',
    updated_at: '2026-08-25T08:00:00',
    last_opened_at: '2026-08-26T09:00:00',
  }

  const result = buildDrawerSections([], [], [unopened, opened])

  assert.deepEqual(result.recents.map(row => `${row.kind}:${row.item.id}`), [
    'project:p-opened',
  ])
})

test('Creations join Recents only after they have been opened', () => {
  const project = {
    id: 'project-1', name: 'Album', updated_at: '2026-08-26T08:00:00',
    artifacts: [
      { id: 'unopened', name: 'Unopened', status: 'ok', has_output: true },
      {
        id: 'opened', name: 'Opened', status: 'ok', has_output: true,
        last_opened_at: '2026-08-26T10:00:00',
      },
    ],
  }

  const result = buildDrawerSections([], [], [project])

  assert.deepEqual(result.recents.map(row => `${row.kind}:${row.item.id}`), [
    'artifact:project-1:opened',
  ])
})

test('a pinned project shares the combined pinned order and leaves Recents', () => {
  const project = {
    id: 'pinned-project',
    name: 'Pinned project',
    last_opened_at: '2026-08-26T09:00:00',
    pinned_at: '2026-08-26T10:00:00',
  }
  const app = {
    id: 7,
    name: 'Pinned app',
    pinned_at: '2026-08-26T09:00:00',
  }

  const result = buildDrawerSections([], [app], [project])

  assert.deepEqual(result.pinned.map(row => `${row.kind}:${row.item.id}`), [
    'app:7',
    'project:pinned-project',
  ])
  assert.equal(result.recents.some(row => row.item.id === project.id), false)
})

test('the shared row menu resolves project identities', () => {
  const project = { id: 'project-1', name: 'Project one' }
  assert.equal(
    findDrawerMenuItem({ kind: 'project', id: project.id }, [], [], [project]),
    project,
  )
})
