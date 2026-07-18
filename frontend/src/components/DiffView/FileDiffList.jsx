// CANONICAL DIFF VIEWER: copy this entire folder verbatim. It imports only
// React and its own flat sibling modules. Styles ship as a JavaScript string
// because the mini-app compiler rejects CSS side-output.

import { useId, useState } from 'react'
import DiffView from './DiffView.jsx'
import { decodeGitPath } from './parseUnifiedDiff.js'
import { ensureDiffViewerStyles } from './styles.js'

ensureDiffViewerStyles()

// Eight files still scan as one compact list. Above that, showing six gives a
// useful sample while keeping the explicit “Show all” control in the viewport.
const EXPAND_THRESHOLD = 8
const COLLAPSED_COUNT = 6

const KIND_LABELS = {
  A: 'new',
  C: 'copied',
  D: 'deleted',
  R: 'renamed',
  T: 'type changed',
}

export function splitPath(path) {
  const value = String(path || '')
  const slash = value.lastIndexOf('/')
  if (slash < 0) return { dir: '', base: value }
  return { dir: value.slice(0, slash), base: value.slice(slash + 1) }
}

// RTL left-truncation reorders neutrals at BOTH ends. The LRM anchors LEADING
// punctuation (.github, /etc) so it stays in path order; the trailing separator
// is handled structurally below. Verified by render — dropping either one moves
// the slash to the wrong side.
export function bidiSafeDirectory(dir) {
  return dir ? `\u200E${dir}` : ''
}

function FilePath({ path }) {
  const { dir, base } = splitPath(path)
  return (
    <span className="file-diff-list__path" title={path}>
      {dir ? (
        <span className="file-diff-list__dir">{bidiSafeDirectory(dir)}</span>
      ) : null}
      {/* The slash MUST stay outside the RTL truncation span: inside it, the
          trailing neutral is reordered to the visual start ("/srcApp.jsx"). */}
      {dir ? <span className="file-diff-list__separator">/</span> : null}
      <span className="file-diff-list__basename">{base}</span>
    </span>
  )
}

function FileStat({ insertions, deletions }) {
  if (typeof insertions !== 'number' && typeof deletions !== 'number') return null
  return (
    <span
      className="file-diff-list__stat"
      role="img"
      aria-label={`${insertions || 0} additions, ${deletions || 0} deletions`}
    >
      <span className="file-diff-list__add">+{insertions || 0}</span>
      {/* U+2212 is a real minus sign; a hyphen is shorter and misreads in stats. */}
      <span className="file-diff-list__delete">−{deletions || 0}</span>
    </span>
  )
}

function takeQueuedIndex(queues, path, used) {
  const queue = queues.get(path)
  while (queue?.length) {
    const index = queue.shift()
    if (!used.has(index)) return index
  }
  return undefined
}

/** Build ordered rows, preserving summary counts and exact parsed-path bodies. */
export function buildFileRows(files, summaryOverrides) {
  const parsedFiles = Array.isArray(files) ? files : []
  if (!Array.isArray(summaryOverrides) || summaryOverrides.length === 0) {
    return parsedFiles.map((file, index) => ({
      key: `${index}:${file?.path || ''}`,
      path: file?.path || file?.newPath || file?.oldPath || 'Unknown file',
      status: file?.status || 'M',
      insertions: file?.insertions,
      deletions: file?.deletions,
      file,
    }))
  }

  const summaries = summaryOverrides.map((summary) => ({
    summary,
    path: decodeGitPath(summary?.path) || 'Unknown file',
  }))
  const exactQueues = new Map()
  parsedFiles.forEach((file, index) => {
    if (!file?.path) return
    const queue = exactQueues.get(file.path) || []
    queue.push(index)
    exactQueues.set(file.path, queue)
  })

  const matchedIndexes = Array(summaries.length)
  const used = new Set()

  // Reserve every canonical file.path match before aliases are considered. An
  // oldPath collision can therefore never steal another row's exact diff body.
  summaries.forEach(({ path }, index) => {
    const parsedIndex = takeQueuedIndex(exactQueues, path, used)
    if (parsedIndex === undefined) return
    matchedIndexes[index] = parsedIndex
    used.add(parsedIndex)
  })

  // Rename/copy aliases are only a recovery path for a genuine canonical miss.
  summaries.forEach(({ path }, summaryIndex) => {
    if (matchedIndexes[summaryIndex] !== undefined) return
    const parsedIndex = parsedFiles.findIndex((file, index) => (
      !used.has(index) && (file?.newPath === path || file?.oldPath === path)
    ))
    if (parsedIndex < 0) return
    matchedIndexes[summaryIndex] = parsedIndex
    used.add(parsedIndex)
  })

  return summaries.map(({ summary, path }, index) => {
    const parsedIndex = matchedIndexes[index]
    const file = parsedIndex === undefined ? undefined : parsedFiles[parsedIndex]
    return {
      key: `${index}:${path}`,
      path,
      status: summary?.status || file?.status || 'M',
      insertions: typeof summary?.insertions === 'number'
        ? summary.insertions
        : file?.insertions,
      deletions: typeof summary?.deletions === 'number'
        ? summary.deletions
        : file?.deletions,
      file,
    }
  })
}

export function collapseFileRows(rows, showAll = false) {
  const allRows = Array.isArray(rows) ? rows : []
  const collapsed = allRows.length > EXPAND_THRESHOLD && !showAll
  return {
    collapsed,
    visibleRows: collapsed ? allRows.slice(0, COLLAPSED_COUNT) : allRows,
  }
}

export function filePreviewState(row, truncated, truncatedTail) {
  const hasTextDiff = (row?.file?.hunks?.length || 0) > 0
  if (
    truncated
    && (!row?.file || (truncatedTail && !row.file.binary && !hasTextDiff))
  ) return 'truncated'
  if (!row?.file) return 'unavailable'
  if (row.file.binary || hasTextDiff) return 'diff'
  return 'empty'
}

function FileRow({ row, open, panelId, truncated, truncatedTail, onToggle }) {
  const kind = row.file?.binary
    ? 'bin'
    : KIND_LABELS[String(row.status || '').charAt(0).toUpperCase()]
  const previewState = filePreviewState(row, truncated, truncatedTail)

  return (
    <li className="file-diff-list__item">
      <button
        type="button"
        className="file-diff-list__row"
        aria-expanded={open}
        aria-controls={open ? panelId : undefined}
        onClick={onToggle}
      >
        <span className="file-diff-list__caret" aria-hidden="true" />
        <FilePath path={row.path} />
        <span className="file-diff-list__meta">
          {kind ? <span className="file-diff-list__kind">{kind}</span> : null}
          {row.file?.binary ? null : (
            <FileStat insertions={row.insertions} deletions={row.deletions} />
          )}
        </span>
      </button>
      {open ? (
        <div className="file-diff-list__panel" id={panelId}>
          {previewState === 'truncated' ? (
            <p className="file-diff-list__message">
              Diff not shown — this update is large; the full change still applies.
            </p>
          ) : previewState === 'unavailable' ? (
            <p className="file-diff-list__message">
              Diff unavailable — the file summary is still available.
            </p>
          ) : (
            <>
              <DiffView file={row.file} />
              {truncatedTail ? (
                <p className="file-diff-list__note">
                  Diff truncated here — the full change still applies.
                </p>
              ) : null}
            </>
          )}
        </div>
      ) : null}
    </li>
  )
}

/**
 * Render parsed files with optional ordered per-path summary overrides.
 * Overrides supply authoritative counts and can represent files omitted from a
 * truncated diff; apps with only diff text can pass `files` alone.
 */
export default function FileDiffList({
  files,
  summaryOverrides,
  diffTruncated = false,
}) {
  const parsedFiles = Array.isArray(files) ? files : []
  const rows = buildFileRows(parsedFiles, summaryOverrides)
  const [openFiles, setOpenFiles] = useState(() => new Set())
  const [showAll, setShowAll] = useState(false)
  const listId = useId()
  const lastParsedFile = parsedFiles.at(-1)
  const { collapsed, visibleRows } = collapseFileRows(rows, showAll)

  function toggleFile(key) {
    setOpenFiles((current) => {
      const next = new Set(current)
      if (next.has(key)) next.delete(key)
      else next.add(key)
      return next
    })
  }

  if (rows.length === 0) return null

  return (
    <div className="file-diff-list">
      <ul className="file-diff-list__files">
        {visibleRows.map((row, index) => {
          const panelId = `${listId}-file-diff-${index}`
          return (
            <FileRow
              key={row.key}
              row={row}
              open={openFiles.has(row.key)}
              panelId={panelId}
              truncated={diffTruncated}
              truncatedTail={diffTruncated && row.file === lastParsedFile}
              onToggle={() => toggleFile(row.key)}
            />
          )
        })}
      </ul>
      {collapsed ? (
        <button
          type="button"
          className="file-diff-list__more"
          onClick={() => setShowAll(true)}
        >
          Show all {rows.length} files
        </button>
      ) : null}
      {rows.length > EXPAND_THRESHOLD && showAll ? (
        <button
          type="button"
          className="file-diff-list__more"
          onClick={() => setShowAll(false)}
        >
          Show fewer
        </button>
      ) : null}
    </div>
  )
}
