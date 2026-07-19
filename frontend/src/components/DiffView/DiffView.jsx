// CANONICAL DIFF VIEWER: copy this entire folder verbatim. It imports only
// React and its own flat sibling modules. Styles ship as a JavaScript string
// because the mini-app compiler rejects CSS side-output.

import { ensureDiffViewerStyles } from './styles.js'

ensureDiffViewerStyles()

function lineSign(type) {
  if (type === 'add') return '+'
  if (type === 'del') return '-'
  if (type === 'meta') return '\\'
  return ' '
}

function LineNumber({ value }) {
  return (
    <span className="diff-view__line-number">
      {typeof value === 'number' ? value : ''}
    </span>
  )
}

export default function DiffView({ file }) {
  if (!file) return null
  if (file.binary) {
    return (
      <div className="diff-view diff-view--message">
        Binary file — no preview
      </div>
    )
  }

  const hunks = Array.isArray(file.hunks) ? file.hunks : []
  if (hunks.length === 0) {
    return (
      <div className="diff-view diff-view--message">
        No textual changes to preview.
      </div>
    )
  }

  return (
    <div
      className="diff-view"
      role="region"
      aria-label={file.path ? `Diff for ${file.path}` : 'File diff'}
      tabIndex={0}
    >
      <div className="diff-view__content">
        {hunks.map((hunk, hunkIndex) => (
          <div className="diff-view__hunk" key={`${hunk.header}-${hunkIndex}`}>
            <div className="diff-view__hunk-header">
              <span className="diff-view__hunk-gutter" aria-hidden="true" />
              <code>{hunk.header}</code>
            </div>
            {/* Guarded like file.hunks above. The canonical parser always emits
                lines, so the platform suite can never catch a producer that does
                not — and in a mini-app frame an exception here is a frame-error
                and a blank app, not one degraded row. */}
            {(Array.isArray(hunk.lines) ? hunk.lines : []).map((line, lineIndex) => (
              <div
                className={`diff-view__line diff-view__line--${line.type}`}
                key={`${hunkIndex}-${lineIndex}`}
              >
                <span className="diff-view__numbers" aria-hidden="true">
                  <LineNumber value={line.oldNo} />
                  <LineNumber value={line.newNo} />
                </span>
                <span className="diff-view__sign" aria-hidden="true">
                  {lineSign(line.type)}
                </span>
                <code className="diff-view__line-text">{line.text}</code>
              </div>
            ))}
          </div>
        ))}
      </div>
    </div>
  )
}
