import { test } from 'node:test'
import assert from 'node:assert/strict'
import { BIN_FOLDER_NAME, pathSegments } from '../folderPaths.js'
import { createHandleFolder } from '../handleFolder.js'
import { createSnapshotFolder } from '../snapshotFolder.js'
import { browserName, deviceLabel } from '../folderStore.js'
import { shrinkImage } from '../fileReads.js'
import { FakeDirectoryHandle, fileText } from './fakeDirectory.js'

async function failsWith(promise, code) {
  await assert.rejects(promise, error => error.code === code)
}

function pickedFiles(entries) {
  return entries.map(([path, content]) => {
    const file = new File([content], path.split('/').at(-1), { lastModified: 1 })
    Object.defineProperty(file, 'webkitRelativePath', { value: path })
    return file
  })
}

test('a path can never leave the shared folder', () => {
  for (const path of ['..', 'a/../b', './a', 'a//b', 'a\\b', 'a\u0000b']) {
    assert.throws(() => pathSegments(path), error => error.code === 'invalid_path', path)
  }
  assert.deepEqual(pathSegments('/Photos/a.jpg'), ['Photos', 'a.jpg'])
  assert.deepEqual(pathSegments('', { allowRoot: true }), [])
  assert.throws(() => pathSegments(''), error => error.code === 'invalid_path')
})

test('remembered folder lists folders first and pages large folders', async () => {
  const root = new FakeDirectoryHandle('Downloads')
  root.addFile('b.txt', 'bee')
  root.addFile('a.txt', 'a')
  root.addFolder('Zips')
  const folder = createHandleFolder(root)
  const first = await folder.run({ op: 'list', path: '', limit: 2 })
  assert.deepEqual(first.entries.map(entry => entry.name), ['Zips', 'a.txt'])
  assert.equal(first.total, 3)
  assert.equal(first.nextCursor, 2)
  const rest = await folder.run({ op: 'list', path: '', cursor: 2 })
  assert.deepEqual(rest.entries, [{ name: 'b.txt', kind: 'file', size: 3, modified: 1_700_000_000_000 }])
})

test('read returns only the requested piece, from the start or the end', async () => {
  const root = new FakeDirectoryHandle('Logs')
  root.addFile('app.log', 'line one\nline two\nline three\n')
  const folder = createHandleFolder(root)
  const tail = await folder.run({ op: 'read', path: 'app.log', length: 11, fromEnd: true })
  assert.equal(tail.text, 'line three\n')
  assert.equal(tail.eof, true)
  const head = await folder.run({ op: 'read', path: 'app.log', offset: 0, length: 4 })
  assert.equal(head.text, 'line')
  assert.equal(head.eof, false)
  const encoded = await folder.run({ op: 'read', path: 'app.log', length: 4, encoding: 'base64' })
  assert.equal(encoded.base64, btoa('line'))
})

test('paged reads never split a letter at a page edge', async () => {
  // Two-, three- and four-byte characters, so pages end inside each kind.
  const text = 'Čevapi, ćevapčići, šljiva, žito, đak. Здраво. €5 😀 '.repeat(8)
  const root = new FakeDirectoryHandle('Notes')
  root.addFile('bs.txt', text)
  const folder = createHandleFolder(root)
  for (const length of [5, 7, 13]) {
    let offset = 0
    let joined = ''
    for (;;) {
      const page = await folder.run({ op: 'read', path: 'bs.txt', offset, length })
      assert.ok(!page.text.includes('�'), `page at ${offset} split a letter`)
      joined += page.text
      offset = page.offset + page.length
      if (page.eof) break
    }
    assert.equal(joined, text, `pages of ${length} bytes`)
  }
})

test('a read from the end starts on a whole letter', async () => {
  const root = new FakeDirectoryHandle('Notes')
  root.addFile('end.txt', 'ab čć')
  const tail = await createHandleFolder(root).run({ op: 'read', path: 'end.txt', length: 3, fromEnd: true })
  assert.equal(tail.text, 'ć')
  assert.equal(tail.offset, 5)
})

test('a binary file is not returned as garbled text', async () => {
  const root = new FakeDirectoryHandle('Pictures')
  root.addFile('photo.jpg', new Uint8Array([255, 216, 0, 1]))
  await failsWith(createHandleFolder(root).run({ op: 'read', path: 'photo.jpg' }), 'binary')
})

test('write creates files and refuses to replace one unless asked', async () => {
  const root = new FakeDirectoryHandle('Notes')
  const folder = createHandleFolder(root)
  assert.equal((await folder.run({ op: 'write', path: 'todo.txt', content: 'milk' })).created, true)
  await failsWith(folder.run({ op: 'write', path: 'todo.txt', content: 'eggs' }), 'exists')
  await folder.run({ op: 'write', path: 'todo.txt', content: 'eggs', overwrite: true })
  assert.equal(await fileText(root, 'todo.txt'), 'eggs')
  await failsWith(folder.run({ op: 'write', path: 'missing/todo.txt', content: 'x' }), 'not_found')
})

test('replacing a file keeps its old version in the Möbius bin', async () => {
  const root = new FakeDirectoryHandle('Notes')
  root.addFile('todo.txt', 'milk')
  const folder = createHandleFolder(root)
  const first = await folder.run({ op: 'write', path: 'todo.txt', content: 'eggs', overwrite: true })
  assert.deepEqual([first.created, first.binned], [false, `${BIN_FOLDER_NAME}/todo.txt`])
  const second = await folder.run({ op: 'write', path: 'todo.txt', content: 'bread', overwrite: true })
  assert.equal(second.binned, `${BIN_FOLDER_NAME}/todo (2).txt`)
  assert.equal(await fileText(root, 'todo.txt'), 'bread')
  assert.equal(await fileText(root, BIN_FOLDER_NAME, 'todo.txt'), 'milk')
  assert.equal(await fileText(root, BIN_FOLDER_NAME, 'todo (2).txt'), 'eggs')
  assert.equal((await folder.run({ op: 'write', path: 'new.txt', content: 'x' })).binned, undefined)
})

test('no change reaches into the Möbius bin, however its name is spelled', async () => {
  const root = new FakeDirectoryHandle('Downloads')
  root.addFolder(BIN_FOLDER_NAME).addFile('old.txt', 'binned')
  root.addFile('a.txt', 'a')
  const folder = createHandleFolder(root)
  // macOS stores "ö" as "o" plus a combining mark, and ignores letter case.
  for (const bin of [BIN_FOLDER_NAME, 'möbius BIN', 'Mo\u0308bius bin']) {
    await failsWith(folder.run({ op: 'write', path: `${bin}/old.txt`, content: 'x', overwrite: true }), 'in_bin')
    await failsWith(folder.run({ op: 'write', path: `${bin}/new.txt`, content: 'x' }), 'in_bin')
    await failsWith(folder.run({ op: 'move', path: `${bin}/old.txt`, to: 'back.txt' }), 'in_bin')
    await failsWith(folder.run({ op: 'move', path: 'a.txt', to: `${bin}/a.txt` }), 'in_bin')
    await failsWith(folder.run({ op: 'mkdir', path: `${bin}/sub` }), 'in_bin')
    await failsWith(folder.run({ op: 'bin', path: `${bin}/old.txt` }), 'already_in_bin')
  }
  assert.equal((await folder.run({ op: 'read', path: `${BIN_FOLDER_NAME}/old.txt` })).text, 'binned')
  assert.deepEqual([...root.children.get(BIN_FOLDER_NAME).children.keys()], ['old.txt'])
  assert.equal(await fileText(root, 'a.txt'), 'a')
})

test('hidden names can be read but never changed', async () => {
  const root = new FakeDirectoryHandle('Project')
  root.addFolder('.git').addFolder('hooks')
  root.addFile('.env', 'KEY=1')
  root.addFile('notes.txt', 'n')
  const folder = createHandleFolder(root)
  await failsWith(folder.run({ op: 'write', path: '.git/hooks/pre-commit', content: 'curl evil' }), 'hidden_name')
  await failsWith(folder.run({ op: 'write', path: '.env', content: 'KEY=2', overwrite: true }), 'hidden_name')
  await failsWith(folder.run({ op: 'mkdir', path: '.vscode' }), 'hidden_name')
  await failsWith(folder.run({ op: 'move', path: 'notes.txt', to: '.envrc' }), 'hidden_name')
  await failsWith(folder.run({ op: 'move', path: '.env', to: 'env.txt' }), 'hidden_name')
  await failsWith(folder.run({ op: 'bin', path: '.env' }), 'hidden_name')
  assert.equal((await folder.run({ op: 'read', path: '.env' })).text, 'KEY=1')
  assert.deepEqual([...root.children.keys()].sort(), ['.env', '.git', 'notes.txt'])
  assert.deepEqual([...root.children.get('.git').children.get('hooks').children.keys()], [])
})

test('move renames a file and refuses to overwrite or move whole folders', async () => {
  const root = new FakeDirectoryHandle('Downloads')
  root.addFile('scan.pdf', 'pdf')
  root.addFile('taken.pdf', 'other')
  root.addFolder('Done')
  const folder = createHandleFolder(root)
  await folder.run({ op: 'move', path: 'scan.pdf', to: 'Done/invoice.pdf' })
  assert.equal(await fileText(root, 'Done', 'invoice.pdf'), 'pdf')
  assert.equal(root.children.has('scan.pdf'), false)
  await failsWith(folder.run({ op: 'move', path: 'taken.pdf', to: 'Done/invoice.pdf' }), 'exists')
  await failsWith(folder.run({ op: 'move', path: 'Done', to: 'Archive' }), 'folder_move_unsupported')
})

test('move falls back to copy then remove when the browser cannot move local files', async () => {
  const root = new FakeDirectoryHandle('Downloads', { withMove: false })
  root.addFile('big.mov', 'video bytes')
  await createHandleFolder(root).run({ op: 'move', path: 'big.mov', to: 'clip.mov' })
  assert.equal(await fileText(root, 'clip.mov'), 'video bytes')
  assert.equal(root.children.has('big.mov'), false)
})

test('a copy-and-remove move keeps an original that changed during the copy', async () => {
  const root = new FakeDirectoryHandle('Downloads', { withMove: false })
  const source = root.addFile('notes.txt', 'version one')
  const readFile = source.getFile.bind(source)
  let reads = 0
  // Another app saves the file after the copy has read it.
  source.getFile = async () => {
    reads += 1
    if (reads === 2) source.modified += 1
    return readFile()
  }
  await failsWith(
    createHandleFolder(root).run({ op: 'move', path: 'notes.txt', to: 'kept.txt' }),
    'changed_during_move',
  )
  assert.equal(root.children.has('notes.txt'), true)
  assert.equal(await fileText(root, 'kept.txt'), 'version one')
})

test('a copied file whose original cannot be removed says that both exist', async () => {
  const root = new FakeDirectoryHandle('Downloads', { withMove: false })
  root.addFile('locked.docx', 'report')
  const removeEntry = root.removeEntry.bind(root)
  root.removeEntry = async name => {
    if (name !== 'locked.docx') return removeEntry(name)
    const error = new Error('in use')
    error.name = 'NoModificationAllowedError'
    throw error
  }
  const folder = createHandleFolder(root)
  await failsWith(folder.run({ op: 'move', path: 'locked.docx', to: 'report.docx' }), 'original_kept')
  assert.equal(await fileText(root, 'report.docx'), 'report')
  assert.equal(await fileText(root, 'locked.docx'), 'report')
  await failsWith(folder.run({ op: 'bin', path: 'locked.docx' }), 'original_kept')
  assert.equal(await fileText(root, BIN_FOLDER_NAME, 'locked.docx'), 'report')
})

test('bin moves a file into the Möbius bin, never deleting it', async () => {
  const root = new FakeDirectoryHandle('Downloads')
  root.addFile('old.txt', 'first')
  const folder = createHandleFolder(root)
  assert.equal((await folder.run({ op: 'bin', path: 'old.txt' })).to, `${BIN_FOLDER_NAME}/old.txt`)
  root.addFile('old.txt', 'second')
  assert.equal((await folder.run({ op: 'bin', path: 'old.txt' })).to, `${BIN_FOLDER_NAME}/old (2).txt`)
  assert.equal(await fileText(root, BIN_FOLDER_NAME, 'old.txt'), 'first')
  await failsWith(folder.run({ op: 'bin', path: `${BIN_FOLDER_NAME}/old.txt` }), 'already_in_bin')
})

test('mkdir creates a folder once and reports an existing one', async () => {
  const root = new FakeDirectoryHandle('Projects')
  const folder = createHandleFolder(root)
  assert.equal((await folder.run({ op: 'mkdir', path: 'Reports' })).created, true)
  assert.equal((await folder.run({ op: 'mkdir', path: 'Reports' })).created, false)
})

test('a lost browser permission becomes an owner-actionable code', async () => {
  const root = new FakeDirectoryHandle('Downloads', { denied: true })
  await failsWith(createHandleFolder(root).run({ op: 'read', path: 'a.txt' }), 'needs_permission')
})

test('a one-visit folder reads files but refuses every change', async () => {
  const folder = createSnapshotFolder(pickedFiles([
    ['Trip/notes.txt', 'packing list'],
    ['Trip/Photos/1.jpg', 'jpeg'],
  ]))
  assert.equal(folder.rootName, 'Trip')
  const listed = await folder.run({ op: 'list', path: '' })
  assert.deepEqual(listed.entries.map(entry => `${entry.kind}:${entry.name}`), ['folder:Photos', 'file:notes.txt'])
  assert.equal((await folder.run({ op: 'read', path: 'notes.txt', length: 7 })).text, 'packing')
  assert.equal((await folder.run({ op: 'info', path: 'Photos' })).entries, 1)
  await failsWith(folder.run({ op: 'read', path: 'Photos' }), 'wrong_kind')
  await failsWith(folder.run({ op: 'read', path: 'nope.txt' }), 'not_found')
  for (const op of ['write', 'move', 'mkdir', 'bin']) {
    await failsWith(folder.run({ op, path: 'notes.txt', to: 'x.txt', content: 'x' }), 'read_only')
  }
})

test('device label names the browser and the system', () => {
  const on = userAgent => ({ userAgent })
  assert.equal(deviceLabel(on('Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/141.0 Safari/537.36')), 'Chrome on Mac')
  assert.equal(deviceLabel(on('Mozilla/5.0 (iPhone; CPU iPhone OS 18_4 like Mac OS X) AppleWebKit/605.1.15 Version/18.4 Mobile/15E148 Safari/604.1')), 'Safari on iPhone')
  assert.equal(deviceLabel(on('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/141.0 Safari/537.36 Edg/141.0')), 'Edge on Windows')
  assert.equal(deviceLabel(on('Mozilla/5.0 (Linux; Android 15) AppleWebKit/537.36 Chrome/141.0 Mobile Safari/537.36')), 'Chrome on Android')
})

test('Brave is named Brave even though it reports a Chrome user agent', () => {
  const brave = { brave: {}, userAgent: 'Mozilla/5.0 (Linux; Android 15) AppleWebKit/537.36 Chrome/141.0 Mobile Safari/537.36' }
  assert.equal(browserName(brave), 'Brave')
  assert.equal(deviceLabel(brave), 'Brave on Android')
  assert.equal(browserName({ userAgent: '' }), 'This browser')
})

function withFakeImageDecoder(width, height, run) {
  const saved = { createImageBitmap: globalThis.createImageBitmap, OffscreenCanvas: globalThis.OffscreenCanvas }
  const drawn = []
  globalThis.createImageBitmap = async () => ({ width, height, close() {} })
  globalThis.OffscreenCanvas = class {
    constructor(w, h) { this.width = w; this.height = h }
    getContext() { return { fillRect() {}, drawImage: (_, x, y, w, h) => drawn.push([w, h]) } }
    async convertToBlob() { return new Blob([new Uint8Array(300_000)], { type: 'image/jpeg' }) }
  }
  return run(drawn).finally(() => Object.assign(globalThis, saved))
}

test('a big phone photo is shrunk on the device to the AI size limit', async () => {
  await withFakeImageDecoder(4032, 3024, async drawn => {
    const photo = new File([new Uint8Array(5_000_000)], 'IMG_1.jpg', { type: 'image/jpeg' })
    const result = await shrinkImage(photo, 'IMG_1.jpg', 2576)
    assert.deepEqual([result.width, result.height], [2576, 1932])
    assert.deepEqual(drawn, [[2576, 1932]])
    assert.equal(result.shrunk, true)
    assert.equal(result.type, 'image/jpeg')
    assert.equal(result.bytes, 300_000)
    assert.equal(result.size, 5_000_000)
  })
})

test('a small JPEG within the limit travels unchanged', async () => {
  await withFakeImageDecoder(800, 600, async drawn => {
    const photo = new File([new Uint8Array(1000)], 'small.jpg', { type: 'image/jpeg' })
    const result = await shrinkImage(photo, 'small.jpg', 2576)
    assert.equal(result.shrunk, false)
    assert.deepEqual(drawn, [])
    assert.equal(result.bytes, 1000)
  })
})

test('an image the browser cannot open gets an owner-actionable reason', async () => {
  const saved = globalThis.createImageBitmap
  globalThis.createImageBitmap = async () => { throw new Error('decode failed') }
  try {
    const heic = new File([new Uint8Array(10)], 'IMG_2.HEIC', { type: 'image/heic' })
    await failsWith(shrinkImage(heic, 'IMG_2.HEIC', 2576), 'unsupported_image')
  } finally {
    globalThis.createImageBitmap = saved
  }
})
