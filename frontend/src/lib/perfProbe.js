/**
 * Field performance probe — opt-in, per-device, passive.
 *
 * WHY THIS EXISTS
 * Every prior Mobius performance investigation profiled headless desktop
 * Chromium with software rasterization. That environment structurally cannot
 * observe what makes a phone slow:
 *   - a tile-based deferred GPU, where `backdrop-filter` forces a tile resolve
 *   - a 3-5x slower CPU, which turns a "fine" 5ms render into a dropped frame
 *   - touch input sampled at 120-240Hz instead of one wheel notch per gesture
 *   - `visualViewport` resize/scroll, which barely fire on desktop at all
 *   - flash storage, where an IndexedDB write costs an order of magnitude more
 *   - a real cellular link instead of loopback
 * So the desktop numbers were not merely incomplete — for several suspects they
 * were actively misleading. This probe closes that gap by measuring the owner's
 * real devices instead of a proxy for them.
 *
 * MEASUREMENT MUST NOT CREATE THE JANK IT REPORTS
 * This is the design constraint that shapes everything below. The probe uses
 * ONLY passive `PerformanceObserver` entries, which the browser already
 * computes for its own devtools. Specifically it does NOT run a
 * `requestAnimationFrame` loop to count frames: a rAF loop keeps the compositor
 * awake at display refresh rate, which would manufacture exactly the
 * always-on-compositor cost a previous round of this investigation was trying
 * to find. An idle probe must leave the machine idle.
 *
 * Long Animation Frames (LoAF) are the primary signal. Unlike `longtask`, a
 * LoAF entry attributes blocking time to a script URL and function name, so a
 * report says "useScrollMode.js blocked 180ms" rather than "something blocked
 * 180ms" — the difference between evidence and a new guess.
 *
 * ENABLEMENT
 * Off by default and zero-cost when off: `perfMark()` returns immediately, no
 * observers are registered, and nothing is sent. Turn it on per device with
 * `?perf=1` (persisted to localStorage so it survives navigation), off with
 * `?perf=0`. A device that was never opted in never reports.
 */

import { getAuthHeaders, BASE } from '../api/client.js'
import { isStandaloneDisplay } from '../utils/installPlatform.js'

const STORAGE_KEY = 'mobius-perf-probe'
const SESSION_KEY = 'mobius-perf-session'

// How often aggregated counters are flushed to the server. Long enough that
// the POST itself is negligible, short enough that a phone put down mid-session
// still reports what it saw.
const FLUSH_INTERVAL_MS = 15_000

// Per-flush caps. A probe that grows without bound would itself become a memory
// problem on the constrained device it is supposed to be diagnosing.
const MAX_LOAF_SAMPLES = 40
const MAX_EVENT_SAMPLES = 30
const MAX_RESOURCE_SAMPLES = 60

let enabled = false
let started = false
let sessionId = null

/** Accumulators, reset after every successful flush. */
let counters = Object.create(null)
let loafs = []
let events = []
let longTasks = { count: 0, totalMs: 0, maxMs: 0 }
let cls = 0
const observers = []
let flushTimer = null

/**
 * Resolve the opt-in state, honouring an explicit `?perf=` override first so a
 * device can be switched on or off by opening one link on it — which is the
 * only practical enrolment path for a phone.
 */
function resolveEnabled() {
  try {
    const param = new URLSearchParams(window.location.search).get('perf')
    if (param === '1') {
      localStorage.setItem(STORAGE_KEY, '1')
      return true
    }
    if (param === '0') {
      localStorage.removeItem(STORAGE_KEY)
      return false
    }
    return localStorage.getItem(STORAGE_KEY) === '1'
  } catch {
    return false
  }
}

export function isPerfProbeEnabled() {
  return enabled
}

/**
 * Record one observation from an instrumented hot path.
 *
 * Deliberately shaped as a plain counter rather than a `performance.measure()`
 * pair: the User Timing buffer is itself an allocation per call, and these call
 * sites fire per touchmove and per streamed frame. When the probe is disabled
 * this is a single boolean test that a JIT can hoist out entirely, so leaving
 * the call sites in place costs nothing in normal use.
 */
export function perfMark(name, durationMs = 0) {
  if (!enabled) return
  const slot = counters[name] || (counters[name] = { count: 0, totalMs: 0, maxMs: 0 })
  slot.count += 1
  if (durationMs > 0) {
    slot.totalMs += durationMs
    if (durationMs > slot.maxMs) slot.maxMs = durationMs
  }
}

/**
 * Time a synchronous hot-path call and attribute it to `name`.
 * Returns the callee's value so a call site can wrap in place.
 */
export function perfTime(name, fn) {
  if (!enabled) return fn()
  const t0 = performance.now()
  try {
    return fn()
  } finally {
    perfMark(name, performance.now() - t0)
  }
}

function describeDevice() {
  const nav = navigator || {}
  const conn = nav.connection || nav.mozConnection || nav.webkitConnection || null
  const standalone = isStandaloneDisplay(window)
  // Form factor is decided by the coarse-pointer media query rather than a UA
  // string: what matters for these measurements is whether the device uses a
  // touch digitizer and a mobile GPU, which is what the query actually asks.
  const coarse = window.matchMedia?.('(pointer: coarse)')?.matches === true
  return {
    formFactor: coarse || window.innerWidth < 700 ? 'phone' : 'desktop',
    ua: nav.userAgent,
    dpr: window.devicePixelRatio,
    viewport: `${window.innerWidth}x${window.innerHeight}`,
    screen: `${window.screen?.width}x${window.screen?.height}`,
    hardwareConcurrency: nav.hardwareConcurrency ?? null,
    deviceMemoryGb: nav.deviceMemory ?? null,
    standalone,
    coarsePointer: coarse,
    reducedMotion: window.matchMedia?.('(prefers-reduced-motion: reduce)')?.matches === true,
    connection: conn
      ? {
          effectiveType: conn.effectiveType,
          downlinkMbps: conn.downlink,
          rttMs: conn.rtt,
          saveData: conn.saveData,
        }
      : null,
  }
}

/**
 * Navigation + transfer summary.
 *
 * `transferSize` vs `decodedBodySize` is the load-side headline: it proves on
 * the real network path whether assets arrived compressed and whether the
 * device reused its cache, neither of which can be inferred from the server
 * side. A `transferSize` of 0 means the entry was served from cache or the
 * service worker; equal transfer and decoded sizes mean it arrived
 * uncompressed.
 */
function collectLoad() {
  const navEntry = performance.getEntriesByType('navigation')[0]
  const resources = performance.getEntriesByType('resource')

  let transfer = 0
  let decoded = 0
  let cached = 0
  const heavy = []

  for (const r of resources) {
    transfer += r.transferSize || 0
    decoded += r.decodedBodySize || 0
    if ((r.transferSize || 0) === 0 && (r.decodedBodySize || 0) > 0) cached += 1
    if ((r.decodedBodySize || 0) > 20_000) {
      heavy.push({
        name: r.name.split('/').slice(-1)[0].slice(0, 60),
        type: r.initiatorType,
        transferSize: r.transferSize,
        decodedBodySize: r.decodedBodySize,
        durationMs: Math.round(r.duration),
      })
    }
  }

  heavy.sort((a, b) => b.durationMs - a.durationMs)

  return {
    navigation: navEntry
      ? {
          type: navEntry.type,
          // Time to the first byte of the document — isolates server/network
          // latency from everything the client does afterwards.
          ttfbMs: Math.round(navEntry.responseStart),
          domContentLoadedMs: Math.round(navEntry.domContentLoadedEventEnd),
          loadEventMs: Math.round(navEntry.loadEventEnd),
          transferSize: navEntry.transferSize,
          decodedBodySize: navEntry.decodedBodySize,
        }
      : null,
    resourceCount: resources.length,
    totalTransferBytes: transfer,
    totalDecodedBytes: decoded,
    cachedResourceCount: cached,
    heaviest: heavy.slice(0, MAX_RESOURCE_SAMPLES),
  }
}

/**
 * Census of what is still animating.
 *
 * `document.getAnimations()` is the definitive answer to "what keeps the
 * compositor awake when Mobius is idle", and it is a question static analysis
 * of the stylesheet cannot settle — a keyframe rule only matters if some
 * element is actually running it right now.
 */
function collectAnimations() {
  try {
    const running = document.getAnimations().filter((a) => a.playState === 'running')
    const byName = Object.create(null)
    for (const a of running) {
      const name = a.animationName || a.constructor?.name || 'unknown'
      byName[name] = (byName[name] || 0) + 1
    }
    return { runningCount: running.length, byName }
  } catch {
    return null
  }
}

export function collectAnimationsForSample(reason) {
  // Android may synchronously recalculate style/layout to answer
  // document.getAnimations(). Recurring and lifecycle-triggered samples must
  // only read browser entries already recorded for them; the animation census
  // remains available for an explicit one-shot console sample.
  if (reason !== 'manual') return null
  return collectAnimations()
}

export function collectDom() {
  return {
    // Keep the interval sample O(1) with respect to style/layout. The former
    // census called getComputedStyle twice for every node in the document. On
    // the owner's phone that made the supposedly passive probe create its own
    // 250-350ms long animation frame every 15 seconds, then report that frame
    // at the next interval. A node count is enough to correlate a large live
    // surface with other passive entries; property-level style inspection
    // belongs in an explicit one-shot DevTools investigation, never the field
    // recorder that is measuring ordinary interaction.
    nodeCount: document.getElementsByTagName('*').length,
  }
}

function startObservers() {
  const observe = (type, handler, extra = {}) => {
    try {
      const po = new PerformanceObserver(handler)
      po.observe({ type, buffered: true, ...extra })
      observers.push(po)
    } catch {
      // Unsupported entry type on this browser (notably LoAF on Safari) — the
      // sample simply omits that section rather than failing the whole probe.
    }
  }

  // Long Animation Frames: the attributed signal. `blockingDuration` is the
  // part of the frame that actually delayed input response.
  observe('long-animation-frame', (list) => {
    for (const entry of list.getEntries()) {
      if (loafs.length >= MAX_LOAF_SAMPLES) break
      loafs.push({
        durationMs: Math.round(entry.duration),
        blockingMs: Math.round(entry.blockingDuration),
        renderMs: Math.round(entry.renderStart ? entry.styleAndLayoutStart - entry.renderStart : 0),
        styleLayoutMs: Math.round(
          entry.styleAndLayoutStart ? entry.startTime + entry.duration - entry.styleAndLayoutStart : 0,
        ),
        scripts: (entry.scripts || []).slice(0, 3).map((s) => ({
          // `invoker` names the call site (e.g. an event listener); `sourceURL`
          // plus function name points at the owning module.
          invoker: String(s.invoker || '').slice(0, 80),
          source: String(s.sourceURL || '').split('/').slice(-1)[0].slice(0, 50),
          fn: String(s.sourceFunctionName || '').slice(0, 50),
          durationMs: Math.round(s.duration),
          forcedStyleAndLayoutMs: Math.round(s.forcedStyleAndLayoutDuration || 0),
        })),
      })
    }
  })

  observe('longtask', (list) => {
    for (const entry of list.getEntries()) {
      longTasks.count += 1
      longTasks.totalMs += entry.duration
      if (entry.duration > longTasks.maxMs) longTasks.maxMs = entry.duration
    }
  })

  // Interaction latency — how long a tap or keystroke took to be reflected on
  // screen. This is the number that most directly corresponds to what the
  // owner actually feels as "sluggish".
  observe(
    'event',
    (list) => {
      for (const entry of list.getEntries()) {
        if (events.length >= MAX_EVENT_SAMPLES) break
        events.push({
          name: entry.name,
          durationMs: Math.round(entry.duration),
          // Time spent waiting before the handler even started, which
          // distinguishes "the main thread was busy" from "the handler is slow".
          delayMs: Math.round(entry.processingStart - entry.startTime),
          processingMs: Math.round(entry.processingEnd - entry.processingStart),
        })
      }
    },
    { durationThreshold: 40 },
  )

  observe('layout-shift', (list) => {
    for (const entry of list.getEntries()) {
      if (!entry.hadRecentInput) cls += entry.value
    }
  })
}

function buildSample(reason) {
  const sorted = [...events].sort((a, b) => b.durationMs - a.durationMs)
  return {
    sessionId,
    reason,
    at: new Date().toISOString(),
    uptimeMs: Math.round(performance.now()),
    build: window.__MOBIUS_BUILD__ || null,
    route: window.location.pathname,
    device: describeDevice(),
    load: collectLoad(),
    longTasks: {
      count: longTasks.count,
      totalMs: Math.round(longTasks.totalMs),
      maxMs: Math.round(longTasks.maxMs),
    },
    loaf: {
      count: loafs.length,
      totalBlockingMs: Math.round(loafs.reduce((s, l) => s + l.blockingMs, 0)),
      worst: [...loafs].sort((a, b) => b.blockingMs - a.blockingMs).slice(0, 10),
    },
    interactions: {
      count: events.length,
      worst: sorted.slice(0, 10),
      p75DurationMs: sorted.length ? sorted[Math.floor(sorted.length * 0.25)].durationMs : 0,
    },
    cls: Math.round(cls * 1000) / 1000,
    animations: collectAnimationsForSample(reason),
    dom: collectDom(),
    hotPaths: counters,
  }
}

function resetAccumulators() {
  counters = Object.create(null)
  loafs = []
  events = []
  longTasks = { count: 0, totalMs: 0, maxMs: 0 }
  cls = 0
}

async function flush(reason) {
  if (!enabled) return
  const sample = buildSample(reason)
  // Nothing observed and nothing counted — skip the round trip so an idle
  // backgrounded phone does not wake its radio to report zeroes.
  if (
    reason !== 'initial' &&
    sample.longTasks.count === 0 &&
    sample.loaf.count === 0 &&
    sample.interactions.count === 0 &&
    Object.keys(sample.hotPaths).length === 0
  ) {
    return
  }

  resetAccumulators()

  const body = JSON.stringify(sample)
  try {
    // `keepalive` rather than `sendBeacon`, because Mobius authenticates with a
    // bearer token: `sendBeacon` cannot set an Authorization header, so a beacon
    // would be rejected exactly on the flush that matters most — the one sent as
    // the owner backgrounds the phone right after it felt slow. A keepalive
    // fetch carries headers AND survives page teardown, so it satisfies both
    // requirements with one path instead of two.
    await fetch(`${BASE}/api/debug/perf`, {
      method: 'POST',
      headers: getAuthHeaders({ 'Content-Type': 'application/json' }),
      body,
      keepalive: true,
    })
  } catch {
    // A dropped sample is not worth surfacing to the owner; the next flush
    // carries fresh data anyway.
  }
}

/**
 * Install the probe. Safe to call unconditionally at startup: when the device
 * has not opted in this returns immediately, registers no observers, and leaves
 * `perfMark` a no-op.
 */
export function installPerfProbe() {
  if (started) return
  started = true
  enabled = resolveEnabled()
  if (!enabled) return

  try {
    sessionId = sessionStorage.getItem(SESSION_KEY)
    if (!sessionId) {
      sessionId = `${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 8)}`
      sessionStorage.setItem(SESSION_KEY, sessionId)
    }
  } catch {
    sessionId = 'ephemeral'
  }

  startObservers()

  // The first sample carries load/transfer timing, which is only meaningful
  // once the load has finished.
  if (document.readyState === 'complete') {
    setTimeout(() => flush('initial'), 1500)
  } else {
    window.addEventListener('load', () => setTimeout(() => flush('initial'), 1500), { once: true })
  }

  flushTimer = setInterval(() => flush('interval'), FLUSH_INTERVAL_MS)

  // A phone's session usually ends by being backgrounded rather than closed, so
  // `visibilitychange` is the reliable last-chance flush.
  document.addEventListener('visibilitychange', () => {
    if (document.visibilityState === 'hidden') flush('hidden')
  })

  // Make the probe's own state legible from the console on the device under
  // test, so enrolment can be confirmed without a debugger attached.
  window.__mobiusPerf = {
    enabled: () => enabled,
    sample: () => buildSample('manual'),
    flush: () => flush('manual'),
    stop: () => {
      try {
        localStorage.removeItem(STORAGE_KEY)
      } catch {}
      enabled = false
      observers.forEach((o) => o.disconnect())
      if (flushTimer) clearInterval(flushTimer)
    },
  }

  console.info('[mobius] performance probe active — disable with ?perf=0')
}
