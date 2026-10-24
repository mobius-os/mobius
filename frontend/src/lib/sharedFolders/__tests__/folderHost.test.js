import { test, beforeEach, mock } from 'node:test'
import assert from 'node:assert/strict'
import { IDBFactory } from 'fake-indexeddb'
import { applyRemotePause, stopEverywhere } from '../folderActions.js'
import {
  handleSharedFolderCommand, setSharedFolderSubscription, showSharedFolderActivity,
} from '../folderHost.js'
import { deviceId } from '../folderStore.js'
import { createHandleFolder } from '../handleFolder.js'
import { hostStore, publish } from '../hostState.js'
import { FakeDirectoryHandle, fileText } from './fakeDirectory.js'

let answers = []
let pauses = []
let serverReachable = true

beforeEach(() => {
  answers = []
  pauses = []
  serverReachable = true
  globalThis.indexedDB = new IDBFactory()
  globalThis.fetch = async (url, options) => {
    if (String(url).endsWith('/api/shared-folders/responses')) answers.push(JSON.parse(options.body))
    if (String(url).endsWith('/api/shared-folders/pause')) {
      if (!serverReachable) throw new TypeError('Failed to fetch')
      pauses.push(JSON.parse(options.body))
    }
    return new Response('{}', { status: 200 })
  }
  publish({ activity: null })
  const store = hostStore()
  const root = new FakeDirectoryHandle('Downloads')
  root.addFile('a.txt', 'hello')
  store.records = new Map([
    ['edit', { id: 'edit', name: 'Downloads', mode: 'edit', access: 'remembered', paused: false }],
    ['read', { id: 'read', name: 'Docs', mode: 'read', access: 'remembered', paused: false }],
    ['paused', { id: 'paused', name: 'Old', mode: 'edit', access: 'remembered', paused: true }],
  ])
  store.access = new Map([['edit', createHandleFolder(root)], ['read', createHandleFolder(root)], ['paused', createHandleFolder(root)]])
  store.permissions = new Map([['edit', 'granted'], ['read', 'granted'], ['paused', 'granted']])
  store.subscriptionId = 'stream-1'
})

function command(folderId, op, extra = {}) {
  return {
    type: 'shared_folder_command', commandId: `c-${op}`, folderId, op, path: 'a.txt', startWithinMs: 25_000, ...extra,
  }
}

test('an agent command is answered on the stream that delivered it', async () => {
  await handleSharedFolderCommand(command('edit', 'read', { length: 5 }))
  assert.equal(answers.length, 1)
  assert.equal(answers[0].subscriptionId, 'stream-1')
  assert.equal(answers[0].commandId, 'c-read')
  assert.equal(answers[0].ok, true)
  assert.equal(answers[0].result.text, 'hello')
})

test('the browser itself refuses changes to a Read only folder', async () => {
  await handleSharedFolderCommand(command('read', 'bin'))
  assert.deepEqual([answers[0].ok, answers[0].code], [false, 'read_only'])
})

test('a paused folder refuses every command until the owner resumes it', async () => {
  await handleSharedFolderCommand(command('paused', 'list', { path: '' }))
  assert.deepEqual([answers[0].ok, answers[0].code], [false, 'paused'])
})

test('a folder this browser no longer shares answers not found', async () => {
  await handleSharedFolderCommand(command('gone', 'list', { path: '' }))
  assert.deepEqual([answers[0].ok, answers[0].code], [false, 'not_found'])
})

function editFolder(root) {
  hostStore().access.set('edit', createHandleFolder(root))
}

test('two changes at the same moment cannot both claim one new name', async () => {
  const root = new FakeDirectoryHandle('Downloads')
  editFolder(root)
  await Promise.all(['first', 'second'].map(text => handleSharedFolderCommand(
    command('edit', 'write', { commandId: `c-${text}`, path: 'new.txt', content: text }),
  )))
  assert.deepEqual(answers.map(answer => answer.ok ? 'ok' : answer.code).sort(), ['exists', 'ok'])
  assert.equal(await fileText(root, 'new.txt'), 'first')
})

const withdrawals = {
  paused: store => { store.records.get('edit').paused = true },
  not_found: store => { store.records.delete('edit'); store.access.delete('edit') },
  read_only: store => { store.records.get('edit').mode = 'read' },
}

/** An earlier change holds the folder's lock until the returned release is awaited. */
async function holdFolderLock(folderId) {
  let release
  const holding = new Promise(resolve => { release = resolve })
  let lockHeld
  const held = new Promise(resolve => { lockHeld = resolve })
  const finished = navigator.locks.request(`mobius-shared-folder:${folderId}`, () => { lockHeld(); return holding })
  await held
  return async () => {
    release()
    await finished
  }
}

for (const [code, withdraw] of Object.entries(withdrawals)) {
  test(`a change waiting its turn is refused if the owner withdraws rights meanwhile (${code})`, async () => {
    const root = new FakeDirectoryHandle('Downloads')
    editFolder(root)
    const release = await holdFolderLock('edit')
    const waiting = handleSharedFolderCommand(command('edit', 'write', { path: 'late.txt', content: 'late' }))
    withdraw(hostStore())
    await release()
    await waiting
    assert.deepEqual([answers[0].ok, answers[0].code], [false, code])
    assert.equal(root.children.has('late.txt'), false)
  })
}

test('a change still waiting when the server would stop waiting is dropped, not made late', async () => {
  const root = new FakeDirectoryHandle('Downloads')
  editFolder(root)
  const release = await holdFolderLock('edit')
  const waiting = handleSharedFolderCommand(command('edit', 'write', { path: 'late.txt', content: 'late', startWithinMs: 1 }))
  await new Promise(resolve => setTimeout(resolve, 20))
  await release()
  await waiting
  assert.deepEqual([answers[0].ok, answers[0].code], [false, 'busy'])
  assert.equal(root.children.has('late.txt'), false)
})

test('a read already running when the owner taps Stop sends none of its contents', async () => {
  let finish
  hostStore().access.set('edit', { run: () => new Promise(resolve => { finish = resolve }) })
  const running = handleSharedFolderCommand(command('edit', 'read', { length: 5 }))
  await new Promise(resolve => setImmediate(resolve))
  hostStore().records.get('edit').paused = true
  finish({ text: 'secret' })
  await running
  assert.deepEqual([answers[0].ok, answers[0].code, answers[0].result], [false, 'paused', undefined])
  assert.ok(!JSON.stringify(answers[0]).includes('secret'))
})

test('binning two same-named files at once keeps both in the bin', async () => {
  const root = new FakeDirectoryHandle('Downloads')
  root.addFolder('a').addFile('x.txt', 'from a')
  root.addFolder('b').addFile('x.txt', 'from b')
  editFolder(root)
  await Promise.all(['a', 'b'].map(folder => handleSharedFolderCommand(
    command('edit', 'bin', { commandId: `c-bin-${folder}`, path: `${folder}/x.txt` }),
  )))
  const bin = root.children.get('Möbius bin')
  assert.deepEqual([...bin.children.keys()].sort(), ['x (2).txt', 'x.txt'])
})

function withTimers(run) {
  mock.timers.enable({ apis: ['setTimeout'] })
  try {
    return run()
  } finally {
    mock.timers.reset()
  }
}

const working = extra => ({ type: 'shared_folder_activity', commands: 1, folderId: 'f9', folderName: 'Photos', ...extra })

test('the pill names the device when the agent works in a folder on another one', () => {
  showSharedFolderActivity(working({ deviceId: 'the-laptop', deviceLabel: 'Chrome on Mac' }))
  assert.deepEqual(hostStore().state.activity, { folderId: 'f9', folderName: 'Photos', deviceLabel: 'Chrome on Mac' })
  showSharedFolderActivity(working({ deviceId: deviceId(), deviceLabel: 'Chrome on Mac' }))
  assert.equal(hostStore().state.activity.deviceLabel, null)
})

test('the pill stays while commands run anywhere and leaves a few seconds after', () => withTimers(() => {
  showSharedFolderActivity(working({ deviceId: 'the-laptop' }))
  mock.timers.tick(30_000)
  assert.equal(hostStore().state.activity?.folderName, 'Photos')
  showSharedFolderActivity({ type: 'shared_folder_activity', commands: 0 })
  assert.equal(hostStore().state.activity?.folderName, 'Photos')
  mock.timers.tick(6_000)
  assert.equal(hostStore().state.activity, null)
}))

test('a pill whose end was lost with the connection still goes away', () => withTimers(() => {
  showSharedFolderActivity(working({ deviceId: 'the-laptop' }))
  mock.timers.tick(45_000)
  assert.equal(hostStore().state.activity, null)
  showSharedFolderActivity(working({ deviceId: 'the-laptop' }))
  void setSharedFolderSubscription(null)
  assert.equal(hostStore().state.activity, null)
}))

test('Stop on another device pauses the folders this browser holds, for good', async () => {
  await applyRemotePause({ type: 'shared_folder_pause', folderIds: ['edit', 'on-another-device'] })
  assert.equal(hostStore().records.get('edit').paused, true)
  assert.equal(hostStore().records.get('read').paused, false)
  await handleSharedFolderCommand(command('edit', 'read', { length: 5 }))
  assert.deepEqual([answers[0].ok, answers[0].code], [false, 'paused'])
})

test('a change arriving while a remote Stop is still being saved finds every named folder paused', async () => {
  const store = hostStore()
  // The target is not the first folder Stop reaches.
  store.records = new Map([['read', store.records.get('read')], ['edit', store.records.get('edit')]])
  const root = new FakeDirectoryHandle('Downloads')
  editFolder(root)
  const pausing = applyRemotePause({ type: 'shared_folder_pause', folderIds: ['read', 'edit'] })
  await handleSharedFolderCommand(command('edit', 'write', { path: 'after-stop.txt', content: 'x' }))
  await pausing
  assert.deepEqual([answers[0].ok, answers[0].code], [false, 'paused'])
  assert.equal(root.children.has('after-stop.txt'), false)
})

test('Stop pauses this browser and asks every other device to stop', async () => {
  await stopEverywhere()
  assert.deepEqual(pauses, [{}])
  assert.ok([...hostStore().records.values()].every(record => record.paused))
})

test('Stop still pauses this browser when the server cannot be reached', async () => {
  serverReachable = false
  await stopEverywhere()
  assert.ok([...hostStore().records.values()].every(record => record.paused))
})
