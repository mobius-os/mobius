/* Expand a saved file change through the canonical diff viewer without changing the editor. */
import { useMemo, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { jsonOrThrow } from '../../api/client.js'
import DiffView from '../DiffView/DiffView.jsx'
import { parseUnifiedDiff } from '../DiffView/parseUnifiedDiff.js'

export function ChangeLineCounts({ additions, deletions, truncated = false }) {
  if (!Number.isFinite(additions) || !Number.isFinite(deletions)) return null
  return <span className="project-finder__diff-total" aria-label={`${additions} additions and ${deletions} deletions${truncated ? ', partial counts' : ''}`}><b>+{additions}</b><i>−{deletions}</i>{truncated && <small title="Partial counts">…</small>}</span>
}

export default function ProjectChangeDiff({ source, change, onOpenFile }) {
  const [open, setOpen] = useState(false)
  const query = useQuery({
    queryKey: source.gitDiffKey(change.path),
    queryFn: async ({ signal }) => jsonOrThrow(await source.gitDiff(change.path, { signal }), 'Diff failed:'),
    enabled: open,
    staleTime: 5_000,
  })
  const file = useMemo(() => parseUnifiedDiff(query.data?.patch || '')[0], [query.data?.patch])
  return <details className="project-change-diff" onToggle={event => setOpen(event.currentTarget.open)}>
    <summary><span title={change.path}>{change.path}</span><small>{change.status === 'untracked' ? 'new' : change.status}</small>{change.binary ? <small>Binary</small> : <ChangeLineCounts {...change} />}</summary>
    {open && <>
      {query.isLoading ? <p role="status">Loading diff…</p> : query.isError ? <p role="alert"><button type="button" onClick={() => query.refetch()}>Retry diff</button></p> : query.data?.binary ? <p>Binary file changed.</p> : file ? <DiffView file={file} /> : <p>No text diff available.</p>}
      {query.data?.truncated && <p>Diff truncated for this large file.</p>}
      {change.status !== 'deleted' && <button type="button" className="project-overview__action" onClick={() => onOpenFile(change.path)}>Open file</button>}
    </>}
  </details>
}
