/**
 * Unit tests for useBridgePartial.
 *
 * Run with:
 *   cd frontend && node --loader=./src/components/ChatView/hooks/__tests__/react-loader.mjs \
 *     --test src/components/ChatView/hooks/__tests__/useBridgePartial.test.js
 */
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { renderHook } from './react-hook-shim.mjs'
import useBridgePartial from '../useBridgePartial.js'

test('shouldBridge is true when running at mount and last-message ts matches', () => {
  const { result } = renderHook(useBridgePartial, {
    runningAtMount: true,
    lastMsgAtMount: { ts: 555, role: 'assistant' },
  })
  assert.equal(result.current.shouldBridge({ ts: 555 }), true)
})

test('shouldBridge is false after markBridged (one-shot)', () => {
  const { result } = renderHook(useBridgePartial, {
    runningAtMount: true,
    lastMsgAtMount: { ts: 555, role: 'assistant' },
  })
  assert.equal(result.current.shouldBridge({ ts: 555 }), true)
  result.current.markBridged()
  assert.equal(result.current.shouldBridge({ ts: 555 }), false)
})

test('shouldBridge is false when runningAtMount is false', () => {
  const { result } = renderHook(useBridgePartial, {
    runningAtMount: false,
    lastMsgAtMount: { ts: 555, role: 'assistant' },
  })
  assert.equal(result.current.shouldBridge({ ts: 555 }), false)
})

test('shouldBridge is false when lastMsgAtMount is null', () => {
  const { result } = renderHook(useBridgePartial, {
    runningAtMount: true,
    lastMsgAtMount: null,
  })
  assert.equal(result.current.shouldBridge({ ts: 1 }), false)
})

test('shouldBridge is false when the current last-ts differs from the captured ts', () => {
  // A new turn since mount has appended a fresh assistant message
  // with its own ts — the kept partial is no longer "last."
  const { result } = renderHook(useBridgePartial, {
    runningAtMount: true,
    lastMsgAtMount: { ts: 555, role: 'assistant' },
  })
  assert.equal(result.current.shouldBridge({ ts: 9999 }), false)
})

test('shouldBridge is FALSE when last message at mount was an error (parallel-agent be32e58)', () => {
  // be32e58 made errors persist as the LAST message in the chat.
  // The earlier role-based check ("last message is assistant")
  // would have bridged an error message into the next turn's
  // promote — corrupting both the error display and the partial.
  // ts-based gating must reject this: error role at mount means
  // no kept-partial-ts is captured, shouldBridge returns false
  // regardless of any subsequent currentLastMsg.ts.
  const { result } = renderHook(useBridgePartial, {
    runningAtMount: true,
    lastMsgAtMount: { ts: 555, role: 'error' },
  })
  assert.equal(result.current.shouldBridge({ ts: 555 }), false)
})

test('shouldBridge is FALSE when last message at mount was a system role', () => {
  const { result } = renderHook(useBridgePartial, {
    runningAtMount: true,
    lastMsgAtMount: { ts: 555, role: 'system' },
  })
  assert.equal(result.current.shouldBridge({ ts: 555 }), false)
})

test('shouldBridge is false when currentLastMsg is null/undefined', () => {
  const { result } = renderHook(useBridgePartial, {
    runningAtMount: true,
    lastMsgAtMount: { ts: 555, role: 'assistant' },
  })
  assert.equal(result.current.shouldBridge(null), false)
  assert.equal(result.current.shouldBridge(undefined), false)
})

test('the captured ts is sticky across re-renders with different args', () => {
  // The mount-time decision is the load-bearing one. A re-render
  // with new args (e.g. parent re-rendered with running=false
  // because state updated elsewhere) MUST NOT clear the captured
  // partial-ts mid-bridge.
  const { result, rerender } = renderHook(useBridgePartial, {
    runningAtMount: true,
    lastMsgAtMount: { ts: 555, role: 'assistant' },
  })
  assert.equal(result.current.shouldBridge({ ts: 555 }), true)
  rerender({ runningAtMount: false, lastMsgAtMount: null })
  assert.equal(result.current.shouldBridge({ ts: 555 }), true)
})

test('layout effect fires on mount: shouldBridge returns true with no prior interaction', () => {
  // Lock in the contract that the 9e9a516 fix added: capture runs
  // inside useLayoutEffect (not the render body). Before the shim
  // gained useLayoutEffect support, the hook errored on import; this
  // test confirms the effect now actually runs and captures the ts.
  // A regression that re-broke the layout-effect path would make
  // shouldBridge return false here.
  const { result } = renderHook(useBridgePartial, {
    runningAtMount: true,
    lastMsgAtMount: { role: 'assistant', ts: 100 },
  })
  assert.equal(result.current.shouldBridge({ ts: 100 }), true,
    'effect captured the kept-partial ts on mount')
  assert.equal(result.current.shouldBridge({ ts: 99 }), false,
    'a different ts does not match the captured one')
})

test('StrictMode double-invoke: capturedRef one-shot survives a second effect call', () => {
  // React StrictMode in dev double-invokes effects (mount → cleanup →
  // mount again). The capturedRef guard in useBridgePartial makes the
  // capture a one-shot so the second run is a no-op. We simulate the
  // "effect fires twice" behavior by rerendering with a fresh object
  // identity for lastMsgAtMount (same content, different reference →
  // dep array sees a change → effect re-fires).
  const args1 = {
    runningAtMount: true,
    lastMsgAtMount: { role: 'assistant', ts: 100 },
  }
  const args2 = {
    runningAtMount: true,
    lastMsgAtMount: { role: 'assistant', ts: 100 },
  }
  const { result, rerender } = renderHook(useBridgePartial, args1)
  assert.equal(result.current.shouldBridge({ ts: 100 }), true,
    'first invocation captures ts=100')
  rerender(args2)
  assert.equal(result.current.shouldBridge({ ts: 100 }), true,
    'second effect call keeps the captured ts (capturedRef guard held)')
  assert.equal(result.current.shouldBridge({ ts: 999 }), false,
    'capturedRef did not re-arm against a hypothetical new ts')
})
