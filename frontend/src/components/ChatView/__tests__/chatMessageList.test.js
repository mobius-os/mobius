import { test } from 'node:test'
import assert from 'node:assert/strict'

import { sameMessageList } from '../chatMessageList.js'

test('an earlier assistant completion is not hidden by an unchanged newer user row', () => {
  const prior = [
    { role: 'user', content: 'first', ts: 1, cid: 'q1' },
    {
      role: 'assistant',
      content: 'Opening update',
      blocks: [
        { type: 'text', content: 'Opening update' },
        { type: 'tool', tool: 'Bash', status: 'done', tool_use_id: 'tool-1' },
      ],
    },
    { role: 'user', content: 'next message', ts: 3, cid: 'q2' },
  ]
  const completed = [
    prior[0],
    {
      role: 'assistant',
      content: 'Opening update\n\nProgress update\n\nFinal reply',
      blocks: [
        { type: 'text', content: 'Opening update' },
        { type: 'tool', tool: 'Bash', status: 'done', tool_use_id: 'tool-1' },
        { type: 'text', content: 'Progress update' },
        { type: 'text', content: 'Final reply' },
      ],
    },
    prior[2],
  ]

  assert.equal(
    sameMessageList(prior, completed),
    false,
    'the mounted transcript must accept completed blocks before the latest row',
  )
})

test('identical rendered chat windows remain equal', () => {
  const rows = [
    { role: 'user', content: 'hello', ts: 1, cid: 'q1' },
    {
      role: 'assistant',
      content: 'hi',
      blocks: [{ type: 'text', content: 'hi' }],
    },
  ]
  assert.equal(sameMessageList(rows, rows), true)
  assert.equal(sameMessageList(rows, rows.map(row => ({ ...row }))), true)
})

test('earlier message metadata that changes rendering is not treated as equal', () => {
  const base = [
    {
      role: 'assistant', content: '', ts: 1,
      blocks: [{ type: 'error', message: 'Paused', resumable: false }],
    },
    { role: 'user', content: 'continue', ts: 2, cid: 'q2' },
  ]
  const resumable = [
    {
      ...base[0],
      blocks: [{ type: 'error', message: 'Paused', resumable: true }],
    },
    base[1],
  ]
  assert.equal(sameMessageList(base, resumable), false)
})
