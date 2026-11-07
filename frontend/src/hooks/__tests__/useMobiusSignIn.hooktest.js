/* In-place Möbius sign-in state: the approved retry stays in memory, and cancel cannot abandon a completion already under way. */
import assert from 'node:assert/strict'
import { test } from 'node:test'
import { setTimeout as tick } from 'node:timers/promises'
import { renderHook } from '../../components/ChatView/hooks/__tests__/react-hook-shim.mjs'
import useMobiusSignIn from '../useMobiusSignIn.js'

const identity = { account_mode: 'linked', account_unavailable: false }
const payload = { code: 'c'.repeat(40), state: 's'.repeat(40), attempt: 'a'.repeat(20) }

test('a confirmed sign-in hands the linked identity to the page', async t => {
  const signedIn = []
  const view = renderHook(props => useMobiusSignIn(props), {
    onSignedIn: next => signedIn.push(next),
    link: async () => ({ status: 'linked', identity }),
  })
  t.after(() => view.unmount())
  view.result.current.begin('google')
  assert.equal(view.result.current.pending, 'google')
  await tick(5)
  assert.deepEqual(signedIn, [identity])
  assert.equal(view.result.current.pending, '')
  assert.equal(view.result.current.error, '')
})

test('a sign-in confirmed after the page closed still reaches the account cache', async t => {
  const signedIn = []
  let approve
  const view = renderHook(props => useMobiusSignIn(props), {
    onSignedIn: next => signedIn.push(next),
    link: ({ onCompleting }) => new Promise(resolve => {
      approve = () => { onCompleting(); resolve({ status: 'linked', identity }) }
    }),
  })
  view.result.current.begin('google')
  // The owner closes the guide or leaves Settings during "Finishing sign-in…".
  view.unmount()
  approve()
  await tick(5)
  assert.deepEqual(signedIn, [identity])
})

test('an unconfirmed completion offers one in-memory retry that finishes the link', async t => {
  const signedIn = []
  const completions = []
  const view = renderHook(props => useMobiusSignIn(props), {
    onSignedIn: next => signedIn.push(next),
    link: async () => ({ status: 'unconfirmed', completion: payload, message: 'Retry to finish signing in.' }),
    complete: async completion => { completions.push(completion); return { status: 'linked', identity } },
  })
  t.after(() => view.unmount())
  view.result.current.begin('apple')
  await tick(5)
  assert.equal(view.result.current.canRetry, true)
  assert.match(view.result.current.error, /Retry/)
  view.result.current.retry()
  assert.equal(view.result.current.pending, 'complete')
  await tick(5)
  assert.deepEqual(completions, [payload])
  assert.deepEqual(signedIn, [identity])
  assert.equal(view.result.current.canRetry, false)
})

test('cancel aborts a waiting sign-in without an error, but not one already completing', async t => {
  let signal
  let approve
  const view = renderHook(props => useMobiusSignIn(props), {
    link: ({ signal: next, onCompleting }) => {
      signal = next
      return new Promise((resolve, reject) => {
        next.addEventListener('abort', () => reject(new Error('Sign-in cancelled.')))
        approve = () => { onCompleting(); setTimeout(() => resolve({ status: 'linked', identity }), 5) }
      })
    },
  })
  t.after(() => view.unmount())
  view.result.current.begin('google')
  view.result.current.cancel()
  await tick(5)
  assert.equal(signal.aborted, true)
  assert.equal(view.result.current.error, '')
  assert.equal(view.result.current.pending, '')

  view.result.current.begin('google')
  approve()
  assert.equal(view.result.current.pending, 'complete')
  view.result.current.cancel()
  assert.equal(signal.aborted, false)
  await tick(10)
  assert.equal(view.result.current.pending, '')
})
