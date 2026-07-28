import BrainCircuit from 'lucide-react/dist/esm/icons/brain-circuit.mjs'
import {
  messageSources,
  sourceHost,
  sourceLabel,
} from './messageSources.js'
import { messageRecall, noteHref, noteLabel } from './memoryRecall.js'

function sourceMark(host) {
  const displayHost = String(host || '').replace(/^www\./i, '')
  return displayHost.match(/[a-z0-9]/i)?.[0]?.toUpperCase() || '•'
}

function MemoryMark() {
  return <BrainCircuit className="chat__source-glyph" aria-hidden="true" />
}

// Everything that informed an answer, surfaced ONCE at the end of the message:
// the notes the agent recalled from Memory, then the web sources it read. See
// memoryRecall.js / messageSources.js for where each comes from and why both
// are derived rather than carried as their own content blocks.
//
// Message level rather than inside the tool row, because a citation is a
// property of the ANSWER, not of the individual search that happened to find
// it: collapsed tool rows hid them, and one search's results are rarely the
// whole citation set.
//
// The recall row exists to make three states distinguishable at a glance —
// remembered these notes / looked and found nothing / never looked (no row).
// The middle state is the one that earns trust, and it is also the prompt to
// write the note that was missing.

export default function MessageSources({ blocks, onInternalNav }) {
  const sources = messageSources(blocks)
  const recall = messageRecall(blocks)
  const notes = recall?.notes || []
  if (sources.length === 0 && !recall) return null

  const handleNoteClick = (event, href) => {
    if (!onInternalNav || !href) return
    // Let the browser own a modified click (new tab, download, middle button)
    // exactly as it does for an ordinary link.
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

  return (
    <section className="chat__sources" aria-label="What informed this answer">
      <ul className="chat__sources-list">
        {notes.map(note => {
          const label = noteLabel(note)
          const href = noteHref(note)
          const body = (
            <>
              <span className="chat__source-icon" aria-hidden="true">
                <MemoryMark />
              </span>
              <span className="chat__source-copy">
                <span className="chat__source-title">{label}</span>
                <span className="chat__source-host" aria-hidden="true">
                  Memory
                </span>
              </span>
            </>
          )
          return (
            <li key={note.path || note.id} className="chat__source-item">
              {href ? (
                <a
                  className="chat__source-chip chat__source-chip--memory"
                  href={href}
                  title={note.excerpt || label}
                  aria-label={`${label} — recalled from Memory`}
                  onClick={event => handleNoteClick(event, href)}
                >
                  {body}
                </a>
              ) : (
                <span
                  className="chat__source-chip chat__source-chip--memory"
                  title={note.excerpt || label}
                >
                  {body}
                </span>
              )}
            </li>
          )
        })}
        {/* A lookup that came back empty. Deliberately quiet and unclickable:
            it is a fact about the answer, not a destination. */}
        {recall?.empty && (
          <li className="chat__source-item">
            <span className="chat__source-chip chat__source-chip--memory chat__source-chip--quiet">
              <span className="chat__source-icon" aria-hidden="true">
                <MemoryMark />
              </span>
              <span className="chat__source-copy">
                <span className="chat__source-title">
                  Looked back — nothing on this yet
                </span>
              </span>
            </span>
          </li>
        )}
        {sources.map(source => {
          const label = sourceLabel(source)
          const host = sourceHost(source.url)
          return (
            <li key={source.url} className="chat__source-item chat__source-item--web">
              <a
                className="chat__source-chip"
                href={source.url}
                target="_blank"
                rel="noopener noreferrer"
                title={source.snippet || source.title || source.url}
                aria-label={`${label}${host && host !== label ? ` — ${host}` : ''} (opens in a new tab)`}
              >
                {/* Keep reading passive: a local domain mark avoids contacting
                    every cited site merely because its card neared the viewport. */}
                <span className="chat__source-icon" aria-hidden="true">
                  {sourceMark(host)}
                </span>
                <span className="chat__source-title">{label}</span>
              </a>
            </li>
          )
        })}
      </ul>
    </section>
  )
}
