/* ProgressRail renders the shared compact status sequence above the composer. */

import { useLayoutEffect, useRef, useState } from 'react'

function ProgressStep({ item }) {
  const stepRef = useRef(null)
  const labelRef = useRef(null)
  const [expanded, setExpanded] = useState(false)
  const [canExpand, setCanExpand] = useState(false)

  useLayoutEffect(() => {
    setExpanded(false)
    setCanExpand(false)
  }, [item.label])

  useLayoutEffect(() => {
    const step = stepRef.current
    const label = labelRef.current
    if (!step || !label || expanded || !item.expandable) return undefined

    const measure = () => {
      setCanExpand(label.scrollWidth > step.clientWidth + 1)
    }
    measure()

    if (typeof ResizeObserver === 'undefined') return undefined
    const observer = new ResizeObserver(measure)
    observer.observe(step)
    return () => observer.disconnect()
  }, [expanded, item.expandable, item.label])

  const toggleable = item.expandable && (canExpand || expanded)
  const className = `chat__progress-step${
    item.current ? ' chat__progress-step--current' : ''
  }${toggleable ? ' chat__progress-step--toggle' : ''}${
    expanded ? ' chat__progress-step--expanded' : ''
  }`
  const label = (
    <span ref={labelRef} className="chat__progress-step-label">
      {item.label}
    </span>
  )

  if (!item.expandable) {
    return (
      <span
        ref={stepRef}
        className={className}
        aria-current={item.current ? 'step' : undefined}
        title={item.label}
      >
        {label}
      </span>
    )
  }

  return (
    <button
      ref={stepRef}
      type="button"
      className={`${className} chat__progress-step--button`}
      aria-current={item.current ? 'step' : undefined}
      aria-expanded={expanded}
      aria-label={toggleable
        ? `${expanded ? 'Collapse' : 'Expand'}: ${item.label}`
        : item.label}
      title={toggleable ? (expanded ? 'Collapse' : item.label) : item.label}
      disabled={!toggleable}
      onClick={() => setExpanded(value => !value)}
    >
      {label}
    </button>
  )
}

export default function ProgressRail({ items, ariaLabel }) {
  if (!items.length) return null
  return (
    <div className="chat__progress-rail" role="group" aria-label={ariaLabel}>
      {items.map(item => (
        <ProgressStep key={item.key} item={item} />
      ))}
    </div>
  )
}
