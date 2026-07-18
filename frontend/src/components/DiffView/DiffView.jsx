// Shared presentational diff for platform-update and app-update review. Its API
// is intentionally just one parsed file entry, with no update-specific state.

import './DiffView.css'

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
  if (hunks.length === 0) return null

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
            {hunk.lines.map((line, lineIndex) => (
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
