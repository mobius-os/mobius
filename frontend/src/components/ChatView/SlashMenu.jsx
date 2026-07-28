/* SlashMenu — the command picker that floats above the composer while typing "/". */

import { useEffect, useRef } from 'react'
import './SlashMenu.css'

/**
 * Command list for the composer's "/" menu.
 *
 * Presentational: the caller owns which commands match, which one is
 * highlighted, and what accepting does. Selection is driven from the composer's
 * keydown handler rather than from focus, because the textarea must KEEP focus
 * the whole time — moving focus into the list would collapse the soft keyboard
 * on touch and break the typing flow the menu exists to support. That is also
 * why every row prevents default on pointerdown.
 *
 * Accessibility follows the combobox pattern for exactly this arrangement: the
 * textarea stays the focused control and points at the active row through
 * aria-activedescendant, so a screen reader announces the highlighted command
 * without the focus ever leaving the input.
 */
export default function SlashMenu({
  commands,
  activeIndex,
  onSelect,
  isAvailable,
  unavailableReason,
  listId,
  optionId,
}) {
  const activeRef = useRef(null)

  // Keep the highlighted row in view when arrowing past the visible window.
  useEffect(() => {
    activeRef.current?.scrollIntoView({ block: 'nearest' })
  }, [activeIndex])

  if (!commands.length) return null

  return (
    <div className="slash-menu-anchor">
      <ul className="slash-menu" id={listId} role="listbox" aria-label="Commands">
        {commands.map((command, index) => {
          const active = index === activeIndex
          const available = isAvailable(command)
          return (
            <li
              key={command.name}
              ref={active ? activeRef : null}
              id={active ? optionId : undefined}
              className={`slash-menu__item${active ? ' slash-menu__item--active' : ''}${available ? '' : ' slash-menu__item--unavailable'}`}
              role="option"
              aria-selected={active}
              aria-disabled={!available}
              // Pointerdown would move focus off the textarea before the click
              // lands, closing the keyboard on touch and dropping the caret.
              onPointerDown={(event) => event.preventDefault()}
              onClick={() => { if (available) onSelect(command) }}
            >
              <div className="slash-menu__line">
                <span className="slash-menu__name">/{command.name}</span>
                <span className="slash-menu__args">{command.args}</span>
              </div>
              <div className="slash-menu__summary">{command.summary}</div>
              {(available ? command.detail : unavailableReason(command)) && (
                <div className="slash-menu__detail">
                  {available ? command.detail : unavailableReason(command)}
                </div>
              )}
            </li>
          )
        })}
      </ul>
    </div>
  )
}
