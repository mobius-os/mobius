import test from 'node:test'
import assert from 'node:assert/strict'

import { jsonOrThrow } from '../../api/client.js'

function refusal(detail, status = 422) {
  return new Response(JSON.stringify({ detail }), {
    status,
    headers: { 'Content-Type': 'application/json' },
  })
}

async function errorOf(response) {
  try {
    await jsonOrThrow(response, 'App update failed:')
  } catch (error) {
    return error
  }
  throw new Error('expected a refusal')
}

test('a compile refusal shows its reason and location', async () => {
  const error = await errorOf(refusal({
    code: 'compile_failed',
    message: 'Compilation failed.',
    stderr: '[PARSE_ERROR] Unexpected token\n   ╭─[ index.jsx:2:47 ]\n',
  }))

  assert.equal(
    error.message,
    'Compilation failed.\n[PARSE_ERROR] Unexpected token\n   ╭─[ index.jsx:2:47 ]',
  )
  assert.equal(error.code, 'compile_failed')
})

test('an ordinary typed refusal keeps its message', async () => {
  const error = await errorOf(refusal(
    { code: 'source_changed', message: 'App source must be checked out on main.' },
    409,
  ))

  assert.equal(error.message, 'App source must be checked out on main.')
})
