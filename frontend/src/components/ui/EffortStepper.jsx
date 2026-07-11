import './EffortStepper.css'

/**
 * Shared reasoning-effort stepper — a single horizontal track with N
 * round stops and the selected level's long-form label to the right.
 * One component for every surface that picks effort (the composer
 * popover, Settings background agents, and any future picker) so the
 * control never drifts between them.
 *
 * Dumb by design: the caller passes the provider's `efforts` list
 * (`[{ value, label }]`, from PROVIDER_INFO), the current `value`, and
 * an `onChange`. Stops at/below the selected one read as filled; the
 * selected one sits proud with an accent ring, like a slider thumb.
 *
 * `onStopPointerDown` is the composer's escape hatch: the `+` popover
 * must not let a stop-tap move focus off the chat textarea (it would
 * pop the soft keyboard), so it passes `preserveFocusUnlessTouch`.
 * Surfaces without that constraint (Settings) omit it.
 */
export default function EffortStepper({
  efforts,
  value,
  onChange,
  disabled = false,
  ariaLabel = 'Reasoning effort',
  onStopPointerDown,
}) {
  if (!efforts || efforts.length === 0) return null
  // Default to index 0 when the persisted value isn't in this
  // provider's enum (e.g. a cross-provider effort carryover) so the
  // stepper always renders a valid selection instead of a blank track.
  const selectedIndex = Math.max(0, efforts.findIndex(e => e.value === value))
  const selected = efforts[selectedIndex] || efforts[0]
  return (
    <div className={`effort-stepper${disabled ? ' effort-stepper--disabled' : ''}`}>
      <div className="effort-stepper__track" role="radiogroup" aria-label={ariaLabel}>
        {efforts.map((effort, index) => (
          <button
            key={effort.value}
            type="button"
            role="radio"
            aria-checked={index === selectedIndex}
            aria-label={effort.label}
            disabled={disabled}
            // Roving tabindex: Tab lands on the selected stop, arrows
            // move within the group (idiomatic radiogroup nav).
            tabIndex={index === selectedIndex ? 0 : -1}
            className={
              'effort-stepper__stop'
              + (index === selectedIndex ? ' effort-stepper__stop--on' : '')
              + (index < selectedIndex ? ' effort-stepper__stop--filled' : '')
            }
            onPointerDown={onStopPointerDown}
            onClick={() => onChange(effort.value)}
            onKeyDown={(event) => {
              let next
              if (event.key === 'ArrowRight' || event.key === 'ArrowDown') {
                next = Math.min(efforts.length - 1, index + 1)
              } else if (event.key === 'ArrowLeft' || event.key === 'ArrowUp') {
                next = Math.max(0, index - 1)
              } else if (event.key === 'Home') {
                next = 0
              } else if (event.key === 'End') {
                next = efforts.length - 1
              } else {
                return
              }
              event.preventDefault()
              onChange(efforts[next].value)
              event.currentTarget.parentElement?.children[next]?.focus()
            }}
          />
        ))}
      </div>
      <span className="effort-stepper__label">{selected.label}</span>
    </div>
  )
}
