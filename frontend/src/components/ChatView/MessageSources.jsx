import { messageSources, sourceHost, sourceLabel } from './messageSources.js'

function sourceMark(host) {
  const displayHost = String(host || '').replace(/^www\./i, '')
  return displayHost.match(/[a-z0-9]/i)?.[0]?.toUpperCase() || '•'
}

// Web sources that informed an answer, surfaced once at the end of the message.
// Memory results now stay with their lookup card, where the question, result
// summaries, and note destinations can be understood together.
export default function MessageSources({ blocks }) {
  const sources = messageSources(blocks)
  if (sources.length === 0) return null

  return (
    <section className="chat__sources" aria-label="Sources for this answer">
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
