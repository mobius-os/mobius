import { useEffect, useId, useMemo, useRef, useState } from 'react'
import { ChevronDown } from '@openai/apps-sdk-ui/components/Icon'
import { apiFetch, jsonOrThrow } from '../../api/client.js'
import {
  messageSources,
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

export function messageSourcesUrl(chatId, messageIndex) {
  return `/chats/${encodeURIComponent(chatId)}/message-sources`
    + `?message_index=${encodeURIComponent(messageIndex)}`
}

// Web references that informed an answer. Historical chat payloads carry only
// sourceRef; the link metadata is read when this disclosure first opens.
// A just-completed live answer already has the same bounded metadata in its
// tool blocks, so it can expand without an unnecessary round trip.
export default function MessageSources({
  blocks,
  chatId,
  sourceRef = null,
  disclosureKey,
}) {
  const inlineSources = useMemo(() => messageSources(blocks), [blocks])
  const remoteMessageIndex = Number.isInteger(sourceRef?.message_index)
    ? sourceRef.message_index
    : null
  const count = Number.isInteger(sourceRef?.count) && sourceRef.count > 0
    ? sourceRef.count
    : inlineSources.length
  const [open, setOpen] = useDisclosureState(chatId, disclosureKey)
  const [loadedSources, setLoadedSources] = useState(
    () => inlineSources.length > 0 ? inlineSources : null,
  )
  const [loadError, setLoadError] = useState(false)
  const [loadAttempt, setLoadAttempt] = useState(0)
  const toggleRef = useRef(null)
  const bodyRef = useRef(null)
  const bodyId = useId()
  const remoteKey = remoteMessageIndex == null
    ? ''
    : `${chatId}:${remoteMessageIndex}`

  useEffect(() => {
    setLoadedSources(inlineSources.length > 0 ? inlineSources : null)
    setLoadError(false)
  }, [inlineSources, remoteKey])

  useEffect(() => {
    if (!open || loadedSources !== null || loadError
        || remoteMessageIndex == null) return undefined
    const controller = new AbortController()
    let current = true
    apiFetch(messageSourcesUrl(chatId, remoteMessageIndex), {
      signal: controller.signal,
    })
      .then(response => jsonOrThrow(response, 'References failed to load'))
      .then(data => {
        if (!current) return
        setLoadedSources(messageSources([{
          type: 'tool',
          sources: Array.isArray(data.sources) ? data.sources : [],
        }]))
      })
      .catch(error => {
        if (!current || error?.name === 'AbortError') return
        setLoadError(true)
      })
    return () => {
      current = false
      controller.abort()
    }
  }, [
    chatId,
    loadAttempt,
    loadError,
    loadedSources,
    open,
    remoteKey,
    remoteMessageIndex,
  ])

  if (count === 0) return null
  const sources = loadedSources || []
  const labels = sourceDisplayLabels(sources)
  const toggle = () => {
    preserveTogglePosition(toggleRef.current, bodyRef.current)
    setOpen(value => !value)
  }
  const retry = () => {
    setLoadError(false)
    setLoadAttempt(value => value + 1)
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
        <span className="chat__sources-count">{count}</span>
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
        {open && loadedSources === null && !loadError && (
          <span className="chat__sources-status" role="status" aria-live="polite">
            Loading references…
          </span>
        )}
        {open && loadError && (
          <div className="chat__lazy-status">
            <span className="chat__sources-status" role="status" aria-live="polite">
              References unavailable.
            </span>
            <button type="button" className="chat__lazy-retry" onClick={retry}>
              Retry
            </button>
          </div>
        )}
        {open && loadedSources !== null && (
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
                    title={source.snippet || source.title || source.url}
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
