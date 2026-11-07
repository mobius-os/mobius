/* The chat's one footer copies the agent's whole stretch, even when it began
   before the loaded history page: the missing start is read page by page. */
import assert from 'node:assert/strict'
import test from 'node:test'
import { readStretchPrefix } from '../useStretchPrefix.js'
import { stretchTail } from '../../messageCopy.js'

const owner = content => ({ role: 'user', content })
const reply = content => ({ role: 'assistant', blocks: [{ type: 'text', content }] })
const carrier = { role: 'user', content: 'answer', hidden: true, kind: 'continuation' }

test('a page’s stretch tail runs back past hidden carriers to a visible owner row', () => {
  assert.deepEqual(stretchTail([reply('old'), owner('Go'), reply('a'), carrier, reply('b')]),
    { rows: [reply('a'), carrier, reply('b')], complete: true })
  assert.deepEqual(stretchTail([reply('a'), carrier]), { rows: [reply('a'), carrier], complete: false })
  assert.deepEqual(stretchTail([owner('Go')]), { rows: [], complete: true })
})

test('the prefix reads earlier pages until the stretch’s start, oldest first', async () => {
  const pages = {
    40: { offset: 20, messages: [reply('two'), carrier, reply('three')] },
    20: { offset: 0, messages: [owner('Go'), reply('one'), carrier] },
  }
  const asked = []
  const markdown = await readStretchPrefix(before => { asked.push(before); return pages[before] }, 40)
  assert.deepEqual(asked, [40, 20])
  assert.equal(markdown, 'one\n\ntwo\n\nthree')
})

test('the prefix stops at the first page and never re-reads a page that does not advance', async () => {
  let calls = 0
  const markdown = await readStretchPrefix(() => {
    calls += 1
    return { offset: 20, messages: [reply('loop')] }
  }, 20)
  assert.equal(calls, 1)
  assert.equal(markdown, 'loop')
})
