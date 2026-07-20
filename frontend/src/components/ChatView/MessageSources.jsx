import { useId } from 'react'
import { messageSources, sourceHost, sourceLabel } from './messageSources.js'

function SourceGlobeIcon() {
  return (
    <svg viewBox="0 0 16 16" fill="none" stroke="currentColor"
      strokeWidth="1.35" strokeLinecap="round" strokeLinejoin="round">
      <circle cx="8" cy="8" r="5.5" />
      <path d="M2.5 8h11M8 2.5c2 1.8 2 9.2 0 11-2-1.8-2-9.2 0-11Z" />
    </svg>
  )
}

function ExternalLinkIcon() {
  return (
    <svg viewBox="0 0 16 16" fill="none" stroke="currentColor"
      strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
      <path d="M6 3h7v7M13 3 7.25 8.75" />
      <path d="M11 8.5v3A1.5 1.5 0 0 1 9.5 13h-6A1.5 1.5 0 0 1 2 11.5v-6A1.5 1.5 0 0 1 3.5 4H7" />
    </svg>
  )
}

// The web sources that informed an answer, surfaced ONCE at the end of the
// message — see messageSources.js for where the data comes from and why it is
// derived rather than carried as its own content block.
//
// Message level rather than inside the tool row, because a source is a
// property of the ANSWER, not of the individual search that happened to find
// it: collapsed tool rows hid them, and one search's results are rarely the
// whole citation set.

export default function MessageSources({ blocks }) {
  const labelId = useId()
  const sources = messageSources(blocks)
  if (sources.length === 0) return null

  return (
    <section className="chat__sources" aria-labelledby={labelId}>
      <span id={labelId} className="chat__sources-label">Sources</span>
      <ul className="chat__sources-list">
        {sources.map(source => {
          const label = sourceLabel(source)
          const host = sourceHost(source.url)
          return (
            <li key={source.url} className="chat__source-item">
              <a
                className="chat__source-chip"
                href={source.url}
                target="_blank"
                rel="noopener noreferrer"
                title={source.snippet || source.title || source.url}
                aria-label={`${label}${host && host !== label ? ` — ${host}` : ''} (opens in a new tab)`}
              >
                {/* A local glyph is deliberate: remote favicons would make
                    merely viewing an answer contact every cited domain. */}
                <span className="chat__source-icon" aria-hidden="true">
                  <SourceGlobeIcon />
                </span>
                <span className="chat__source-copy">
                  <span className="chat__source-title">{label}</span>
                  {/* A title-less Codex source already reads as its host. */}
                  {host && host !== label && (
                    <span className="chat__source-host">{host}</span>
                  )}
                </span>
                <span className="chat__source-open" aria-hidden="true">
                  <ExternalLinkIcon />
                </span>
              </a>
            </li>
          )
        })}
      </ul>
    </section>
  )
}
