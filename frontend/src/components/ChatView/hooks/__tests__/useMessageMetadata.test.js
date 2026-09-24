/* Transcript identity bounds copy serialization during unrelated parent renders. */
import test from 'node:test'
import assert from 'node:assert/strict'
import useMessageMetadata from '../useMessageMetadata.js'
import { renderHook } from './react-hook-shim.mjs'

test('stream and composer rerenders reuse settled copy text until the transcript changes', () => {
  let reads = 0
  const messages = [{
    role: 'assistant',
    blocks: [{ type: 'text', get content() { reads += 1; return 'Saved answer' } }],
  }]
  const { result, rerender, unmount } = renderHook(useMessageMetadata, messages)
  const original = result.current
  const initialReads = reads
  assert.ok(initialReads > 0)
  assert.equal(original[0].copyText, 'Saved answer')
  for (let i = 0; i < 25; i += 1) rerender(messages)
  assert.equal(reads, initialReads)
  assert.equal(result.current, original)

  rerender([...messages, { role: 'assistant', content: 'Next answer' }])
  assert.notEqual(result.current, original)
  assert.deepEqual(result.current.map(row => row.alwaysVisible), [false, true])
  assert.equal(result.current[1].copyText, 'Next answer')

  rerender([{ role: 'assistant', content: 'Replaced answer' }])
  assert.equal(result.current[0].copyText, 'Replaced answer')
  unmount()
})
