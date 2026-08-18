/* ProgressRail renders the shared compact status sequence above the composer. */

import { useLayoutEffect, useRef, useState } from 'react'

function ProgressStep({ item, detailsExpanded, onDetailsToggle }) {
  const stepRef = useRef(null)
  const labelRef = useRef(null)
  const [labelExpanded, setLabelExpanded] = useState(false)
  const [canExpand, setCanExpand] = useState(false)

  useLayoutEffect(() => {
    setLabelExpanded(false)
    setCanExpand(false)
  }, [item.label])

  useLayoutEffect(() => {
    const step = stepRef.current
    const label = labelRef.current
    if (!step || !label || labelExpanded || !item.expandable) return undefined

    const measure = () => {
      setCanExpand(label.scrollWidth > step.clientWidth + 1)
    }
    measure()

    if (typeof ResizeObserver === 'undefined') return undefined
    const observer = new ResizeObserver(measure)
    observer.observe(step)
    return () => observer.disconnect()
  }, [labelExpanded, item.expandable, item.label])

  const hasDetails = !!item.details
  const expanded = hasDetails ? detailsExpanded : labelExpanded
  const toggleable = item.expandable && (hasDetails || canExpand || expanded)
  const actionLabel = expanded
    ? item.expandedActionLabel
    : item.actionLabel
  const accessibleLabel = item.ariaLabel || item.label
  const title = item.title || item.label
  const className = `chat__progress-step${
    item.current ? ' chat__progress-step--current' : ''
  }${toggleable ? ' chat__progress-step--toggle' : ''}${
    labelExpanded ? ' chat__progress-step--expanded' : ''
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
        title={title}
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
        ? `${actionLabel || (expanded ? 'Collapse' : 'Expand')}: ${accessibleLabel}`
        : accessibleLabel}
      title={toggleable ? (actionLabel || (expanded ? 'Collapse' : title)) : title}
      disabled={!toggleable}
      onClick={() => {
        if (hasDetails) onDetailsToggle?.()
        else setLabelExpanded(value => !value)
      }}
    >
      {label}
      {hasDetails && (
        <>
          {actionLabel && (
            <span className="chat__progress-step-action" aria-hidden="true">
              {actionLabel}
            </span>
          )}
          <span className="chat__progress-toggle-mark" aria-hidden="true" />
        </>
      )}
    </button>
  )
}

export default function ProgressRail({ items, ariaLabel }) {
  const [detailsKey, setDetailsKey] = useState(null)
  if (!items.length) return null
  const detailItem = items.find(item => item.key === detailsKey && item.details)
  return (
    <div className="chat__progress-rail" role="group" aria-label={ariaLabel}>
      {items.map(item => (
        <ProgressStep
          key={item.key}
          item={item}
          detailsExpanded={detailsKey === item.key}
          onDetailsToggle={() => setDetailsKey(current => (
            current === item.key ? null : item.key
          ))}
        />
      ))}
      {detailItem && detailItem.details}
    </div>
  )
}
