import { memo, useId, useRef } from 'react'
import { ChevronDown } from '@openai/apps-sdk-ui/components/Icon'
import useMessageSources from './hooks/useMessageSources.js'
import {
  sourceDisplayLabels,
  sourceFaviconDiscoveryUrl,
  sourceFaviconUrl,
  sourceHost,
  sourceLabel,
} from './messageSources.js'
import SourceFavicon from './SourceFavicon.jsx'
import { useDisclosureState } from './disclosureState.js'
import { preserveTogglePosition } from './preserveTogglePosition.js'

function sourceMark(host) {
  const displayHost = String(host || '').replace(/^www\./i, '')
  return displayHost.match(/[a-z0-9]/i)?.[0]?.toUpperCase() || '•'
}

// Web references that informed an answer. Historical chat payloads carry only
// source indices; the link metadata is read when this disclosure first opens.
// A just-completed live answer already has the same bounded metadata in its
// tool blocks, so it can expand without an unnecessary round trip.
function MessageSources({
  chatId,
  groups,
  refs,
  disclosureKey,
}) {
  const [open, setOpen] = useDisclosureState(chatId, disclosureKey)
  const { sources, hasSources, count, failed, complete, retry } = useMessageSources({
    chatId,
    groups,
    refs,
    open,
  })
  const toggleRef = useRef(null)
  const bodyRef = useRef(null)
  const bodyId = useId()
  if (!hasSources) return null
  const labels = sourceDisplayLabels(sources)
  const toggle = () => {
    preserveTogglePosition(toggleRef.current, bodyRef.current)
    setOpen(value => !value)
  }

  return (
    <section className={`chat__sources${open ? ' chat__sources--open' : ''}`}>
      <button
        ref={toggleRef}
        type="button"
        className="chat__sources-toggle"
        onClick={toggle}
        aria-expanded={open}
        aria-controls={bodyId}
      >
        <span className="chat__sources-label">References</span>
        {count !== null && <span className="chat__sources-count">{count}</span>}
        <ChevronDown
          className="chat__sources-chevron"
          width={16}
          height={16}
          aria-hidden="true"
        />
      </button>
      <div
        ref={bodyRef}
        id={bodyId}
        className="chat__sources-body"
        hidden={!open}
      >
        {open && !complete && !failed && (
          <span className="chat__sources-status" role="status" aria-live="polite">
            Loading references…
          </span>
        )}
        {open && failed && (
          <div className="chat__lazy-status">
            <span className="chat__sources-status" role="status" aria-live="polite">
              Some references could not load.
            </span>
            <button type="button" className="chat__lazy-retry" onClick={retry}>
              Retry
            </button>
          </div>
        )}
        {open && sources.length > 0 && (
          <ul className="chat__sources-list" aria-label="References for this answer">
            {sources.map((source, index) => {
              const label = labels[index]
              const baseLabel = sourceLabel(source)
              const host = sourceHost(source.url)
              const faviconUrl = sourceFaviconUrl(source.url)
              const faviconDiscoveryUrl = sourceFaviconDiscoveryUrl(source.url)
              return (
                <li key={source.url} className="chat__source-item chat__source-item--web">
                  <a
                    className="chat__source-chip"
                    href={source.url}
                    target="_blank"
                    rel="noopener noreferrer"
                    title={source.title || source.url}
                    aria-label={`${label}${host && label === baseLabel && host !== label ? ` — ${host}` : ''} (opens in a new tab)`}
                  >
                    <SourceFavicon
                      faviconUrl={faviconUrl}
                      discoveryUrl={faviconDiscoveryUrl}
                      fallback={sourceMark(host)}
                    />
                    <span className="chat__source-title">{label}</span>
                  </a>
                </li>
              )
            })}
          </ul>
        )}
      </div>
    </section>
  )
}

// Pagination rebuilds reply groups around unchanged historical messages. The
// source hook serializes every block group, so skip that work when its actual
// inputs (rather than the freshly allocated wrapper arrays) are unchanged.
export function sameMessageSourcesProps(previous, next) {
  if (previous.chatId !== next.chatId || previous.disclosureKey !== next.disclosureKey) return false
  if (previous.groups.length !== next.groups.length || previous.refs.length !== next.refs.length) return false
  return previous.groups.every((blocks, index) => blocks === next.groups[index])
    && previous.refs.every((ref, index) => ref === next.refs[index])
}

export default memo(MessageSources, sameMessageSourcesProps)
