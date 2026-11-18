/* The answer guard as a hook: every key request card wears it (KeyRequest.jsx, KeyQuestionCard.jsx), and so does the shell's confirmation over an app (OwnerConfirmSheet.jsx). */
import { useCallback, useEffect, useRef, useState } from 'react'
import {
  keyAnswerActiveAt, keyAnswerClickCounts, keyAnswerMoved, keyAnswerPlace, keyAnswerPlaceChanged, keyAnswerPressed,
  keyAnswerShown,
} from '../../lib/keyAnswerGuard.js'

/**
 * Keeps the answers asleep until the card has been seen, still and untouched
 * for a moment (keyAnswerGuard.js). ``cardProps`` go on the card: its ref,
 * and capture-phase handlers that hear every press and key on it before any
 * of its controls. The wait starts again whenever ``enabled`` turns on (the
 * answers appear), the card moves (checked when it or anything around it
 * changes size, on any scroll and at each press, so a card pushed or slid
 * into place under the pointer waits again; scrolling the card yourself does
 * too), and whenever the page may have shown something else in its place: a
 * tab or window coming back into view, entering or leaving fullscreen, or a
 * page restored from the back-forward cache. Callers key each card by what it
 * asks (``requestFormKey``), so a replacement request is a new card and waits
 * again. ``early`` names the button of a click the guard just ignored (null
 * otherwise), so for a moment the card can say why nothing happened and which
 * button to tap (earlyAnswerHint) instead of looking broken.
 */
/** What the card says after an early tap on the button labelled ``label``. */
export function earlyAnswerHint(label) {
  return label ? `Wait a second, then tap “${label}” again.` : 'Wait a second, then tap again.'
}
const EARLY_HINT_MS = 2000

export default function useKeyAnswerGuard({ enabled }) {
  const cardRef = useRef(null)
  const place = useRef(null)
  const [shownAt] = useState(() => performance.now())
  const state = useRef(keyAnswerShown(shownAt))
  const [activeAt, setActiveAt] = useState(() => keyAnswerActiveAt(keyAnswerShown(shownAt)))
  const [active, setActive] = useState(false)
  const [early, setEarly] = useState(null)
  const earlyTimer = useRef(null)

  useEffect(() => () => clearTimeout(earlyTimer.current), [])

  useEffect(() => {
    const wait = activeAt - performance.now()
    if (wait <= 0) { setActive(true); return undefined }
    setActive(false)
    const timer = setTimeout(() => setActive(true), wait)
    return () => clearTimeout(timer)
  }, [activeAt])

  const restart = useCallback(() => {
    state.current = keyAnswerMoved(state.current, performance.now())
    setActiveAt(keyAnswerActiveAt(state.current))
  }, [])

  const noteMove = useCallback(() => {
    const node = cardRef.current
    if (!node) return
    const now = keyAnswerPlace(node.getBoundingClientRect())
    if (keyAnswerPlaceChanged(place.current, now)) restart()
    place.current = now
  }, [restart])

  useEffect(() => {
    if (!enabled) return undefined
    restart()
    const revealed = () => { if (document.visibilityState === 'visible') restart() }
    document.addEventListener('visibilitychange', revealed)
    document.addEventListener('fullscreenchange', restart)
    window.addEventListener('pageshow', restart)
    return () => {
      document.removeEventListener('visibilitychange', revealed)
      document.removeEventListener('fullscreenchange', restart)
      window.removeEventListener('pageshow', restart)
    }
  }, [enabled, restart])

  useEffect(() => {
    const node = cardRef.current
    if (!enabled || !node) return undefined
    const check = () => noteMove()
    check()
    const resized = typeof ResizeObserver === 'undefined' ? null : new ResizeObserver(check)
    for (let element = node; element && resized; element = element.parentElement) resized.observe(element)
    window.addEventListener('scroll', check, { capture: true, passive: true })
    window.addEventListener('resize', check)
    return () => {
      resized?.disconnect()
      window.removeEventListener('scroll', check, { capture: true })
      window.removeEventListener('resize', check)
    }
  }, [enabled, noteMove])

  const onPress = event => {
    // A held key's repeats are one press, not many.
    if (event.repeat) return
    // A move nothing reported yet counts as happening now.
    noteMove()
    state.current = keyAnswerPressed(state.current, event)
    setActiveAt(keyAnswerActiveAt(state.current))
  }

  return {
    cardProps: { ref: cardRef, onPointerDownCapture: onPress, onKeyDownCapture: onPress },
    active,
    early,
    counts: event => {
      const counted = keyAnswerClickCounts(state.current, event)
      if (!counted) {
        // The wait itself is unchanged; the card only says why nothing happened.
        setEarly(event.currentTarget?.textContent?.trim() || '')
        clearTimeout(earlyTimer.current)
        earlyTimer.current = setTimeout(() => setEarly(null), EARLY_HINT_MS)
      }
      return counted
    },
  }
}
