import { test } from 'node:test'
import assert from 'node:assert/strict'
import {
  back,
  closeFile,
  filterEntries,
  finderCrumbs,
  initFinder,
  joinPath,
  openFile,
  openFolder,
  parentPath,
} from '../projectFinderNav.js'

test('parentPath walks one level up and bottoms out at root', () => {
  assert.equal(parentPath('a/b/c'), 'a/b')
  assert.equal(parentPath('a'), '')
  assert.equal(parentPath(''), '')
})

test('joinPath drops empty segments and joins a clean dir + name', () => {
  assert.equal(joinPath('', 'a.md'), 'a.md')
  assert.equal(joinPath('src', 'a.md'), 'src/a.md')
  assert.equal(joinPath('src/lib', 'a.md'), 'src/lib/a.md')
})

test('finderCrumbs is home-relative with cumulative paths', () => {
  assert.deepEqual(finderCrumbs('My project', ''), [{ label: 'My project', path: '' }])
  assert.deepEqual(finderCrumbs('My project', 'src/lib'), [
    { label: 'My project', path: '' },
    { label: 'src', path: 'src' },
    { label: 'lib', path: 'src/lib' },
  ])
})

test('forward steps push the back-stack; a real change signals a history push', () => {
  let state = initFinder()
  assert.equal(state.stack.length, 0)

  let step = openFolder(state, 'src')
  assert.equal(step.pushed, true)
  state = step.state
  assert.equal(state.current.path, 'src')
  assert.equal(state.stack.length, 1)

  step = openFile(state, 'src/a.md')
  assert.equal(step.pushed, true)
  state = step.state
  assert.deepEqual(state.current, { path: 'src', selected: 'src/a.md' })
  assert.equal(state.stack.length, 2)
})

test('navigating to the current location is a no-op (no duplicate history entry)', () => {
  let state = openFolder(initFinder(), 'src').state
  const step = openFolder(state, 'src')
  assert.equal(step.pushed, false)
  assert.equal(step.state, state)
})

// The browser Back button (in-tab): every forward step is retraced one at a
// time, and the extra Back past home leaves the Finder (popped === false).
test('browser Back walks the trail back in-tab, then releases at home', () => {
  let state = initFinder()
  state = openFolder(state, 'src').state          // home -> src
  state = openFolder(state, 'src/lib').state       // src -> src/lib
  state = openFile(state, 'src/lib/x.js').state    // inspect x.js
  assert.equal(state.stack.length, 3)

  let popped
  ;({ state, popped } = back(state))
  assert.equal(popped, true)
  assert.deepEqual(state.current, { path: 'src/lib', selected: null })

  ;({ state, popped } = back(state))
  assert.equal(popped, true)
  assert.deepEqual(state.current, { path: 'src', selected: null })

  ;({ state, popped } = back(state))
  assert.equal(popped, true)
  assert.deepEqual(state.current, { path: '', selected: null })

  // Nothing left on the stack: this Back must bubble out of the Finder.
  ;({ state, popped } = back(state))
  assert.equal(popped, false)
})

test('closing an inspected file is a forward step back to the folder', () => {
  let state = openFile(openFolder(initFinder(), 'src').state, 'src/a.md').state
  const step = closeFile(state)
  assert.equal(step.pushed, true)
  assert.deepEqual(step.state.current, { path: 'src', selected: null })
})

test('filterEntries matches names case-insensitively, prefixes first', () => {
  const entries = [
    { name: 'core.md' },
    { name: 'readme.md' },
    { name: 'assets' },
  ]
  assert.deepEqual(
    filterEntries(entries, 're').map(e => e.name),
    ['readme.md', 'core.md'],
  )
  assert.deepEqual(filterEntries(entries, ''), entries)
  assert.deepEqual(filterEntries(entries, 'zzz'), [])
})
