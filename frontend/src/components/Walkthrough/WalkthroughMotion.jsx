/* Text and loop primitives for the first-run guide. Every effect renders its
   final state when the visitor prefers reduced motion, and screen readers get
   the complete text immediately instead of a half-typed string. */
import { useEffect, useState, useSyncExternalStore } from 'react'

const REDUCED_QUERY = '(prefers-reduced-motion: reduce)'

function subscribeReducedMotion(notify) {
  const media = window.matchMedia?.(REDUCED_QUERY)
  media?.addEventListener?.('change', notify)
  return () => media?.removeEventListener?.('change', notify)
}

export function usePrefersReducedMotion() {
  return useSyncExternalStore(
    subscribeReducedMotion,
    () => Boolean(window.matchMedia?.(REDUCED_QUERY)?.matches),
    () => false,
  )
}

/* Types the text one character at a time with a blinking caret. By default the full text is laid out
   invisibly so nothing around it moves; `reserve={false}` lets the box grow as it types, like a message box. */
export function Typewriter({ text, speed = 22, startDelay = 250, reserve = true, placeholder = null }) {
  const reduced = usePrefersReducedMotion()
  const [count, setCount] = useState(0)
  useEffect(() => {
    if (reduced) return undefined
    let index = 0
    let interval
    const start = setTimeout(() => {
      interval = setInterval(() => {
        index += 1
        setCount(index)
        if (index >= text.length) clearInterval(interval)
      }, speed)
    }, startDelay)
    return () => { clearTimeout(start); clearInterval(interval); setCount(0) }
  }, [text, speed, startDelay, reduced])
  const shown = reduced ? text.length : count
  if (!reserve) return <span className="wt-typewriter">
    <span className="sr-only">{text}</span>
    {/* Until the first letter lands, a message box shows its placeholder, as the real one does. */}
    {shown === 0 && placeholder
      ? <span className="wt-pill__placeholder" aria-hidden="true">{placeholder}</span>
      : <span aria-hidden="true">{text.slice(0, shown)}{shown < text.length && <i className="wt-caret" />}</span>}
  </span>
  return <span className="wt-typewriter">
    <span className="sr-only">{text}</span>
    <span className="wt-typewriter__stage" aria-hidden="true">
      <span className="wt-typewriter__ghost">{text}</span>
      <span className="wt-typewriter__live">{text.slice(0, shown)}{shown < text.length && <i className="wt-caret" />}</span>
    </span>
  </span>
}

/* Words settle in one after another. Screen readers get the whole sentence at once. */
export function WordReveal({ text, stagger = 42, delay = 0 }) {
  return <span className="wt-words">
    <span className="sr-only">{text}</span>
    {text.split(' ').map((word, index) => <span
      key={`${index}-${word}`}
      aria-hidden="true"
      className="wt-words__word"
      style={{ '--wt-i': index, '--wt-stagger': `${stagger}ms`, '--wt-delay': `${delay}ms` }}
    >{word}</span>)}
  </span>
}

/* A looping timeline over a fixed array of per-phase durations (ms). Returns
   the phase index currently shown, wraps around, and stops on unmount.
   Pass a module-level array so its identity is stable. Reduced motion pins the
   last phase, which each caller designs as its complete state. */
export function useLoopingTimeline(durations) {
  const reduced = usePrefersReducedMotion()
  const [index, setIndex] = useState(0)
  useEffect(() => {
    if (reduced) return undefined
    const timer = setTimeout(() => setIndex(current => (current + 1) % durations.length), durations[index])
    return () => clearTimeout(timer)
  }, [index, durations, reduced])
  return reduced ? durations.length - 1 : index
}

/* How headings and intro text arrive. Each screen uses one of these, all short and restrained:
   rise  - a short fade and lift
   mask  - the line slides up out of a clip, like an editorial title card
   blur  - the text comes into focus from a soft blur
   drift - the text eases in from the left
   Only the first screen's intro is typed, and a few intros arrive word by word. */
export function Reveal({ kind = 'rise', delay = 0, children }) {
  const style = { '--wt-delay': `${delay}ms` }
  if (kind === 'mask') return <span className="wt-reveal wt-reveal--mask"><span className="wt-reveal__inner" style={style}>{children}</span></span>
  return <span className={`wt-reveal wt-reveal--${kind}`} style={style}>{children}</span>
}
