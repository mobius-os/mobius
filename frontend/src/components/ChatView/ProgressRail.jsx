/* ProgressRail renders the shared compact status sequence above the composer. */

import { useEffect, useState } from 'react'

function ProgressStep({ item, detailsExpanded, onDetailsToggle }) {
  const [labelExpanded, setLabelExpanded] = useState(false)
  useEffect(() => setLabelExpanded(false), [item.label])

  const hasDetails = !!item.details
  const expanded = hasDetails ? detailsExpanded : labelExpanded
  const accessibleLabel = item.ariaLabel || item.label
  const title = item.title || item.label
  const className = `chat__progress-step${
    item.current ? ' chat__progress-step--current' : ''
  }${item.expandable ? ' chat__progress-step--toggle' : ''}${
    labelExpanded ? ' chat__progress-step--expanded' : ''
  }`
  const label = (
    <span className="chat__progress-step-label">
      {item.label}
    </span>
  )

  if (!item.expandable) {
    return (
      <span
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
      type="button"
      className={`${className} chat__progress-step--button`}
      aria-current={item.current ? 'step' : undefined}
      aria-expanded={expanded}
      aria-label={`${expanded ? 'Collapse' : 'Expand'}: ${accessibleLabel}`}
      title={expanded ? 'Collapse' : title}
      onClick={() => {
        if (hasDetails) onDetailsToggle?.()
        else setLabelExpanded(value => !value)
      }}
    >
      {label}
      {hasDetails && (
        <span className="chat__progress-toggle-mark" aria-hidden="true" />
      )}
    </button>
  )
}

export default function ProgressRail({ items, ariaLabel, resetKey }) {
  const [detailsKey, setDetailsKey] = useState(null)
  useEffect(() => setDetailsKey(null), [resetKey])
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
