/* ProjectPreviewFrame gives opaque project HTML a browser-private test-data namespace. */
import { useEffect, useMemo, useRef } from 'react'
import { projectPreviewSandbox } from '../../lib/projectPreview.js'
import {
  applyProjectPreviewStorageRequest,
  PROJECT_PREVIEW_STORAGE_EVENT,
  projectPreviewStorageKey,
  readProjectPreviewStore,
} from '../../lib/projectPreviewStorage.js'

export default function ProjectPreviewFrame({ projectId, sourcePath, title, className, srcDoc }) {
  const frameRef = useRef(null)
  const storageKey = useMemo(
    () => projectPreviewStorageKey(projectId, sourcePath),
    [projectId, sourcePath],
  )

  useEffect(() => {
    const notifyFrame = (path, value) => frameRef.current?.contentWindow?.postMessage({
      type: 'mobius:project-preview-storage-changed', path, value,
    }, '*')
    const onMessage = event => {
      if (event.source !== frameRef.current?.contentWindow) return
      const message = event.data
      if (!message || message.type !== 'mobius:project-preview-storage') return
      const response = {
        type: 'mobius:project-preview-storage-result',
        requestId: message.requestId,
      }
      try {
        response.value = applyProjectPreviewStorageRequest(localStorage, storageKey, message)
        if (message.method === 'set' || message.method === 'delete') {
          const value = message.method === 'delete' ? null : message.value
          window.dispatchEvent(new CustomEvent(PROJECT_PREVIEW_STORAGE_EVENT, {
            detail: { storageKey, path: message.path, value },
          }))
        }
      } catch (cause) {
        response.error = cause?.message || 'Personal preview data could not be saved.'
      }
      event.source.postMessage(response, '*')
    }
    const onLocalChange = event => {
      if (event.detail?.storageKey === storageKey) {
        notifyFrame(event.detail.path, event.detail.value)
      }
    }
    const onStorage = event => {
      if (event.key !== storageKey) return
      let previous = {}
      try {
        previous = event.oldValue ? JSON.parse(event.oldValue) : {}
      } catch {
        // A malformed old browser value should not break the live preview bridge.
      }
      const next = readProjectPreviewStore(localStorage, storageKey)
      for (const path of new Set([...Object.keys(previous), ...Object.keys(next)])) {
        if (JSON.stringify(previous[path]) !== JSON.stringify(next[path])) {
          notifyFrame(path, Object.hasOwn(next, path) ? next[path] : null)
        }
      }
    }
    window.addEventListener('message', onMessage)
    window.addEventListener(PROJECT_PREVIEW_STORAGE_EVENT, onLocalChange)
    window.addEventListener('storage', onStorage)
    return () => {
      window.removeEventListener('message', onMessage)
      window.removeEventListener(PROJECT_PREVIEW_STORAGE_EVENT, onLocalChange)
      window.removeEventListener('storage', onStorage)
    }
  }, [storageKey])

  return <iframe
    ref={frameRef}
    title={title}
    className={className}
    sandbox={projectPreviewSandbox()}
    srcDoc={srcDoc}
    onLoad={() => frameRef.current?.contentWindow?.postMessage({
      type: 'mobius:project-preview-storage-connected',
    }, '*')}
  />
}
