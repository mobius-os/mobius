// Another browser's folders each say what agents can do with that folder, not what its browser is doing.
import assert from 'node:assert/strict'
import { test } from 'node:test'
import { otherFolderStatus } from '../../../components/SettingsView/sharedFolderLabels.js'
import { groupByDevice } from '../../../components/SettingsView/useOtherDeviceFolders.js'

const device = { id: 'laptop', label: 'Chrome on Windows' }
const folder = (id, state, online = true) => ({ id, name: id, state, online, device })

test('a browser that is open does not make every one of its folders Ready', () => {
  const [laptop] = groupByDevice([
    folder('ready', 'ready'),
    folder('paused', 'paused'),
    folder('forgot', 'needs_pick'),
    folder('asks', 'needs_permission'),
    folder('gone', 'offline', false),
  ])
  assert.equal(laptop.online, true)
  assert.deepEqual(
    laptop.folders.map(item => otherFolderStatus(item).label),
    ['Ready', 'Paused', 'Pick again there', 'Needs reconnect there', 'Offline'],
  )
})

test('a folder the server reports offline is offline whatever its last state was', () => {
  assert.equal(otherFolderStatus(folder('stale', 'ready', false)).label, 'Offline')
})

test('a state the server adds later is never shown as Ready', () => {
  assert.equal(otherFolderStatus(folder('new', 'something_new')).label, 'Offline')
})
