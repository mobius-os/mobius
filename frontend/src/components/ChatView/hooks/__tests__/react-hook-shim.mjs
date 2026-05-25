// Minimal React hooks shim for unit-testing pure hook logic with
// `node --test`. Re-implements just enough of React's hook contract
// (call-order indexing, stable refs across re-renders, useState
// updater functions, layout effects) to exercise the hooks in this
// directory.
//
// This is intentionally NOT a general-purpose React test renderer —
// we don't need rendering, batching, or concurrent mode. We just need
// useState / useRef / useCallback / useLayoutEffect / useEffect to
// behave like React during synchronous test invocations of a hook.
//
// Effect model: useLayoutEffect (and useEffect, aliased identically
// for DOM-free tests) queues its fn during the hook call. renderHook's
// run() flushes queued effects synchronously after hookFn() returns —
// matching React's "layout effects fire after commit" timing without a
// real DOM commit cycle. Dep semantics follow React: Object.is per
// element; undefined deps = fire every render; [] = fire once; [a,b] =
// fire when a or b changes by identity.
//
// Why this instead of @testing-library/react-hooks: zero new
// devDependencies, fits the Möbius preference for keeping the
// frontend toolchain minimal (Vite defaults + Playwright).

const _UNSET = Symbol('unset')

let _slots = []
let _slotIndex = 0
let _rerender = () => {}
let _pendingEffects = []

export function __reset() {
  _slots = []
  _slotIndex = 0
  _pendingEffects = []
}

export function __setRerender(fn) {
  _rerender = fn
}

export function useState(initial) {
  const i = _slotIndex++
  if (_slots[i] === undefined) {
    _slots[i] = {
      value: typeof initial === 'function' ? initial() : initial,
    }
  }
  const slot = _slots[i]
  const setter = (next) => {
    slot.value = typeof next === 'function' ? next(slot.value) : next
    _rerender()
  }
  return [slot.value, setter]
}

export function useRef(initial) {
  const i = _slotIndex++
  if (_slots[i] === undefined) {
    _slots[i] = { current: initial }
  }
  return _slots[i]
}

export function useCallback(fn /*, deps */) {
  // The hooks under test rely on useCallback for identity stability
  // but our tests don't observe identity across re-renders. Returning
  // the function as-is preserves call semantics.
  const i = _slotIndex++
  if (_slots[i] === undefined) {
    _slots[i] = { fn }
  } else {
    _slots[i].fn = fn
  }
  return _slots[i].fn
}

function _scheduleEffect(fn, deps) {
  const i = _slotIndex++
  if (_slots[i] === undefined) {
    _slots[i] = { prevDeps: _UNSET }
  }
  const slot = _slots[i]
  // Fire on: first render (prevDeps === _UNSET), no dep array
  // (undefined → fire every render), or any dep changed by identity.
  const shouldFire =
    slot.prevDeps === _UNSET ||
    deps === undefined ||
    !Array.isArray(slot.prevDeps) ||
    deps.length !== slot.prevDeps.length ||
    deps.some((d, idx) => !Object.is(d, slot.prevDeps[idx]))
  slot.prevDeps = deps === undefined ? _UNSET : deps
  if (shouldFire) _pendingEffects.push(fn)
}

// useLayoutEffect and useEffect collapse to the same scheduling here:
// without a real commit/paint cycle to distinguish them, both fire
// synchronously after the hook returns. The hooks in this dir don't
// observe the timing difference; if a future hook does, split them.
export function useLayoutEffect(fn, deps) {
  _scheduleEffect(fn, deps)
}

export function useEffect(fn, deps) {
  _scheduleEffect(fn, deps)
}

function _flushEffects() {
  // Drain in registration order. Effects that call setState would
  // re-trigger _rerender → run → another flush; the hooks tested here
  // only mutate refs inside effects, so the recursion concern is
  // theoretical. If you hit it, gate _flushEffects behind a depth
  // counter or move setState callers to useEffect-with-deferred-flush.
  const toRun = _pendingEffects.splice(0)
  for (const fn of toRun) fn()
}

/**
 * Run a hook function as if React were mounting it. Returns a
 * { result, rerender } pair; `result.current` reflects the latest
 * return value, and `rerender(...args)` re-invokes the hook with
 * fresh arguments while preserving slot state.
 *
 * Effects (useLayoutEffect / useEffect) registered during the hook
 * call are flushed synchronously after hookFn returns, so callers
 * can assert on ref values that effects set without an `act` wrapper.
 */
export function renderHook(hookFn, ...initialArgs) {
  __reset()
  const result = { current: undefined }
  let currentArgs = initialArgs
  function run() {
    _slotIndex = 0
    result.current = hookFn(...currentArgs)
    _flushEffects()
  }
  __setRerender(run)
  run()
  return {
    result,
    rerender: (...nextArgs) => {
      currentArgs = nextArgs.length > 0 ? nextArgs : currentArgs
      run()
    },
  }
}
