/* Resolve image-view tool results without handing their base64 payload to the generic text renderer. */

const CHAT_IMAGE_PATH = /^\/data\/chats\/([A-Za-z0-9_-]+)\/(uploads|media)\/([^/]+)$/
const GENERATED_IMAGE_PATH = /^\/data\/chats\/([A-Za-z0-9_-]+)\/deliverables\/inbox\/([^/]+)$/
export const VIEWED_SNAPSHOT_NAME = /^viewed-[a-f0-9]{64}\.(?:png|jpg|gif|webp)$/
const SCRATCH_IMAGE_PATH = /^\/data\/agent-scratch\/([^/]+)\/(.+)$/
const INLINE_IMAGE_TYPES = new Set([
  'image/png',
  'image/jpeg',
  'image/gif',
  'image/webp',
  'image/bmp',
  'image/avif',
])
const MAX_INLINE_RESULT_CHARS = 32 * 1024 * 1024

/** Claude's Read activity carries a bare path; Codex dynamic tools carry their
 * arguments as a JSON string. Collapse both into the same path contract. */
export function imagePathFromInput(input) {
  if (input && typeof input === 'object') {
    return input.path || input.file_path || ''
  }
  if (typeof input !== 'string') return ''
  const trimmed = input.trim()
  if (!trimmed.startsWith('{')) return trimmed
  try {
    const parsed = JSON.parse(trimmed)
    return parsed?.path || parsed?.file_path || ''
  } catch {
    return trimmed
  }
}

/** Prefer the original chat file: it is smaller than the tool's base64 result
 * and keeps the browser URL behind the existing short-lived media token. */
export function chatImageReference(input) {
  const match = imagePathFromInput(input).match(CHAT_IMAGE_PATH)
  if (!match) return null
  return {
    kind: 'chat',
    chatId: match[1],
    collection: match[2],
    filename: match[3],
  }
}

/** A view bound to a chat-owned snapshot previews exactly the bytes the
 * provider received, however the viewed path changes afterwards. Unbound views
 * of shared paths such as /tmp have no served preview. */
export function viewedSnapshotReference(chatId, name) {
  if (!chatId || typeof name !== 'string' || !VIEWED_SNAPSHOT_NAME.test(name)) return null
  return { kind: 'chat', chatId, collection: 'media', filename: name }
}

/** A viewed deliverable previews through its final same-turn attachment.
 * Only fingerprinted views can claim a content-matched preview. */
export function generatedInboxImageName(input, chatId) {
  if (!chatId) return null
  const match = imagePathFromInput(input).match(GENERATED_IMAGE_PATH)
  return match?.[1] === chatId ? match[2] : null
}

export function generatedImageReference(input, chatId, {
  files = [], viewedDigest, completed = false,
} = {}) {
  if (!completed) return null
  const name = generatedInboxImageName(input, chatId)
  if (!name) return null
  if (typeof viewedDigest !== 'string' || !/^[a-f0-9]{64}$/.test(viewedDigest)) return null
  const file = files.find(candidate => (
    candidate?.previewable === true
    && INLINE_IMAGE_TYPES.has(candidate.mime_type)
    && (candidate.sha256 === viewedDigest
      || (candidate.sha256 == null && candidate.name === name))
  ))
  if (!file) return null
  return {
    kind: 'generated', chatId, collection: 'generated-files', filename: file.name,
    expectedSha256: viewedDigest,
  }
}

/** Agent scratch is per-chat and expires; this previews its current file only. */
export function scratchImageReference(input, chatId) {
  if (!chatId) return null
  const match = imagePathFromInput(input).match(SCRATCH_IMAGE_PATH)
  if (!match || match[1] !== chatId) return null
  return { kind: 'scratch', chatId, filename: match[2] }
}

/** References that can render through an existing protected route without
 * loading the image tool's much larger base64 sidecar. */
export function servedImageReference(input, chatId, generated = {}) {
  return viewedSnapshotReference(chatId, generated.viewedMedia)
    || chatImageReference(input)
    || scratchImageReference(input, chatId)
    || generatedImageReference(input, chatId, generated)
}

/** Fallback for image tools that viewed a path outside chat media. This work
 * happens only after the owner expands that activity and the full sidecar has
 * loaded; ordinary transcript rendering never parses the image payload. */
export function inlineImageReference(output) {
  if (
    typeof output !== 'string'
    || output.length === 0
    || output.length > MAX_INLINE_RESULT_CHARS
  ) return null

  try {
    const value = JSON.parse(output)
    const source = value?.type === 'image' ? value.source : null
    if (
      source?.type !== 'base64'
      || typeof source.data !== 'string'
      || !INLINE_IMAGE_TYPES.has(source.media_type)
    ) return null
    return {
      kind: 'inline',
      src: `data:${source.media_type};base64,${source.data}`,
    }
  } catch {
    return null
  }
}

export function toolImageReference(input, output, chatId, generated = {}) {
  return servedImageReference(input, chatId, generated) || inlineImageReference(output)
}
