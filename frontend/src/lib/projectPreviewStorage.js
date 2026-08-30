// Browser-private storage for an isolated Project preview frame.

export const PROJECT_PREVIEW_STORAGE_EVENT = 'mobius:project-preview-storage-local'
export const PROJECT_PREVIEW_STORAGE_LIMIT = 512 * 1024

function cleanPart(value) {
  return encodeURIComponent(String(value ?? '').slice(0, 240))
}

export function projectPreviewStorageKey(projectId, sourcePath) {
  return `mobius:project-preview:v1:${cleanPart(projectId)}:${cleanPart(sourcePath || 'index.html')}`
}

export function validProjectPreviewPath(path, { allowEmpty = false } = {}) {
  if (allowEmpty && path === '') return true
  if (typeof path !== 'string' || path.length < 1 || path.length > 200) return false
  if (path.startsWith('/') || path.includes('\\') || path.split('/').includes('..')) return false
  return /^[\p{L}\p{N}._/@+ -]+$/u.test(path)
}

export function readProjectPreviewStore(storage, key) {
  try {
    const value = JSON.parse(storage.getItem(key) || '{}')
    return value && typeof value === 'object' && !Array.isArray(value) ? value : {}
  } catch { return {} }
}

export function applyProjectPreviewStorageRequest(storage, key, request) {
  const method = String(request?.method || '')
  const path = request?.path == null ? '' : String(request.path)
  if (method === 'list') {
    if (!validProjectPreviewPath(path, { allowEmpty: true })) throw new Error('Invalid preview storage prefix.')
    return Object.keys(readProjectPreviewStore(storage, key)).filter(item => item.startsWith(path)).sort()
  }
  if (!validProjectPreviewPath(path)) throw new Error('Invalid preview storage path.')
  const current = readProjectPreviewStore(storage, key)
  if (method === 'get') return Object.hasOwn(current, path) ? current[path] : null
  if (method === 'delete') {
    delete current[path]
    storage.setItem(key, JSON.stringify(current))
    return null
  }
  if (method === 'set') {
    const next = { ...current, [path]: request.value }
    const encoded = JSON.stringify(next)
    if (encoded.length > PROJECT_PREVIEW_STORAGE_LIMIT) {
      throw new Error('Personal preview data is full. Remove test data and try again.')
    }
    storage.setItem(key, encoded)
    return request.value
  }
  throw new Error('Unsupported preview storage operation.')
}
