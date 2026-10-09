import { test } from 'node:test'
import assert from 'node:assert/strict'
import { renderHook } from './react-hook-shim.mjs'
import { useStableImageReference } from '../../useToolImagePreview.js'

const shot = () => ({ kind: 'chat', chatId: 'c1', collection: 'media', filename: 'shot.png' })

test('an open image keeps its reference when a re-render rebuilds an equal one', () => {
  const first = shot()
  const { result, rerender } = renderHook(useStableImageReference, first)

  rerender(shot())

  assert.equal(result.current, first, 'an equal rebuilt reference must not reset the open preview')
})

test('a different picture replaces the kept reference', () => {
  const { result, rerender } = renderHook(useStableImageReference, shot())

  const other = { ...shot(), filename: 'other.png' }
  rerender(other)
  assert.equal(result.current, other)

  const refingerprinted = { ...other, expectedSha256: 'abc' }
  rerender(refingerprinted)
  assert.equal(result.current, refingerprinted)
})

test('an image that disappears and returns is a fresh reference', () => {
  const { result, rerender } = renderHook(useStableImageReference, shot())

  rerender(null)
  assert.equal(result.current, null)

  const back = shot()
  rerender(back)
  assert.equal(result.current, back)
})
