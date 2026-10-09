/* Resolve image-view tool results without handing their base64 payload to the generic text renderer. */

const CHAT_IMAGE_PATH = /^\/data\/chats\/([A-Za-z0-9_-]+)\/(uploads|media)\/([^/]+)$/
const GENERATED_IMAGE_PATH = /^\/data\/chats\/([A-Za-z0-9_-]+)\/deliverables\/inbox\/([^/]+)$/
const TMP_IMAGE_PATH = /^\/tmp\/(.+)$/
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

/** A native Codex image-view event records only its path. Temporary raster
 * images therefore use the owning chat's narrow, token-protected /tmp route
 * instead of waiting for a base64 result that Codex never emits. */
export function temporaryImageReference(input, chatId) {
  if (!chatId) return null
  const match = imagePathFromInput(input).match(TMP_IMAGE_PATH)
  if (!match) return null
  return {
    kind: 'tmp',
    chatId,
    filename: match[1],
  }
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

const SAVED_IMAGE_NAME = /^[A-Za-z0-9][A-Za-z0-9._-]*$/

/** A screenshot step's input is a route or app id, never a path. The server
 * records the picture it saved in this chat's media as the step's
 * `saved_image` (backend/app/screenshot_steps.py), so the step renders from
 * chat media instead of downloading its much larger stored result. */
export function savedStepImageReference(savedImage, chatId) {
  if (!chatId || typeof savedImage !== 'string' || !SAVED_IMAGE_NAME.test(savedImage)) {
    return null
  }
  return { kind: 'chat', chatId, collection: 'media', filename: savedImage }
}

/** References that can render through an existing protected route without
 * loading the image tool's much larger base64 sidecar. `step` carries what
 * the transcript records about this step's picture: a screenshot's
 * `savedImage`, and a viewed deliverable's `files`, `viewedDigest`, and
 * `completed` (see generatedImageReference). */
export function servedImageReference(input, chatId, step = {}) {
  return savedStepImageReference(step.savedImage, chatId)
    || chatImageReference(input)
    || temporaryImageReference(input, chatId)
    || scratchImageReference(input, chatId)
    || generatedImageReference(input, chatId, step)
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

const IMAGE_REFERENCE_FIELDS = ['kind', 'chatId', 'collection', 'filename', 'expectedSha256', 'src']

/** Two references name the same picture when every identifying field matches. */
export function sameImageReference(a, b) {
  if (a === b) return true
  if (!a || !b) return false
  return IMAGE_REFERENCE_FIELDS.every(field => a[field] === b[field])
}

export function toolImageReference(input, output, chatId, step = {}) {
  return servedImageReference(input, chatId, step)
    || inlineImageReference(output)
}
