import { useEffect, useRef } from 'react'
import { api, jsonOrThrow } from '../../api/client.js'
import { projectPreviewSandbox } from '../../lib/projectPreview.js'
import { normalizeSharedAppSnapshot } from '../../lib/sharedAppState.js'


function changedPaths(previous, next) {
  return [...new Set([...Object.keys(previous || {}), ...Object.keys(next || {})])]
    .filter(path => JSON.stringify(previous?.[path]) !== JSON.stringify(next?.[path]))
}


export default function SharedAppFrame({ instanceId, srcDoc, initialState, title }) {
  const frameRef = useRef(null)
  const stateRef = useRef(initialState || { cursor: 0, values: {}, versions: {} })

  useEffect(() => {
    stateRef.current = initialState || { cursor: 0, values: {}, versions: {} }
  }, [instanceId, initialState])

  useEffect(() => {
    let active = true
    let syncing = false
    let mutating = false
    let pollTimer = 0
    let pollDelay = 2_500
    let mutationQueue = Promise.resolve()
    const requests = new Map()
    const notify = (path, value) => frameRef.current?.contentWindow?.postMessage({
      type: 'mobius:project-preview-storage-changed', path, value,
    }, '*')
    const adopt = next => {
      const normalized = normalizeSharedAppSnapshot(stateRef.current, next)
      if (!normalized) return false
      const previous = stateRef.current?.values || {}
      const values = normalized.values
      stateRef.current = normalized
      for (const path of changedPaths(previous, values)) {
        notify(path, Object.hasOwn(values, path) ? values[path] : null)
      }
      return true
    }
    const refresh = async () => {
      if (!active || syncing || mutating) return false
      syncing = true
      try {
        adopt(await jsonOrThrow(await api.sharedApps.state(instanceId), 'Shared data failed:'))
        return true
      }
      catch { /* the next explicit interaction owns user-facing feedback */ }
      finally { syncing = false }
      return false
    }
    const handleRequest = async message => {
      const response = {
        type: 'mobius:project-preview-storage-result',
        requestId: message.requestId,
      }
      try {
        const values = stateRef.current?.values || {}
        if (message.method === 'get') {
          response.value = Object.hasOwn(values, message.path) ? values[message.path] : null
        } else if (message.method === 'list') {
          response.value = Object.keys(values).filter(path => path.startsWith(message.path || '')).sort()
        } else if (message.method === 'set' || message.method === 'delete') {
          mutating = true
          try {
            const result = await jsonOrThrow(await api.sharedApps.writeState(instanceId, message.path, {
              expected_version: stateRef.current.versions?.[message.path] ?? null,
              value: message.value,
              delete: message.method === 'delete',
            }), 'Shared data changed:')
            const nextValues = { ...stateRef.current.values }
            const nextVersions = { ...stateRef.current.versions }
            if (message.method === 'delete') {
              delete nextValues[message.path]
              delete nextVersions[message.path]
            } else {
              nextValues[message.path] = message.value
              nextVersions[message.path] = result.version
            }
            adopt({
              cursor: result.change_id,
              values: nextValues,
              versions: nextVersions,
            })
            response.value = message.method === 'delete' ? null : message.value
          } finally {
            mutating = false
          }
        } else {
          throw new Error('Unsupported shared data operation.')
        }
      } catch (cause) {
        await refresh()
        response.error = cause?.message || 'Shared data could not be saved.'
      }
      return response
    }
    const onMessage = event => {
      if (event.source !== frameRef.current?.contentWindow) return
      const message = event.data
      if (!message || message.type !== 'mobius:project-preview-storage') return
      let task = requests.get(message.requestId)
      if (!task) {
        task = mutationQueue.then(() => handleRequest(message))
        mutationQueue = task.catch(() => {})
        requests.set(message.requestId, task)
        if (requests.size > 256) requests.delete(requests.keys().next().value)
      }
      void task.then(response => event.source.postMessage(response, '*'))
    }
    const schedulePoll = delay => {
      window.clearTimeout(pollTimer)
      pollTimer = window.setTimeout(poll, delay)
    }
    const poll = async () => {
      if (!active) return
      if (document.hidden || syncing || mutating) {
        schedulePoll(pollDelay)
        return
      }
      try {
        const result = await jsonOrThrow(
          await api.sharedApps.changes(instanceId, stateRef.current.cursor || 0),
          'Shared changes failed:',
        )
        if (result.truncated || result.changes?.length) {
          await refresh()
        } else {
          stateRef.current = { ...stateRef.current, cursor: Number(result.cursor || 0) }
        }
        pollDelay = 2_500
      } catch {
        pollDelay = Math.min(pollDelay * 2, 10_000)
      }
      schedulePoll(pollDelay)
    }
    const onVisibilityChange = () => {
      if (!document.hidden) schedulePoll(0)
    }
    window.addEventListener('message', onMessage)
    document.addEventListener('visibilitychange', onVisibilityChange)
    schedulePoll(pollDelay)
    return () => {
      active = false
      window.clearTimeout(pollTimer)
      window.removeEventListener('message', onMessage)
      document.removeEventListener('visibilitychange', onVisibilityChange)
    }
  }, [instanceId, srcDoc])

  return <iframe
    ref={frameRef}
    title={title}
    className="shared-app__frame"
    sandbox={projectPreviewSandbox()}
    srcDoc={srcDoc}
    onLoad={() => frameRef.current?.contentWindow?.postMessage({
      type: 'mobius:project-preview-storage-connected',
    }, '*')}
  />
}
