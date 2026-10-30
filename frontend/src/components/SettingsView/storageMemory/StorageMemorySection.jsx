/* Settings box that shows storage and memory and frees space without an agent. */
import { useEffect, useRef, useState } from 'react'
import { ArrowRotateCw, ChevronDown } from '@openai/apps-sdk-ui/components/Icon'
import StorageMemoryDetails from './StorageMemoryDetails.jsx'
import useStorageMemory from './useStorageMemory.js'
import { boxStatus, memoryMeter, resourceWarning, storageMeter, syncedLabel } from './storageMemoryPresentation.js'
import './StorageMemorySection.css'

function ResourceMeter({ meter }) {
  return (
    <div className={`storage-memory__meter${meter.tone ? ` storage-memory__meter--${meter.tone}` : ''}`}>
      <div className="storage-memory__meter-top">
        <span>{meter.label}</span>
        <span className="storage-memory__meter-value">{meter.value}</span>
      </div>
      <div className="storage-memory__bar" role="meter" aria-label={`${meter.label} used`} aria-valuemin={0} aria-valuemax={100} aria-valuenow={meter.percent} aria-valuetext={`${meter.value}. ${meter.support}. ${meter.note}`}>
        <span className="storage-memory__fill" style={{ width: `${meter.percent}%` }} />
        <span className="storage-memory__mark" style={{ left: `${meter.markPercent}%` }} />
      </div>
      <div className="storage-memory__meter-bottom"><span>{meter.support}</span><span>{meter.note}</span></div>
    </div>
  )
}

function ResourceNotice({ resources, warning, onReveal }) {
  if (resources.error) return (
    <div className="storage-memory__notice storage-memory__notice--warning" role="alert">
      <strong>Could not update measurements</strong>
      <p>{resources.overview ? 'Showing previous readings. ' : ''}{resources.error}</p>
      <button type="button" className="storage-memory__chat-link" disabled={resources.measuring} onClick={resources.measure}>Retry measurement</button>
    </div>
  )
  if (!warning) return null
  return (
    <div className={`storage-memory__notice storage-memory__notice--${warning.tone}`}>
      <strong>{warning.title}</strong><p>{warning.description}</p>
      <div className="storage-memory__notice-actions">{warning.actions.map(action => <button key={action.group} type="button" className="storage-memory__chat-link" onClick={() => onReveal(action.group)}>{action.label}</button>)}</div>
    </div>
  )
}

// Re-renders on a slow tick so "Synced 3m ago" keeps counting while open.
function useMinuteClock() {
  const [now, setNow] = useState(() => Date.now())
  useEffect(() => {
    const timer = setInterval(() => setNow(Date.now()), 30_000)
    return () => clearInterval(timer)
  }, [])
  return now
}

function SyncControl({ resources }) {
  const now = useMinuteClock()
  const { overview, measuring, error } = resources
  const label = syncedLabel({ measuredAt: overview?.measured_at, measuring, failed: Boolean(error) }, now)
  return (
    <div className="storage-memory__sync">
      <span className="storage-memory__sync-label">{label}</span>
      <button type="button" className="storage-memory__refresh" onClick={resources.measure} disabled={measuring}
        aria-label="Refresh storage and memory" title="Refresh storage and memory" aria-busy={measuring}>
        <ArrowRotateCw width={16} height={16} aria-hidden="true" />
      </button>
    </div>
  )
}

export default function StorageMemorySection(props) {
  const resources = useStorageMemory({ active: props.active })
  return <StorageMemorySectionView {...props} resources={resources} />
}

export function StorageMemorySectionView({ resources, focusRequest, onOpenApp, onOpenChat, sectionRef, inertBoundaryRef }) {
  const [expanded, setExpanded] = useState(false)
  const [focusGroup, setFocusGroup] = useState(null)
  const detailsRef = useRef(null)
  const { overview, error } = resources
  const status = boxStatus(overview)
  const warning = resourceWarning(overview)

  // Arriving from a waiting card or the storage alert means "help me free
  // space", so the details open with it.
  useEffect(() => {
    if (focusRequest) setExpanded(true)
  }, [focusRequest])
  useEffect(() => {
    if (!focusGroup) return
    const group = detailsRef.current?.querySelector(`[data-resource-group="${focusGroup.name}"]`)
    group?.focus({ preventScroll: true })
    group?.scrollIntoView({ block: 'nearest', behavior: 'instant' })
  }, [focusGroup])
  function reveal(name) { setExpanded(true); setFocusGroup({ name }) }

  return (
    <section ref={sectionRef} tabIndex={-1} className="settings__section storage-memory" aria-labelledby="storage-memory-title">
      <div className="storage-memory__heading">
        <h2 id="storage-memory-title" className="storage-memory__title">Storage and Memory</h2>
        {status && <span className={`storage-memory__status storage-memory__status--${status.tone}`} role="status">{status.label}</span>}
      </div>
      <ResourceNotice resources={resources} warning={warning} onReveal={reveal} />
      {!overview && !error && <p className="storage-memory__note" role="status">Checking storage and memory…</p>}
      {overview && (overview.storage
        ? <ResourceMeter meter={storageMeter(overview.storage)} />
        : <p className="storage-memory__note">Storage reading unavailable right now.</p>)}
      {overview && (overview.memory
        ? <ResourceMeter meter={memoryMeter(overview.memory)} />
        : <p className="storage-memory__note">Memory reading unavailable right now.</p>)}
      {overview && (
        <div className="storage-memory__controls">
          <button
            type="button"
            className="storage-memory__toggle"
            aria-expanded={expanded}
            aria-controls="storage-memory-details"
            onClick={() => setExpanded(open => !open)}
          >
            <span>{expanded ? 'Hide details' : 'Show details'}</span>
            <ChevronDown width={16} height={16} aria-hidden="true" />
          </button>
          <SyncControl resources={resources} />
        </div>
      )}
      {expanded && overview && <div ref={detailsRef}><StorageMemoryDetails id="storage-memory-details" resources={resources} onOpenApp={onOpenApp} onOpenChat={onOpenChat} inertBoundaryRef={inertBoundaryRef} /></div>}
    </section>
  )
}
