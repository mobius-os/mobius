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
// fire when a or b changes by identity. A function returned by an effect
// is retained as its cleanup and invoked before that effect fires again,
// like React — a hook that owns an external subscription (an
// IntersectionObserver) cannot be tested honestly without that teardown.
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

export function useReducer(reducer, initialArg, init) {
  const i = _slotIndex++
  if (_slots[i] === undefined) {
    const slot = {
      value: typeof init === 'function' ? init(initialArg) : initialArg,
      dispatch: null,
    }
    slot.dispatch = (action) => {
      slot.value = reducer(slot.value, action)
      _rerender()
    }
    _slots[i] = slot
  }
  const slot = _slots[i]
  return [slot.value, slot.dispatch]
}

export function useRef(initial) {
  const i = _slotIndex++
  if (_slots[i] === undefined) {
    _slots[i] = { current: initial }
  }
  return _slots[i]
}

export function useCallback(fn, deps) {
  const i = _slotIndex++
  if (_slots[i] === undefined) {
    _slots[i] = { fn, deps }
  } else if (
    deps === undefined
    || !Array.isArray(_slots[i].deps)
    || deps.length !== _slots[i].deps.length
    || deps.some((dep, idx) => !Object.is(dep, _slots[i].deps[idx]))
  ) {
    _slots[i].fn = fn
    _slots[i].deps = deps
  }
  return _slots[i].fn
}

export function useMemo(factory, deps) {
  const i = _slotIndex++
  if (_slots[i] === undefined) {
    _slots[i] = { value: factory(), deps }
  } else if (
    deps === undefined
    || !Array.isArray(_slots[i].deps)
    || deps.length !== _slots[i].deps.length
    || deps.some((dep, idx) => !Object.is(dep, _slots[i].deps[idx]))
  ) {
    _slots[i].value = factory()
    _slots[i].deps = deps
  }
  return _slots[i].value
}

export function useSyncExternalStore(subscribe, getSnapshot) {
  const snapshotRef = useRef(getSnapshot())
  snapshotRef.current = getSnapshot()
  useEffect(() => subscribe(() => {
    const next = getSnapshot()
    if (Object.is(next, snapshotRef.current)) return
    snapshotRef.current = next
    _rerender()
  }), [getSnapshot, subscribe])
  return snapshotRef.current
}

// Faithful to React: the callback runs SYNCHRONOUSLY — startTransition only
// marks the state updates it schedules as non-urgent, it does not defer the
// callback itself. This shim has no concurrent scheduler, so running fn() inline
// (its updates commit through the same synchronous _rerender path as any other)
// is the honest model for the hooks under test. Without this export, a hook that
// imports `startTransition` (useNavigation) fails to load: the shimmed `react`
// module has no such named export.
export function startTransition(fn) {
  fn()
}

function _scheduleEffect(fn, deps) {
  const i = _slotIndex++
  if (_slots[i] === undefined) {
    _slots[i] = { prevDeps: _UNSET, cleanup: null }
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
  if (shouldFire) _pendingEffects.push({ slot, fn })
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
  // only mutate refs or re-set the same state inside effects, so the
  // recursion concern is theoretical. If you hit it, gate _flushEffects
  // behind a depth counter or move setState callers to
  // useEffect-with-deferred-flush.
  const toRun = _pendingEffects.splice(0)
  for (const { slot, fn } of toRun) {
    // Tear down the previous subscription before re-establishing it, so a
    // re-fire cannot leave two live observers behind (React's order too).
    if (typeof slot.cleanup === 'function') slot.cleanup()
    const cleanup = fn()
    slot.cleanup = typeof cleanup === 'function' ? cleanup : null
  }
}

/**
 * Run a hook function as if React were mounting it. Returns a
 * { result, rerender, unmount } triple; `result.current` reflects the
 * latest return value, `rerender(...args)` re-invokes the hook with
 * fresh arguments while preserving slot state, and `unmount()` runs
 * every retained effect cleanup like React's teardown.
 *
 * Effects (useLayoutEffect / useEffect) registered during the hook
 * call are flushed synchronously after hookFn returns, so callers
 * can assert on ref values that effects set without an `act` wrapper.
 *
 * `unmount` exists because a hook that owns an external subscription
 * (an IntersectionObserver) has a teardown path that no dep change can
 * reach: without it, "the observer is disconnected when the component
 * goes away" is untestable and a leaked observer looks identical to a
 * released one.
 */
export function renderHook(hookFn, ...initialArgs) {
  __reset()
  const result = { current: undefined }
  let currentArgs = initialArgs
  let unmounted = false
  function run() {
    if (unmounted) return
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
    unmount: () => {
      if (unmounted) return
      unmounted = true
      // React tears effects down in the order they were registered.
      for (const slot of _slots) {
        if (slot && typeof slot.cleanup === 'function') {
          slot.cleanup()
          slot.cleanup = null
        }
      }
    },
  }
}
