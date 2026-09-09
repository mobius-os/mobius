/* FileMentionMenu — the project-file picker that floats above the composer while typing "@". */

import { useEffect, useRef } from 'react'
import './SlashMenu.css'

/**
 * Presentational, exactly like SlashMenu and for the same reasons: the caller
 * owns matching, highlight, and acceptance; the textarea keeps focus the whole
 * time (rows prevent default on pointerdown) and points at the active row via
 * aria-activedescendant. Reuses the slash-menu styling so the two composer
 * pickers read as one system.
 */
export default function FileMentionMenu({ files, activeIndex, onSelect, listId, optionId }) {
  const activeRef = useRef(null)

  useEffect(() => {
    activeRef.current?.scrollIntoView({ block: 'nearest' })
  }, [activeIndex])

  if (!files.length) return null

  return (
    <div className="slash-menu-anchor">
      <ul className="slash-menu" id={listId} role="listbox" aria-label="Project files">
        {files.map((file, index) => {
          const active = index === activeIndex
          return (
            <li
              key={file.path}
              ref={active ? activeRef : null}
              id={active ? optionId : undefined}
              className={`slash-menu__item${active ? ' slash-menu__item--active' : ''}`}
              role="option"
              aria-selected={active}
              onPointerDown={(event) => event.preventDefault()}
              onClick={() => onSelect(file)}
            >
              <div className="slash-menu__line">
                <span className="slash-menu__name">{file.name}</span>
                <span className="slash-menu__args">{file.path}</span>
              </div>
            </li>
          )
        })}
      </ul>
    </div>
  )
}
