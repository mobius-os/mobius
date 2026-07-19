import { messageSources, sourceHost, sourceLabel } from './messageSources.js'

// The web sources that informed an answer, surfaced ONCE at the end of the
// message — see messageSources.js for where the data comes from and why it is
// derived rather than carried as its own content block.
//
// Message level rather than inside the tool row, because a source is a
// property of the ANSWER, not of the individual search that happened to find
// it: collapsed tool rows hid them, and one search's results are rarely the
// whole citation set.

export default function MessageSources({ blocks }) {
  const sources = messageSources(blocks)
  if (sources.length === 0) return null

  return (
    <div className="chat__sources">
      <span className="chat__sources-label">Sources</span>
      <div className="chat__sources-chips">
        {sources.map(source => {
          const label = sourceLabel(source)
          const host = sourceHost(source.url)
          return (
            <a
              key={source.url}
              className="chat__tool-source-chip"
              href={source.url}
              target="_blank"
              rel="noopener noreferrer"
              title={source.snippet || source.title || source.url}
            >
              <span className="chat__tool-source-title">{label}</span>
              {/* A title-less source already reads as its host, so repeating
                  the host beside it would just print the same word twice. */}
              {host && host !== label && (
                <span className="chat__tool-source-host">{host}</span>
              )}
            </a>
          )
        })}
      </div>
    </div>
  )
}
