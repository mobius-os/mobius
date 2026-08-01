
// ── ChatSplit — window.mobius.split(opts) ────────────────────────────────────
//
// Manages a pill ↔ split ↔ full state machine for a mount element that holds
// both an app content area and the embedded chat panel. The helper owns all
// drag/touch/keyboard interaction and persists the ratio/state to sessionStorage
// so it survives tab navigation (not page refresh — sessionStorage is appropriate
// for UI transient state). CSS consumers read two custom properties the helper
// sets on `mount`:
//
//   --cs-content-h  (vertical / portrait mode)
//   --cs-content-w  (horizontal / wide mode)
//   data-split-state="pill|split|full"
//   data-orientation="portrait|side" (side when viewport ≥ 600px)
//
// Pure transition / threshold helpers are extracted into src/lib/splitHelper.js
// (testable under node:test) and mirrored as constants here.
//
// Keyboard-open behavior: the helper does NOT try to detect the keyboard. The
// browser compresses the visual viewport, which in turn shrinks `mount` — the
// CSS layout already adapts via the custom properties. No special handling needed.
//
// Wide viewports (≥ 600px): `data-orientation="side"` is applied; the helper
// reads `offsetWidth` rather than `offsetHeight` for drag calculations. The CSS
// consumer uses this attribute to switch between a column layout (portrait) and
// a row layout (side). Pill state is unavailable on wide viewports.

const SPLIT_WIDE_BP = 600
const SPLIT_FLICK_VEL = 0.4
const SPLIT_DEAD_ZONE = 24
const SPLIT_ARROW_STEP = 0.04

const SPLIT_STATES = { PILL: 'pill', SPLIT: 'split', FULL: 'full' }

function _splitClampRatio(ratio, totalPx, minContentPx, minChatPx) {
  if (totalPx <= 0) return ratio
  const lo = minContentPx / totalPx
  const hi = 1 - minChatPx / totalPx
  if (hi < lo) return 0.5
  return Math.min(hi, Math.max(lo, ratio))
}

function _splitResolveTransition(ratio, velocity, wide, totalPx, minContentPx, minChatPx) {
  if (velocity < -SPLIT_FLICK_VEL) return SPLIT_STATES.FULL
  if (velocity > SPLIT_FLICK_VEL) return wide ? SPLIT_STATES.SPLIT : SPLIT_STATES.PILL
  const cr = _splitClampRatio(ratio, totalPx, minContentPx, minChatPx)
  if (cr <= minContentPx / totalPx + 0.01) return SPLIT_STATES.FULL
  if (cr >= 1 - minChatPx / totalPx - 0.01) return wide ? SPLIT_STATES.SPLIT : SPLIT_STATES.PILL
  return SPLIT_STATES.SPLIT
}

export function makeSplit() {
  return function split(opts = {}) {
    const mount = opts.mount
    if (!mount || typeof mount.setAttribute !== 'function') {
      throw new Error('window.mobius.split: opts.mount must be a DOM element')
    }
    const defaultRatio = typeof opts.defaultRatio === 'number' ? opts.defaultRatio : 0.65
    const minContentPx = typeof opts.minContentPx === 'number' ? opts.minContentPx : 120
    const minChatPx = typeof opts.minChatPx === 'number' ? opts.minChatPx : 96
    const persistKey = typeof opts.persistKey === 'string' && opts.persistKey
      ? opts.persistKey : null

    // Restore from sessionStorage or use defaults.
    let ratio = defaultRatio
    let state = SPLIT_STATES.PILL
    if (persistKey) {
      try {
        const raw = JSON.parse(sessionStorage.getItem(persistKey) || 'null')
        if (raw && typeof raw.ratio === 'number' && Object.values(SPLIT_STATES).includes(raw.state)) {
          ratio = raw.ratio
          state = raw.state
        }
      } catch (e) {}
    }

    function persist() {
      if (!persistKey) return
      try { sessionStorage.setItem(persistKey, JSON.stringify({ ratio, state })) } catch (e) {}
    }

    function isWide() {
      return mount.offsetWidth >= SPLIT_WIDE_BP
    }

    function totalPx() {
      return isWide() ? mount.offsetWidth : mount.offsetHeight
    }

    function applyState() {
      const wide = isWide()
      const total = totalPx()
      mount.setAttribute('data-split-state', state)
      mount.setAttribute('data-orientation', wide ? 'side' : 'portrait')
      if (wide) {
        const w = state === SPLIT_STATES.FULL ? 0 : Math.round(ratio * total)
        mount.style.setProperty('--cs-content-w', `${w}px`)
        mount.style.removeProperty('--cs-content-h')
      } else {
        let h
        if (state === SPLIT_STATES.PILL) h = total
        else if (state === SPLIT_STATES.FULL) h = 0
        else h = Math.round(ratio * total)
        mount.style.setProperty('--cs-content-h', `${h}px`)
        mount.style.removeProperty('--cs-content-w')
      }
      // Disable content pane pointer events during chat-open states so
      // drag on the handle doesn't accidentally interact with app content.
      const contentEl = mount.querySelector('[data-split-role="content"]')
      if (contentEl) {
        contentEl.style.pointerEvents =
          state === SPLIT_STATES.FULL ? 'none' : 'auto'
      }
    }

    function setState(newState, newRatio) {
      if (Object.values(SPLIT_STATES).includes(newState)) state = newState
      if (typeof newRatio === 'number') {
        ratio = _splitClampRatio(newRatio, totalPx(), minContentPx, minChatPx)
      }
      applyState()
      persist()
    }

    // ── Drag handle element ───────────────────────────────────────────────
    const handle = document.createElement('div')
    handle.setAttribute('role', 'separator')
    handle.setAttribute('aria-label', 'Resize chat panel')
    handle.setAttribute('aria-valuenow', String(Math.round((1 - ratio) * 100)))
    handle.setAttribute('aria-valuemin', '0')
    handle.setAttribute('aria-valuemax', '100')
    handle.setAttribute('tabindex', '0')
    handle.setAttribute('data-split-role', 'handle')
    // 44px hit target with visible 4×40px bar inside.
    handle.style.cssText = [
      'position:absolute',
      'left:0', 'right:0',
      'height:44px',
      'display:flex',
      'align-items:center',
      'justify-content:center',
      'cursor:ns-resize',
      'touch-action:none',
      'z-index:10',
      'background:transparent',
    ].join(';')
    const bar = document.createElement('div')
    bar.style.cssText = 'width:40px;height:4px;border-radius:2px;background:var(--border,rgba(128,128,128,.5))'
    handle.appendChild(bar)

    // Update aria-valuenow (chat fraction %).
    function updateAria() {
      handle.setAttribute('aria-valuenow', String(Math.round((1 - ratio) * 100)))
    }

    // ── Keyboard resize ───────────────────────────────────────────────────
    function onKeyDown(e) {
      const wide = isWide()
      const total = totalPx()
      if (e.key === 'Home') {
        // Home → pill (content full) or split-max if wide.
        state = wide ? SPLIT_STATES.SPLIT : SPLIT_STATES.PILL
        ratio = _splitClampRatio(defaultRatio, total, minContentPx, minChatPx)
        applyState(); persist(); updateAria()
        e.preventDefault()
      } else if (e.key === 'End') {
        // End → full (chat full).
        setState(SPLIT_STATES.FULL)
        updateAria()
        e.preventDefault()
      } else if (e.key === 'ArrowUp') {
        // ArrowUp → grow content pane (shrink chat).
        const newRatio = Math.min(1, ratio + SPLIT_ARROW_STEP)
        const next = _splitResolveTransition(newRatio, SPLIT_FLICK_VEL + 0.1, wide, total, minContentPx, minChatPx)
        state = next === SPLIT_STATES.PILL || next === SPLIT_STATES.SPLIT ? next : SPLIT_STATES.SPLIT
        ratio = _splitClampRatio(newRatio, total, minContentPx, minChatPx)
        applyState(); persist(); updateAria()
        e.preventDefault()
      } else if (e.key === 'ArrowDown') {
        // ArrowDown → shrink content pane (grow chat).
        const newRatio = Math.max(0, ratio - SPLIT_ARROW_STEP)
        const next = _splitResolveTransition(newRatio, -(SPLIT_FLICK_VEL + 0.1), wide, total, minContentPx, minChatPx)
        state = next
        ratio = _splitClampRatio(newRatio, total, minContentPx, minChatPx)
        applyState(); persist(); updateAria()
        e.preventDefault()
      }
    }
    handle.addEventListener('keydown', onKeyDown)

    // ── Pointer/touch drag ────────────────────────────────────────────────
    let dragStartClient = null
    let dragStartRatio = null
    let dragLastClient = null
    let dragLastTime = null
    let dragVelocity = 0

    function clientAxisPos(e) {
      const wide = isWide()
      if (e.touches && e.touches.length > 0) {
        return wide ? e.touches[0].clientX : e.touches[0].clientY
      }
      return wide ? e.clientX : e.clientY
    }

    function onDragStart(e) {
      if (e.button != null && e.button !== 0) return
      dragStartClient = clientAxisPos(e)
      dragStartRatio = ratio
      dragLastClient = dragStartClient
      dragLastTime = Date.now()
      dragVelocity = 0
      // Disable text selection during drag.
      document.body.style.userSelect = 'none'
      document.body.style.webkitUserSelect = 'none'
      e.preventDefault()
    }

    function onDragMove(e) {
      if (dragStartClient === null) return
      const cur = clientAxisPos(e)
      const now = Date.now()
      const elapsed = now - dragLastTime || 1
      dragVelocity = (cur - dragLastClient) / elapsed
      dragLastClient = cur
      dragLastTime = now
      const delta = cur - dragStartClient
      // Dead zone: ignore the first SPLIT_DEAD_ZONE px of travel.
      if (Math.abs(delta) < SPLIT_DEAD_ZONE) return
      const total = totalPx()
      const newRatio = dragStartRatio + (delta - Math.sign(delta) * SPLIT_DEAD_ZONE) / total
      ratio = _splitClampRatio(newRatio, total, minContentPx, minChatPx)
      state = SPLIT_STATES.SPLIT
      applyState()
    }

    function onDragEnd(e) {
      if (dragStartClient === null) return
      document.body.style.userSelect = ''
      document.body.style.webkitUserSelect = ''
      const total = totalPx()
      const wide = isWide()
      const newState = _splitResolveTransition(
        ratio, dragVelocity, wide, total, minContentPx, minChatPx,
      )
      state = newState
      if (newState === SPLIT_STATES.SPLIT) {
        // Keep the dragged ratio; no snap needed.
      } else if (newState === SPLIT_STATES.PILL || (wide && newState === SPLIT_STATES.SPLIT)) {
        // Restore a sensible split ratio when snapping to pill/split-wide.
        ratio = _splitClampRatio(dragStartRatio, total, minContentPx, minChatPx)
      }
      applyState(); persist(); updateAria()
      dragStartClient = null
    }

    handle.addEventListener('pointerdown', onDragStart, { passive: false })
    handle.addEventListener('touchstart', onDragStart, { passive: false })
    window.addEventListener('pointermove', onDragMove, { passive: false })
    window.addEventListener('touchmove', onDragMove, { passive: false })
    window.addEventListener('pointerup', onDragEnd)
    window.addEventListener('pointercancel', onDragEnd)
    window.addEventListener('touchend', onDragEnd)

    // ── Viewport resize — reapply with current state ──────────────────────
    let _roCleanup = null
    if (typeof ResizeObserver !== 'undefined') {
      const ro = new ResizeObserver(() => applyState())
      ro.observe(mount)
      _roCleanup = () => ro.disconnect()
    }

    mount.appendChild(handle)
    applyState()

    return {
      setState,
      destroy() {
        window.removeEventListener('pointermove', onDragMove)
        window.removeEventListener('touchmove', onDragMove)
        window.removeEventListener('pointerup', onDragEnd)
        window.removeEventListener('pointercancel', onDragEnd)
        window.removeEventListener('touchend', onDragEnd)
        handle.removeEventListener('keydown', onKeyDown)
        handle.removeEventListener('pointerdown', onDragStart)
        handle.removeEventListener('touchstart', onDragStart)
        if (handle.parentNode) handle.parentNode.removeChild(handle)
        if (_roCleanup) _roCleanup()
        mount.removeAttribute('data-split-state')
        mount.removeAttribute('data-orientation')
        mount.style.removeProperty('--cs-content-h')
        mount.style.removeProperty('--cs-content-w')
      },
    }
  }
}

export function makeNav() {
  const stack = []
  const entries = new Set()
  const entriesByRequestId = new Map()

  function postForwardResult(type, requestId) {
    try {
      window.parent.postMessage({ type, requestId }, window.location.origin)
    } catch (e) {}
  }

  // One runtime-level responder makes Forward negotiation total: even a fresh
  // runtime with no retained closure explicitly rejects the request. The shell
  // never has to infer restoration from elapsed time or device speed.
  function onForwardMessage(event) {
    if (event.origin !== window.location.origin) return
    if (event.source !== window.parent) return
    const msg = event.data
    if (msg?.type !== 'moebius:nav-forward' || typeof msg.requestId !== 'string') return
    const entry = entriesByRequestId.get(msg.requestId)
    if (!entry || !entry.reversible || entry.done || entry.disposed) {
      postForwardResult('moebius:nav-forward-rejected', msg.requestId)
      return
    }
    if (entry.active) {
      // Idempotent retry after a lost ack: the semantic view is already live.
      postForwardResult('moebius:nav-forward-ack', msg.requestId)
      return
    }
    entry.owned = true
    entry.active = true
    stack.push(entry)
    try {
      entry.onForward()
    } catch (error) {
      const idx = stack.indexOf(entry)
      if (idx !== -1) stack.splice(idx, 1)
      entry.active = false
      entry.owned = false
      console.error('[Möbius nav] Could not restore app view on Forward', error)
      postForwardResult('moebius:nav-forward-rejected', msg.requestId)
      return
    }
    // onForward may synchronously decide that the view cannot remain open and
    // call its own handle.close(). Do not acknowledge a sentinel the app has
    // already retired during restoration.
    if (!entry.active || !entry.owned) {
      postForwardResult('moebius:nav-forward-rejected', msg.requestId)
      return
    }
    postForwardResult('moebius:nav-forward-ack', msg.requestId)
  }
  if (window.parent !== window) window.addEventListener('message', onForwardMessage)

  function open(label, onBackOrHandlers, onForwardArg) {
    // A new push discards the browser's Forward branch. Retire dormant runtime
    // entries from that branch so their closures cannot accumulate forever.
    for (const old of [...entries]) {
      if (!old.active && old.settled) old.dispose?.()
    }
    const handlers = onBackOrHandlers
      && typeof onBackOrHandlers === 'object'
      ? onBackOrHandlers
      : { onBack: onBackOrHandlers, onForward: onForwardArg }
    const requestId = `nav-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`
    const entry = {
      requestId,
      owned: false,
      active: false,
      done: false,
      disposed: false,
      settled: false,
      cleanupTimer: null,
      readyResolve: null,
      outcomeResolve: null,
      onBack: typeof handlers.onBack === 'function' ? handlers.onBack : null,
      onForward: typeof handlers.onForward === 'function' ? handlers.onForward : null,
      reversible: typeof handlers.onForward === 'function',
      dispose: null,
    }
    entries.add(entry)
    entriesByRequestId.set(requestId, entry)
    const outcome = new Promise((resolve) => {
      entry.outcomeResolve = resolve
    })
    // Compatibility: `ready` keeps its original boolean contract forever.
    // New callers should inspect `outcome` because false historically folded
    // together standalone use, shell rejection, timeout, cancellation, and a
    // failed postMessage — states with different UI consequences.
    const ready = new Promise((resolve) => {
      entry.readyResolve = resolve
    })
    const settleOutcome = (status) => {
      if (entry.outcomeResolve) {
        entry.outcomeResolve({ status })
        entry.outcomeResolve = null
      }
      if (entry.readyResolve) {
        entry.readyResolve(status === 'owned')
        entry.readyResolve = null
      }
    }
    const timer = setTimeout(() => {
      // Ownership is unknown on timeout: the shell may have installed the
      // sentinel but its ack may be delayed. Mark the request abandoned and
      // retain correlation briefly so a late ack can be compensated by pop.
      entry.done = true
      settleOutcome('timeout')
      entry.cleanupTimer = setTimeout(() => {
        entry.settled = true
        dispose()
      }, 30000)
    }, 5000)

    const dispose = () => {
      if (entry.disposed) return
      entry.disposed = true
      entry.done = true
      entry.active = false
      entry.owned = false
      const idx = stack.indexOf(entry)
      if (idx !== -1) stack.splice(idx, 1)
      entries.delete(entry)
      if (entriesByRequestId.get(requestId) === entry) entriesByRequestId.delete(requestId)
      clearTimeout(timer)
      if (entry.cleanupTimer !== null) clearTimeout(entry.cleanupTimer)
      window.removeEventListener('message', onMessage)
    }
    entry.dispose = dispose

    const close = (fromShell = false) => {
      if (entry.done) return
      const idx = stack.indexOf(entry)
      if (idx !== -1) stack.splice(idx, 1)
      if (!fromShell && entry.owned) {
        try {
          window.parent.postMessage({ type: 'moebius:nav-pop' }, window.location.origin)
        } catch (e) {}
      }
      entry.active = false
      entry.owned = false
      if (!entry.settled) {
        // Preserve request correlation long enough to compensate a late ack
        // with nav-pop; disposing now would strand a shell sentinel.
        entry.done = true
        settleOutcome('cancelled')
        return
      }
      // A reversible, settled entry stays dormant so browser Forward can
      // reactivate the same app state. Legacy/back-only entries retain their
      // destructive behavior and are disposed immediately.
      if (!entry.reversible || !entry.settled) dispose()
    }

    function onMessage(event) {
      if (event.origin !== window.location.origin) return
      if (event.source !== window.parent) return
      const msg = event.data
      if (msg?.type === 'moebius:nav-back') {
        if (stack[stack.length - 1] !== entry) return
        if (msg.requestId && msg.requestId !== requestId) return
        close(true)
        if (entry.onBack) entry.onBack()
        return
      }
      if (msg?.requestId !== requestId) return
      if (msg.type === 'moebius:nav-push-ack') {
        entry.settled = true
        clearTimeout(timer)
        if (entry.cleanupTimer !== null) clearTimeout(entry.cleanupTimer)
        if (entry.done) {
          try {
            window.parent.postMessage({ type: 'moebius:nav-pop' }, window.location.origin)
          } catch (e) {}
          // A close-before-ack already settled the public outcome as
          // `cancelled`; the late ack is compensated with exactly one pop.
          settleOutcome('owned')
          dispose()
          return
        }
        entry.owned = true
        entry.active = true
        stack.push(entry)
        settleOutcome('owned')
      } else if (msg.type === 'moebius:nav-push-rejected') {
        entry.settled = true
        clearTimeout(timer)
        if (entry.cleanupTimer !== null) clearTimeout(entry.cleanupTimer)
        entry.owned = false
        settleOutcome('rejected')
        dispose()
      }
    }

    window.addEventListener('message', onMessage)
    if (window.parent === window) {
      clearTimeout(timer)
      entry.settled = true
      // App runtimes are required to be hosted by AppCanvas. Treat a direct
      // top-level invocation as unavailable rather than growing a second
      // navigation implementation.
      entry.reversible = false
      settleOutcome('unavailable')
      window.removeEventListener('message', onMessage)
      return {
        ready,
        outcome,
        close() {
          close(false)
        },
      }
    }
    try {
      window.parent.postMessage(
        {
          type: 'moebius:nav-push',
          label: label || 'app-detail',
          requestId,
          reversible: entry.reversible,
        },
        window.location.origin,
      )
    } catch (e) {
      clearTimeout(timer)
      entry.settled = true
      settleOutcome('error')
      dispose()
    }

    return {
      ready,
      outcome,
      close() {
        close(false)
      },
    }
  }

  return { open }
}
