/* Exact prepared patch reader for the chat-owned Changes review surface. */

import { useEffect, useState } from 'react'
import { apiFetch } from '../../api/client.js'
import UnifiedDiff from '../DiffView/UnifiedDiff.jsx'

export default function ChatContributionDiff({ appId, record }) {
  const recordId = String(record?.id || '')
  const revision = String(record?.plan?.diff_sha256 || record?.action_key || '')
  const [state, setState] = useState({ phase: 'loading', diff: '' })
  const [retryKey, setRetryKey] = useState(0)

  useEffect(() => {
    if (!appId || !recordId) {
      setState({ phase: 'missing', diff: '' })
      return undefined
    }
    const controller = new AbortController()
    setState({ phase: 'loading', diff: '' })
    apiFetch(
      `/storage/apps/${encodeURIComponent(appId)}/contributions/${encodeURIComponent(recordId)}.diff`,
      { signal: controller.signal, headers: { Accept: 'application/octet-stream' } },
    ).then(async response => {
      if (response.status === 404) return { phase: 'missing', diff: '' }
      if (!response.ok) return { phase: 'error', diff: '' }
      const diff = await response.text()
      return diff.trim()
        ? { phase: 'ready', diff }
        : { phase: 'missing', diff: '' }
    }).then(next => {
      if (!controller.signal.aborted) setState(next)
    }).catch(() => {
      if (!controller.signal.aborted) setState({ phase: 'error', diff: '' })
    })
    return () => controller.abort()
  }, [appId, recordId, revision, retryKey])

  if (state.phase === 'loading') {
    return <div className="chat-work__diff-state" role="status">Loading the reviewed diff…</div>
  }
  if (state.phase === 'missing') {
    return <div className="chat-work__diff-state">The exact patch is no longer stored here. Its review record is still available.</div>
  }
  if (state.phase === 'error') {
    return (
      <div className="chat-work__diff-state is-error" role="status">
        <span>The reviewed diff could not be loaded.</span>
        <button type="button" onClick={() => setRetryKey(value => value + 1)}>Try again</button>
      </div>
    )
  }
  return <UnifiedDiff diff={state.diff} initiallyOpenFirst />
}
