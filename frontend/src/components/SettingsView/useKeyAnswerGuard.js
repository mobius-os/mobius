/* A key request card's answer guard as a hook: KeyRequest.jsx puts it on every card (Settings › Keys, a chat, over an app). */
import { useCallback, useEffect, useRef, useState } from 'react'
import {
  keyAnswerActiveAt, keyAnswerClickCounts, keyAnswerMoved, keyAnswerPlace, keyAnswerPlaceChanged, keyAnswerPressed,
  keyAnswerShown,
} from '../../lib/keyAnswerGuard.js'

/**
 * Keeps the answers asleep until the card has been still and untouched for a
 * moment (keyAnswerGuard.js). ``cardRef`` goes on the card; ``onPressCapture``
 * hears every press on it, before any of its controls. The card's place is
 * checked when it or anything around it changes size, on any scroll and at
 * each press, so a card pushed or slid into place under the pointer waits
 * again; scrolling the card yourself does too. Callers key each card by
 * ``requestFormKey``, so a replacement request is a new card and waits again.
 * ``early`` is true for a moment after a click the guard ignored, so the card
 * can say why nothing happened (EARLY_ANSWER_HINT) instead of looking broken.
 */
export const EARLY_ANSWER_HINT = 'One moment, then tap again.'
const EARLY_HINT_MS = 2000

export default function useKeyAnswerGuard({ enabled }) {
  const cardRef = useRef(null)
  const place = useRef(null)
  const [shownAt] = useState(() => performance.now())
  const state = useRef(keyAnswerShown(shownAt))
  const [activeAt, setActiveAt] = useState(() => keyAnswerActiveAt(keyAnswerShown(shownAt)))
  const [active, setActive] = useState(false)
  const [early, setEarly] = useState(false)
  const earlyTimer = useRef(null)

  useEffect(() => () => clearTimeout(earlyTimer.current), [])

  useEffect(() => {
    const wait = activeAt - performance.now()
    if (wait <= 0) { setActive(true); return undefined }
    setActive(false)
    const timer = setTimeout(() => setActive(true), wait)
    return () => clearTimeout(timer)
  }, [activeAt])

  const noteMove = useCallback(() => {
    const node = cardRef.current
    if (!node) return
    const now = keyAnswerPlace(node.getBoundingClientRect())
    if (keyAnswerPlaceChanged(place.current, now)) {
      state.current = keyAnswerMoved(state.current, performance.now())
      setActiveAt(keyAnswerActiveAt(state.current))
    }
    place.current = now
  }, [])

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

  return {
    cardRef,
    active,
    onPressCapture: event => {
      // A move nothing reported yet counts as happening now.
      noteMove()
      state.current = keyAnswerPressed(state.current, event)
      setActiveAt(keyAnswerActiveAt(state.current))
    },
    early,
    counts: event => {
      const counted = keyAnswerClickCounts(state.current, event)
      if (!counted) {
        // The wait itself is unchanged; the card only says why nothing happened.
        setEarly(true)
        clearTimeout(earlyTimer.current)
        earlyTimer.current = setTimeout(() => setEarly(false), EARLY_HINT_MS)
      }
      return counted
    },
  }
}
