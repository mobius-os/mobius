/* Saved changes open in the file pane; one renderer owns unified diff states. */
import { useMemo } from 'react'
import DiffView from '../DiffView/DiffView.jsx'
import { parseUnifiedDiff } from '../DiffView/parseUnifiedDiff.js'

export function ChangeLineCounts({ additions, deletions, truncated = false }) {
  if (!Number.isFinite(additions) || !Number.isFinite(deletions)) return null
  return <span className="project-finder__diff-total" aria-label={`${additions} additions and ${deletions} deletions${truncated ? ', partial counts' : ''}`}><b>+{additions}</b><i>−{deletions}</i>{truncated && <small title="Partial counts">…</small>}</span>
}

export function ProjectFileDiff({ query, changed, dirty }) {
  const file = useMemo(() => parseUnifiedDiff(query.data?.patch || '')[0], [query.data?.patch])
  return <div className="project-file-diff">
    {dirty && <p role="status">Showing saved changes. Your unsaved draft is preserved in Code.</p>}
    {!changed ? <p>No saved changes against HEAD.</p> : query.isPending ? <p role="status">Loading diff…</p> : query.isError ? <div role="alert"><p>Could not load this diff.</p><button type="button" onClick={() => query.refetch()}>Retry diff</button></div> : query.data?.binary ? <p>Binary file changed. Open Preview to inspect the saved file.</p> : file ? <DiffView file={file} /> : <p>No text diff available.</p>}
    {query.data?.truncated && <p>Diff truncated for this large file.</p>}
  </div>
}

export default function ProjectChangeDiff({ change, onOpenFile }) {
  return <button type="button" className="project-change-diff" aria-label={`View changes in ${change.path}`} onClick={() => onOpenFile(change.path)}>
    <span title={change.path}>{change.path}</span><small>{change.status === 'untracked' ? 'new' : change.status}</small>{change.binary ? <small>Binary</small> : <ChangeLineCounts {...change} />}
  </button>
}
