import { useId } from 'react'
import { messageSources, sourceHost, sourceLabel } from './messageSources.js'

function sourceMark(host) {
  const displayHost = String(host || '').replace(/^www\./i, '')
  return displayHost.match(/[a-z0-9]/i)?.[0]?.toUpperCase() || '•'
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
          const displayHost = host.replace(/^www\./i, '')
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
                {/* A local domain mark is deliberate: remote favicons would
                    contact every cited site merely by viewing an answer. */}
                <span className="chat__source-icon" aria-hidden="true">
                  {sourceMark(host)}
                </span>
                <span className="chat__source-copy">
                  <span className="chat__source-title">{label}</span>
                  {/* A title-less Codex source already reads as its host. */}
                  {host && host !== label && (
                    <span className="chat__source-host" aria-hidden="true">
                      {displayHost}
                    </span>
                  )}
                </span>
              </a>
            </li>
          )
        })}
      </ul>
    </section>
  )
}
