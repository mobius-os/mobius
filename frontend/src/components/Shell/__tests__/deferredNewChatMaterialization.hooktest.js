/* Verify recovery scheduling without replacing the allocator's network and reuse policy. */
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { renderHook } from '../../ChatView/hooks/__tests__/react-hook-shim.mjs'
import useDeferredNewChatMaterialization from '../useDeferredNewChatMaterialization.js'

function harness({ ready = false, viewMode = 'single', singleScreen = null } = {}) {
  const state = { ready }
  const creates = []
  const failures = []
  const invocations = []
  const workspaceStateRef = { current: { ws: { viewMode, singleScreen } } }
  const pendingNewChatRef = { current: { token: 1, candidateId: null } }
  const materializeRef = {
    current(pending) {
      invocations.push(pending.token)
      if (!state.ready) {
        // resolveNewChatId returns { reason: 'offline' }: no POST, landing kept.
        failures.push('offline')
        return
      }
      // A single authoritative create; then the slot is owned by the new row.
      creates.push(pending.token)
      pendingNewChatRef.current = null
      workspaceStateRef.current.ws = {
        viewMode: 'single',
        singleScreen: { kind: 'chat', id: `created-${pending.token}` },
      }
    },
  }
  const props = (overrides = {}) => ({
    pendingNewChatToken: 1,
    materializeNewChatRevision: 0,
    recoveryGeneration: 0,
    modeActive: false,
    modeTransition: false,
    viewMode: workspaceStateRef.current.ws.viewMode,
    singleScreen: workspaceStateRef.current.ws.singleScreen,
    workspaceStateRef,
    pendingNewChatRef,
    materializeRef,
    ...overrides,
  })
  return { state, creates, failures, invocations, workspaceStateRef, pendingNewChatRef, props }
}

test('defers offline with zero creates, then auto-creates exactly once on the readiness edge', () => {
  const h = harness({ ready: false })

  // Empty list resolved; scene idle; slot an empty single.
  const { rerender } = renderHook(useDeferredNewChatMaterialization, h.props())

  // Zero create requests before readiness — the landing is kept with its request.
  assert.deepEqual(h.creates, [], 'no chat is created while delivery is not ready')
  assert.deepEqual(h.failures, ['offline'], 'the deferred attempt recorded an offline landing')
  assert.equal(h.pendingNewChatRef.current?.token, 1, 'the pending request is retained for retry')
  assert.equal(h.workspaceStateRef.current.ws.singleScreen, null, 'the slot stays the New Chat landing')

  // Delivery readiness arrives: the store bumps recoveryGeneration (not-ready → ready).
  h.state.ready = true
  rerender(h.props({ recoveryGeneration: 1 }))

  assert.deepEqual(h.creates, [1], 'exactly one create fires after readiness')
  assert.deepEqual(h.invocations, [1, 1], 'the recovery edge re-entered the watcher for the SAME token')
  assert.equal(h.workspaceStateRef.current.ws.singleScreen.id, 'created-1', 'the new row now owns the slot')

  // A later render at the same generation must not create again (idempotent).
  rerender(h.props({ recoveryGeneration: 1 }))
  // A subsequent readiness edge finds no pending request and does nothing.
  rerender(h.props({ recoveryGeneration: 2 }))
  assert.deepEqual(h.creates, [1], 'no duplicate create across further edges or renders')
})

test('a readiness edge never navigates into a slot filled while offline', () => {
  const h = harness({ ready: false })
  const { rerender } = renderHook(useDeferredNewChatMaterialization, h.props())
  assert.deepEqual(h.creates, [], 'offline: nothing created')

  // While offline the owner navigated into an existing chat: the slot is filled.
  h.state.ready = true
  h.workspaceStateRef.current.ws = { viewMode: 'single', singleScreen: { kind: 'chat', id: 'existing-9' } }
  rerender(h.props({ recoveryGeneration: 1 }))

  assert.deepEqual(h.creates, [], 'the recovery edge does not create over a filled slot')
  assert.deepEqual(h.invocations, [1], 'materialize is NOT re-invoked once the slot is no longer an empty single')
  assert.equal(h.pendingNewChatRef.current, null, 'the stale request is dropped')
  assert.equal(h.workspaceStateRef.current.ws.singleScreen.id, 'existing-9', 'no navigation into another/undefined chat')
})

test('a readiness edge never replaces a newer owner intent that cleared the token', () => {
  const h = harness({ ready: false })
  const { rerender } = renderHook(useDeferredNewChatMaterialization, h.props())
  assert.deepEqual(h.creates, [], 'offline: nothing created')

  // An explicit owner New Chat presentation supersedes the deferral: it clears the
  // token (startUserNewChatPresentation → setPendingNewChatToken(0)) and the ref.
  h.state.ready = true
  h.pendingNewChatRef.current = null
  rerender(h.props({ pendingNewChatToken: 0, recoveryGeneration: 1 }))

  assert.deepEqual(h.creates, [], 'the recovery edge does not resurrect a superseded deferral')
  assert.deepEqual(h.invocations, [1], 'materialize is NOT invoked once the owner cleared the token')
})

test('an ordinary server error is not auto-retried on a render — only a fresh token retries', () => {
  const h = harness({ ready: true })
  // Ready, but the create fails server-side: keep pending, record error, do NOT clear
  // the slot. Readiness never dropped, so recoveryGeneration does not move. Override
  // the harness materialize with an error-returning contract.
  h.props().materializeRef.current = (pending) => {
    h.invocations.push(pending.token)
    h.failures.push('error') // resolveNewChatId → { reason: 'error' }; pending kept
  }

  const { rerender } = renderHook(useDeferredNewChatMaterialization, h.props())
  assert.deepEqual(h.failures, ['error'], 'the attempt failed with a server error')
  assert.deepEqual(h.invocations, [1], 'one attempt so far')

  // A plain re-render (no readiness edge, same token/revision) must not re-attempt:
  // an unresolved server error would otherwise spin a retry loop.
  rerender(h.props())
  assert.deepEqual(h.invocations, [1], 'no auto-retry of a server error without a proven recovery edge')

  // The manual retry affordance bumps the token AND the ref (requestEmptySingleNewChat)
  // — that, not a readiness signal, is what re-drives an error.
  h.pendingNewChatRef.current = { token: 2, candidateId: null }
  rerender(h.props({ pendingNewChatToken: 2 }))
  assert.deepEqual(h.invocations, [1, 2], 'a manual retry (new token) re-attempts materialization')
})

test('nothing materializes while the mode transition scene is still busy', () => {
  const h = harness({ ready: true })
  const { rerender } = renderHook(useDeferredNewChatMaterialization, h.props({ modeActive: true }))
  assert.deepEqual(h.invocations, [], 'a busy scene defers the attempt')

  rerender(h.props({ modeActive: false }))
  assert.deepEqual(h.creates, [1], 'the attempt runs once the scene idles')
})
