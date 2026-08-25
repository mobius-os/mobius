/* ProgressRail renders the shared compact status sequence above the composer. */

import { useEffect, useState } from 'react'
import { X } from '@openai/apps-sdk-ui/components/Icon'

function ProgressStep({ item, detailsExpanded, onDetailsToggle, onClear, onAction }) {
  const [labelExpanded, setLabelExpanded] = useState(false)
  useEffect(() => setLabelExpanded(false), [item.label])

  const hasDetails = !!item.details
  const expanded = hasDetails ? detailsExpanded : labelExpanded
  const accessibleLabel = item.ariaLabel || item.label
  const title = item.title || item.label
  const clearable = !!(item.clearable && onClear)
  const actionable = !!(item.actionLabel && onAction)
  const stepModifiers = `${item.current ? ' chat__progress-step--current' : ''}${
    item.expandable ? ' chat__progress-step--toggle' : ''
  }${labelExpanded ? ' chat__progress-step--expanded' : ''}${
    item.tone ? ` chat__progress-step--${item.tone}` : ''
  }`
  const label = (
    <span className="chat__progress-step-label">
      {item.label}
    </span>
  )

  if (!item.expandable) {
    return (
      <span
        className={`chat__progress-step${stepModifiers}`}
        aria-current={item.current ? 'step' : undefined}
        title={title}
      >
        {label}
      </span>
    )
  }

  // When the item has sibling actions, the step is a wrapper holding the
  // toggle plus buttons — a button cannot nest inside the toggle button.
  const toggle = (
    <button
      type="button"
      className={(clearable || actionable)
        ? 'chat__progress-step--button chat__progress-step--toggle'
        : `chat__progress-step${stepModifiers} chat__progress-step--button`}
      aria-current={item.current ? 'step' : undefined}
      aria-expanded={expanded}
      aria-label={`${expanded ? 'Collapse' : 'Expand'}: ${accessibleLabel}`}
      title={expanded ? 'Collapse' : title}
      // Keep composer focus (and the soft keyboard) put when expanding.
      onPointerDown={(event) => event.preventDefault()}
      onClick={() => {
        if (hasDetails) onDetailsToggle?.()
        else setLabelExpanded(value => !value)
      }}
    >
      {label}
    </button>
  )

  if (!clearable && !actionable) return toggle

  return (
    <span className={`chat__progress-step${stepModifiers} chat__progress-step--clearable`}>
      {toggle}
      {actionable && (
        <button
          type="button"
          className="chat__progress-action"
          onPointerDown={(event) => event.preventDefault()}
          onClick={() => onAction(item)}
          aria-label={item.actionAriaLabel || item.actionLabel}
          title={item.actionAriaLabel || item.actionLabel}
        >
          {item.actionIcon || item.actionLabel}
        </button>
      )}
      {clearable && (
        <button
          type="button"
          className="chat__progress-clear"
          // Keep composer focus (and the soft keyboard) put when clearing.
          onPointerDown={(event) => event.preventDefault()}
          onClick={() => onClear()}
          // Label text is item-supplied so the shared rail stays domain-neutral.
          aria-label={item.clearLabel || 'Clear'}
          title={item.clearLabel || 'Clear'}
        >
          <X width={14} height={14} aria-hidden="true" />
        </button>
      )}
    </span>
  )
}

export default function ProgressRail({
  items, ariaLabel, resetKey, onClearItem, onActionItem,
}) {
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
          onClear={item.clearable ? onClearItem : undefined}
          onAction={item.actionLabel ? onActionItem : undefined}
        />
      ))}
      {detailItem && detailItem.details}
    </div>
  )
}
