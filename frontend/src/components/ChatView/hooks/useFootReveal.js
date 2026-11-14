/* The Goal rail and the helpers/waits stack stay pinned above the composer
   and step aside like a mobile browser's toolbar: as the reader scrolls up
   (away from the bottom) they sink behind the composer to the view bottom
   by as many pixels as the reader moved, and as the reader scrolls back down they rise again by
   the same amount, wherever the reader is in the history.

   One number drives it: the hide offset, clamped to [0, H], where H is the
   distance from the rows' resting top to the view bottom. It is published on
   the foot as `--foot-hide` (px) and `--foot-hide-fraction` (0 shown, 1
   gone); the rows and the floating stack above them (jump to latest, the
   question and resume nudges) share that offset, but the floating actions
   stop above the composer.

   A visual layer only. It reads scroll position and the controller's
   `data-scroll-mode`, and never writes scroll position, so scroll policy
   (useScrollMode, scroll/*) is untouched. The foot keeps its measured height,
   so the transcript's bottom clearance is the same hidden or shown.

   - At the physical bottom the rows are always fully shown.
   - Entering a live-end mode (FOLLOW_BOTTOM, PIN_USER_MSG: a send, the jump
     to latest, a follow that re-engaged) shows them: the reader is back at
     the conversation's end. While following they stay as they are, so a
     streamed reply running a frame ahead of the follow never moves them.
   - Only the reader's own scrolling moves them. A scroll counts as the
     reader's while a wheel, key, touch or scrollbar drag is in progress or
     happened within READER_SCROLL_WINDOW_MS, and each such scroll extends
     the window so momentum after a lift still counts. Anything else (scroll
     to bottom, anchor repairs, restores, the controller's follow writes) is
     programmatic and ignored. The controller does not expose its own gesture
     window, so this mirrors its input set read-only with the same 250ms
     quiet edge (GESTURE_SETTLE_MS) rather than sharing state.
   - A layout change in the same frame can only shrink the counted motion
     (readerScrollMotion), so an older page prepended above a scrolling
     reader, which the controller compensates in scrollTop, is not counted as
     a jump back down.
   - Once mostly hidden they are inert: no taps, no focus.
   - With reduced motion they snap fully hidden or shown by direction. */
import { useLayoutEffect } from 'react'

// Within this of the end counts as the physical bottom (sub-pixel scrollTop).
export const FOOT_REVEAL_BOTTOM_PX = 2
export const FOOT_HIDDEN_AT = 0.75
export const READER_SCROLL_WINDOW_MS = 250
const REDUCED_MOTION_QUERY = '(prefers-reduced-motion: reduce)'
// Scroll-controller modes that hold the view at the conversation's live end.
const LIVE_END_MODES = new Set(['FOLLOW_BOTTOM', 'PIN_USER_MSG'])

// Shown, with no previous frame to measure motion from.
export const FOOT_REVEAL_SHOWN = Object.freeze({
  offset: 0, height: 0, scrollTop: null, scrollHeight: null, mode: undefined,
})

/** The reader's own motion between two frames, positive toward the bottom.
 *  When the content height also changed, the controller may have compensated
 *  scrollTop for rows added or removed above; take whichever reading moves
 *  less, so layout never inflates the motion. */
export function readerScrollMotion(prev, next) {
  if (prev.scrollTop == null) return 0
  const moved = next.scrollTop - prev.scrollTop
  const grown = next.scrollHeight - prev.scrollHeight
  if (!grown) return moved
  const compensated = moved - grown
  return Math.abs(compensated) < Math.abs(moved) ? compensated : moved
}

/** One frame of the hide offset.
 *  `frame`: { scrollTop, scrollHeight, clientHeight, height, mode,
 *  readerScrolled, reducedMotion }. Returns the next state; its `offset` is
 *  in [0, frame.height]. */
export function footRevealStep(state, frame) {
  const height = Math.max(0, Number(frame.height) || 0)
  // The rows changed height (a helper came or left): keep how hidden they are.
  let offset = state.height > 0 ? state.offset * (height / state.height) : 0
  const distance = frame.scrollHeight - frame.clientHeight - frame.scrollTop
  const enteredLiveEnd = frame.mode !== state.mode && LIVE_END_MODES.has(frame.mode)
  if (!height || distance <= FOOT_REVEAL_BOTTOM_PX || enteredLiveEnd) {
    offset = 0
  } else if (frame.readerScrolled) {
    const motion = readerScrollMotion(state, frame)
    if (frame.reducedMotion) {
      if (motion < 0) offset = height
      else if (motion > 0) offset = 0
    } else {
      offset -= motion
    }
  }
  return {
    offset: Math.min(height, Math.max(0, offset)),
    height,
    scrollTop: frame.scrollTop,
    scrollHeight: frame.scrollHeight,
    mode: frame.mode,
  }
}

export function footRevealFraction({ offset, height }) {
  return height > 0 ? offset / height : 0
}

const round = value => Math.round(value * 1000) / 1000

function applyReveal(foot, layer, state) {
  const fraction = footRevealFraction(state)
  foot.style.setProperty('--foot-hide', `${round(state.offset)}px`)
  foot.style.setProperty('--foot-hide-fraction', String(round(fraction)))
  const hidden = fraction >= FOOT_HIDDEN_AT
  if (layer.inert !== hidden) layer.inert = hidden
  if (hidden) layer.setAttribute('data-hidden', '')
  else layer.removeAttribute('data-hidden')
}

// Travel reaches the view bottom, including the composer and safe-area gap.
// Floating navigation retains its shorter path above the composer.
export function layerTravel(layer, foot) {
  const height = layer.offsetHeight
  if (!height) return 0
  const gap = Number.parseFloat(window.getComputedStyle?.(layer)?.marginBottom) || 0
  const rowsTravel = height + gap
  foot.style.setProperty('--foot-rows-travel', `${rowsTravel}px`)
  // offsetTop is layout-only: unlike a bounding rect it excludes our transform.
  const safeBottom = Number.parseFloat(window.getComputedStyle?.(foot)?.bottom) || 0
  return Math.max(rowsTravel, foot.offsetHeight - layer.offsetTop + safeBottom)
}

/** Drive the foot's hide offset from the reader's scrolling of `scrollRef`
 *  while `active`. Writes the variables on `footRef`, inert on `layerRef`. */
export default function useFootReveal(scrollRef, footRef, layerRef, active) {
  useLayoutEffect(() => {
    const foot = footRef.current
    const layer = layerRef.current
    if (!foot || !layer) return undefined
    const scroller = active ? scrollRef.current : null
    if (!scroller) {
      applyReveal(foot, layer, FOOT_REVEAL_SHOWN)
      return undefined
    }
    const motion = typeof window.matchMedia === 'function' ? window.matchMedia(REDUCED_MOTION_QUERY) : null
    const now = () => (typeof performance !== 'undefined' ? performance.now() : Date.now())
    let readerUntil = 0
    let touches = 0
    let pressing = false
    let readerScrolled = false
    let height = layerTravel(layer, foot)
    let state = FOOT_REVEAL_SHOWN
    let frame = 0
    const update = () => {
      frame = 0
      state = footRevealStep(state, {
        scrollTop: scroller.scrollTop,
        scrollHeight: scroller.scrollHeight,
        clientHeight: scroller.clientHeight,
        height,
        mode: scroller.dataset?.scrollMode,
        readerScrolled,
        reducedMotion: !!motion?.matches,
      })
      readerScrolled = false
      applyReveal(foot, layer, state)
    }
    const schedule = () => {
      if (!frame) frame = window.requestAnimationFrame(update)
    }
    const noteInput = () => { readerUntil = now() + READER_SCROLL_WINDOW_MS }
    const noteTouch = (event) => {
      touches = event.touches ? event.touches.length : 0
      noteInput()
    }
    // A mouse drag on the scrollbar; touch contact is tracked by touch events.
    const notePress = (event) => {
      if (event.pointerType !== 'mouse') return
      pressing = true
      noteInput()
    }
    const noteRelease = () => {
      if (!pressing) return
      pressing = false
      noteInput()
    }
    const onScroll = () => {
      const t = now()
      if (touches > 0 || pressing || t < readerUntil) {
        readerScrolled = true
        readerUntil = t + READER_SCROLL_WINDOW_MS
      }
      schedule()
    }
    update()
    const passive = { passive: true }
    scroller.addEventListener('scroll', onScroll, passive)
    for (const type of ['wheel', 'keydown']) scroller.addEventListener(type, noteInput, passive)
    for (const type of ['touchstart', 'touchmove', 'touchend', 'touchcancel']) scroller.addEventListener(type, noteTouch, passive)
    scroller.addEventListener('pointerdown', notePress, passive)
    window.addEventListener('pointerup', noteRelease, passive)
    window.addEventListener('pointercancel', noteRelease, passive)
    motion?.addEventListener?.('change', schedule)
    // Content growth or a resized foot changes the distance without a scroll
    // event; composer growth also changes how far the rows travel.
    const resize = typeof ResizeObserver === 'function'
      ? new ResizeObserver(() => { height = layerTravel(layer, foot); schedule() })
      : null
    if (resize) {
      resize.observe(scroller)
      resize.observe(foot, { box: 'border-box' })
      resize.observe(layer, { box: 'border-box' })
      for (const child of scroller.children) resize.observe(child, { box: 'border-box' })
    }
    const modes = typeof MutationObserver === 'function' ? new MutationObserver(schedule) : null
    modes?.observe(scroller, { attributes: true, attributeFilter: ['data-scroll-mode'] })
    return () => {
      scroller.removeEventListener('scroll', onScroll)
      for (const type of ['wheel', 'keydown']) scroller.removeEventListener(type, noteInput)
      for (const type of ['touchstart', 'touchmove', 'touchend', 'touchcancel']) scroller.removeEventListener(type, noteTouch)
      scroller.removeEventListener('pointerdown', notePress)
      window.removeEventListener('pointerup', noteRelease)
      window.removeEventListener('pointercancel', noteRelease)
      motion?.removeEventListener?.('change', schedule)
      resize?.disconnect()
      modes?.disconnect()
      if (frame) window.cancelAnimationFrame(frame)
      applyReveal(foot, layer, FOOT_REVEAL_SHOWN)
    }
  }, [scrollRef, footRef, layerRef, active])
}
