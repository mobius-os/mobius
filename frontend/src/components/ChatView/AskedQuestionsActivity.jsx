/* The "Asked a question" activity row: a tool-call row whose detail lists what a card asked. */

import { useId, useRef } from 'react'
import { InlineMarkdown, StandardMarkdown } from './markdown/BlockRenderer.jsx'
import { ActivityTypeIcon } from './ActivityLineHeader.jsx'
import { useDisclosureState } from './disclosureState.js'
import { preserveTogglePosition } from './preserveTogglePosition.js'
import { toolActivityIcon, toolCallLabel } from './toolActivityLabel.js'
import { ASKED_QUESTIONS_TOOL } from './askedQuestionsActivity.js'

/**
 * Rendered by ToolBlock for the AskedQuestions activity, in the generic tool
 * row's own classes. Its detail is read-only: nothing in it can be chosen,
 * typed or sent.
 */
export default function AskedQuestionsActivity({ t, chatId, compact = false, disclosureKey, onInternalNav }) {
  const [open, setOpen] = useDisclosureState(chatId, disclosureKey)
  const headerRef = useRef(null)
  const detailRef = useRef(null)
  const headerId = useId()
  const detailId = useId()
  const label = toolCallLabel(t)
  const iconKind = toolActivityIcon(ASKED_QUESTIONS_TOOL)
  return (
    <div className={`chat__tool chat__tool--done${compact ? ' chat__tool--compact' : ''}`}>
      <button
        ref={headerRef}
        id={headerId}
        type="button"
        className="chat__tool-header"
        aria-expanded={open}
        aria-controls={detailId}
        onClick={() => {
          preserveTogglePosition(headerRef.current, detailRef.current)
          setOpen(o => !o)
        }}
      >
        <span className="chat__tool-icon" data-tool-kind={iconKind} aria-hidden="true">
          <ActivityTypeIcon kind={iconKind} />
        </span>
        <span className="chat__tool-name" title={label}>{label}</span>
      </button>
      <div
        ref={detailRef}
        id={detailId}
        className="chat__tool-detail chat__asked-detail"
        data-chat-scroll-region
        tabIndex={open ? 0 : undefined}
        role="region"
        aria-labelledby={headerId}
        hidden={!open}
      >
        {open && (t.asked || []).map(entry => (
          <div key={entry.key} className="chat__tool-section chat__asked-entry">
            {entry.label && <span className="chat__tool-section-label">{entry.label}</span>}
            <div className="chat__asked-prompt">
              <StandardMarkdown text={entry.prompt} literalTags onInternalNav={onInternalNav} />
            </div>
            {entry.options.length > 0 && (
              <ul className="chat__asked-options">
                {entry.options.map((option, i) => (
                  <li key={i}>
                    <InlineMarkdown text={option.label} />
                    {option.description && <span className="chat__asked-option-desc"> · <InlineMarkdown text={option.description} /></span>}
                  </li>
                ))}
              </ul>
            )}
          </div>
        ))}
      </div>
    </div>
  )
}
