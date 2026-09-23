import { useId, useRef } from 'react'
import { ChevronRight } from '@openai/apps-sdk-ui/components/Icon'
import { appActivityCardModel } from './appActivityCard.js'
import { preserveTogglePosition } from './preserveTogglePosition.js'
import { ActivityTypeIcon } from './ActivityLineHeader.jsx'
import { useDisclosureState } from './disclosureState.js'
import { toolActivityIcon, toolCallLabel } from './toolActivityLabel.js'

function openInternal(event, href, onInternalNav) {
  if (!onInternalNav || !href) return
  if (event.metaKey || event.ctrlKey || event.shiftKey || event.altKey
      || event.button !== 0) return
  let url
  try {
    url = new URL(href, window.location.href)
  } catch {
    return
  }
  event.preventDefault()
  onInternalNav(url)
}

export default function AppActivityCard({
  t, chatId, disclosureKey, onInternalNav,
}) {
  const model = appActivityCardModel(t?.app_activity)
  const [open, setOpen] = useDisclosureState(chatId, disclosureKey)
  const headerRef = useRef(null)
  const detailRef = useRef(null)
  const headerId = useId()
  const detailId = useId()
  if (!model) return null

  const live = model.status === 'running' || t?.status === 'running'
  const label = toolCallLabel(t)
  const iconKind = toolActivityIcon('AppActivity')

  return (
    <div className={
      `chat__tool chat__tool--${live ? 'running' : 'done'} chat__tool--compact`
      + ` chat__app-activity-tool chat__app-activity-tool--${model.status}`
    }>
      <button
        ref={headerRef}
        id={headerId}
        type="button"
        className="chat__tool-header"
        onClick={() => {
          preserveTogglePosition(headerRef.current, detailRef.current)
          setOpen(value => !value)
        }}
        aria-expanded={open}
        aria-controls={detailId}
        aria-label={`${label}${live ? ', in progress' : ''}`}
      >
        <span
          className={`chat__tool-icon${live ? ' chat__tool-icon--running' : ''}`}
          data-tool-kind={iconKind}
          aria-hidden="true"
        >
          <ActivityTypeIcon kind={iconKind} />
        </span>
        <span className="chat__tool-name" title={label}>
          {label}{live ? '…' : ''}
        </span>
      </button>

      <div
        ref={detailRef}
        id={detailId}
        className="chat__tool-detail chat__app-activity-detail"
        role="region"
        aria-labelledby={headerId}
        tabIndex={open ? 0 : undefined}
        hidden={!open}
      >
        {open && (
          <>
            <div className="chat__app-activity-section">
              <span className="chat__app-activity-kicker">{model.appName}</span>
              <p className="chat__app-activity-state">
                {model.detail || model.label}
              </p>
              {model.receiptMissing && (
                <p className="chat__app-activity-state">
                  The command completed, but its app receipt was unavailable.
                </p>
              )}
              {model.warning && (
                <p className="chat__app-activity-state chat__app-activity-state--failed">
                  {model.warning}
                </p>
              )}
            </div>

            {model.resources.length > 0 && (
              <div className="chat__app-activity-section chat__app-activity-results">
                <span className="chat__app-activity-kicker">Results</span>
                <ul className="chat__app-activity-list">
                  {model.resources.map(resource => (
                    <li key={resource.key}>
                      {resource.href ? (
                        <a
                          className="chat__app-activity-resource"
                          href={resource.href}
                          onClick={event => openInternal(
                            event, resource.href, onInternalNav,
                          )}
                        >
                          <span className="chat__app-activity-resource-copy">
                            <strong>{resource.label}</strong>
                            {resource.summary && <span>{resource.summary}</span>}
                          </span>
                          <ChevronRight width={14} height={14} aria-hidden="true" />
                        </a>
                      ) : (
                        <span className="chat__app-activity-resource chat__app-activity-resource--static">
                          <span className="chat__app-activity-resource-copy">
                            <strong>{resource.label}</strong>
                            {resource.summary && <span>{resource.summary}</span>}
                          </span>
                        </span>
                      )}
                    </li>
                  ))}
                </ul>
              </div>
            )}
          </>
        )}
      </div>
    </div>
  )
}
