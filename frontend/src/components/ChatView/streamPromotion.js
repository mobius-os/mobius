/*
 * Pure helpers for turning live stream items into durable assistant
 * messages. ChatView owns when promotion happens; this file owns how a
 * stream becomes blocks/content and how an in-flight DB partial is replaced.
 */

import { questionKey } from './questionKey.js'

export function streamItemToBlock(item, { finalize = true } = {}) {
  if (item.type === 'text') return { type: 'text', content: item.content }
  if (item.type === 'thinking') {
    // The active renderer needs the runner-clock anchors for its live timer.
    // Promotion deliberately strips those client-only fields and keeps only
    // the durable duration.
    if (!finalize) return { ...item }
    return {
      type: 'thinking',
      content: item.content,
      ...(Number.isFinite(item.duration_ms)
        ? { duration_ms: item.duration_ms }
        : {}),
    }
  }
  if (item.type === 'question') {
    return {
      type: 'question',
      questions: item.questions,
      ...(item.question_id ? { question_id: item.question_id } : {}),
      ...(item.answers ? { answers: item.answers } : {}),
    }
  }
  if (item.type === 'error') {
    // Carry the whitelisted extras (resumable drives the one-tap Resume; the
    // single `pause` descriptor — {kind, resets_at?} — drives the calm "Paused"
    // family and the live provider-limit "resets at …" card) so a promoted
    // stream error renders identically to its persisted DB twin.
    return {
      type: 'error',
      message: item.message,
      ...(item.resumable ? { resumable: true } : {}),
      ...(item.pause ? { pause: item.pause } : {}),
    }
  }
  const status = finalize && item.status === 'running' ? 'done' : item.status
  return { type: 'tool', ...item, status }
}


// One key namespace for the active answer, regardless of whether its blocks
// came from the DB partial or streamItemsToAssistantPayload. Tools use their
// protocol identity when available; every legacy/tokenless block falls back to
// its ordinal. Keeping this helper source-agnostic is what lets React preserve
// ToolBlock state and markdown/image DOM across the source switch.
export function assistantBlockKey(block, index) {
  if (block?.type === 'tool') return block.tool_use_id ?? `t-${index}`
  return index
}


function normalizeMirrorText(value) {
  return String(value || '').replace(/\s+/g, ' ').trim()
}

export function assistantMessageText(msg) {
  if (!msg) return ''
  if (Array.isArray(msg.blocks) && msg.blocks.length > 0) {
    return msg.blocks
      .filter(b => b?.type === 'text')
      .map(b => b.content || '')
      .filter(Boolean)
      .join('\n\n')
  }
  return msg.content || ''
}

function sameToolBlock(a, b) {
  if (a?.tool !== b?.tool) return false
  const sameStatus = a?.status === b?.status
    || (a?.status === 'done' && b?.status === 'running')
    || (a?.status === 'running' && b?.status === 'done')
  if (!sameStatus) return false
  if ((a?.input || '') !== (b?.input || '')) return false
  if ((a?.output || '') !== (b?.output || '')) return false
  if ((a?.name || '') !== (b?.name || '')) return false
  return true
}

function streamBlocksCoverMessageBlocks(msgBlocks, streamBlocks) {
  if (!Array.isArray(msgBlocks) || msgBlocks.length === 0) return true
  if (!Array.isArray(streamBlocks) || streamBlocks.length < msgBlocks.length) return false
  for (let i = 0; i < msgBlocks.length; i++) {
    const msgBlock = msgBlocks[i]
    const streamBlock = streamBlocks[i]
    if (!msgBlock || !streamBlock || msgBlock.type !== streamBlock.type) return false
    if (msgBlock.type === 'text') {
      const msgText = normalizeMirrorText(msgBlock.content)
      const streamText = normalizeMirrorText(streamBlock.content)
      if (msgText && !streamText.startsWith(msgText)) return false
    } else if (msgBlock.type === 'tool') {
      if (!sameToolBlock(msgBlock, streamBlock)) return false
    } else if (msgBlock.type === 'question') {
      if (questionKey(msgBlock) !== questionKey(streamBlock)) return false
    } else if (msgBlock.type === 'thinking') {
      const msgText = normalizeMirrorText(msgBlock.content)
      const streamText = normalizeMirrorText(streamBlock.content)
      if (msgText && !streamText.startsWith(msgText)) return false
    } else if (msgBlock.type === 'error') {
      if ((msgBlock.message || '') !== (streamBlock.message || '')) return false
    } else {
      return false
    }
  }
  return true
}

function blockWeight(block) {
  if (!block) return 0
  if (block.type === 'text' || block.type === 'thinking') {
    return normalizeMirrorText(block.content).length
  }
  if (block.type === 'tool') {
    return 40
      + normalizeMirrorText(block.tool).length
      + normalizeMirrorText(block.input).length
      + normalizeMirrorText(block.output).length
  }
  if (block.type === 'question') {
    return 40
      + (Array.isArray(block.questions)
        ? block.questions.map(q => normalizeMirrorText(q?.question || q?.text)).join(' ').length
        : 0)
      + (block.answers ? 20 : 0)
  }
  if (block.type === 'error') return 40 + normalizeMirrorText(block.message).length
  return 0
}

function surfaceWeight({ content, blocks }) {
  const blockTotal = Array.isArray(blocks)
    ? blocks.reduce((sum, block) => sum + blockWeight(block), 0)
    : 0
  return normalizeMirrorText(content).length + blockTotal
}

function firstMeaningfulBlock(blocks) {
  if (!Array.isArray(blocks)) return null
  return blocks.find(block => {
    if (!block) return false
    if (block.type === 'text' || block.type === 'thinking') {
      return !!normalizeMirrorText(block.content)
    }
    return true
  }) || null
}

function blocksLookLikeSameTurn(a, b) {
  if (!a || !b || a.type !== b.type) return false
  if (a.type === 'text' || a.type === 'thinking') {
    const aText = normalizeMirrorText(a.content)
    const bText = normalizeMirrorText(b.content)
    return !!(aText && bText && (aText.startsWith(bText) || bText.startsWith(aText)))
  }
  if (a.type === 'tool') return !!(a.tool && a.tool === b.tool)
  if (a.type === 'question') return questionKey(a) === questionKey(b)
  if (a.type === 'error') return (a.message || '') === (b.message || '')
  return false
}

function assistantSurfacesLookRelated(msg, streamPayload) {
  const msgText = normalizeMirrorText(assistantMessageText(msg))
  const streamText = normalizeMirrorText(streamPayload.content)
  if (msgText && streamText) {
    if (msgText.startsWith(streamText) || streamText.startsWith(msgText)) {
      return true
    }
    // Allow a small amount of metadata drift around the same opening prose.
    // This is deliberately a prefix-only heuristic: unrelated turns often
    // share generic words later in the paragraph, but a long shared opener is
    // a strong signal that the DB partial and replay stream are the same turn.
    const n = Math.min(msgText.length, streamText.length, 48)
    if (n >= 24 && msgText.slice(0, n) === streamText.slice(0, n)) {
      return true
    }
  }

  const msgBlocks = Array.isArray(msg?.blocks) ? msg.blocks.filter(Boolean) : []
  const streamBlocks = Array.isArray(streamPayload?.blocks) ? streamPayload.blocks.filter(Boolean) : []
  return blocksLookLikeSameTurn(
    firstMeaningfulBlock(msgBlocks),
    firstMeaningfulBlock(streamBlocks),
  )
}

export function assistantStreamCoversMessage(msg, items) {
  if (!msg || msg.role !== 'assistant') return false
  if (!Array.isArray(items) || items.length === 0) return false
  const streamPayload = streamItemsToAssistantPayload(items)
  const msgText = normalizeMirrorText(assistantMessageText(msg))
  const streamText = normalizeMirrorText(streamPayload.content)
  if (msgText && !streamText.startsWith(msgText)) return false
  const msgBlocks = Array.isArray(msg.blocks) ? msg.blocks.filter(Boolean) : []
  if (!streamBlocksCoverMessageBlocks(msgBlocks, streamPayload.blocks.filter(Boolean))) return false
  return !!streamText || msgBlocks.length > 0
}

export function messageCoversAssistantStream(msg, items) {
  if (!msg || msg.role !== 'assistant') return false
  if (!Array.isArray(items) || items.length === 0) return false
  const streamPayload = streamItemsToAssistantPayload(items)
  const msgText = normalizeMirrorText(assistantMessageText(msg))
  const streamText = normalizeMirrorText(streamPayload.content)
  if (streamText && !msgText.startsWith(streamText)) return false
  const msgBlocks = Array.isArray(msg.blocks) ? msg.blocks.filter(Boolean) : []
  const streamBlocks = streamPayload.blocks.filter(Boolean)
  if (msgBlocks.length < streamBlocks.length) return false
  return streamBlocks.every((block, i) => {
    const msgBlock = msgBlocks[i]
    if (!msgBlock || msgBlock.type !== block.type) return false
    if (block.type === 'text') return normalizeMirrorText(msgBlock.content).startsWith(normalizeMirrorText(block.content))
    if (block.type === 'thinking') return normalizeMirrorText(msgBlock.content).startsWith(normalizeMirrorText(block.content))
    if (block.type === 'tool') return sameToolBlock(msgBlock, block)
    if (block.type === 'question') return questionKey(msgBlock) === questionKey(block)
    if (block.type === 'error') return (msgBlock.message || '') === (block.message || '')
    return false
  })
}

export function chooseActiveAssistantSurface(msg, items) {
  if (!msg || msg.role !== 'assistant' || !Array.isArray(items) || items.length === 0) {
    return { hideMessage: false, suppressStream: false }
  }

  if (assistantStreamCoversMessage(msg, items)) {
    return { hideMessage: true, suppressStream: false }
  }
  if (messageCoversAssistantStream(msg, items)) {
    return { hideMessage: false, suppressStream: true }
  }

  const streamPayload = streamItemsToAssistantPayload(items)
  if (!assistantSurfacesLookRelated(msg, streamPayload)) {
    return { hideMessage: false, suppressStream: false }
  }

  const msgPayload = {
    content: assistantMessageText(msg),
    blocks: Array.isArray(msg.blocks) ? msg.blocks.filter(Boolean) : [],
  }
  if (surfaceWeight(streamPayload) >= surfaceWeight(msgPayload)) {
    return { hideMessage: true, suppressStream: false }
  }
  return { hideMessage: false, suppressStream: true }
}


// Decide whether an existing DB assistant row belongs in the stable active
// shell. A captured bridge does while the live payload is empty or proves it
// is the same answer; an unrelated payload releases a stale cached bridge and
// falls through to the actual trailing partial. A trailing assistant stays in
// the shell while the active turn has no live items, then remains there only
// if surface selection relates it to the returning stream. This closes the
// empty→nonempty reconnect/question gap without swallowing an unrelated
// completed assistant from the previous turn.
export function chooseActiveAssistantMirrorIndex({
  bridgeMsgIdx,
  trailingAssistantPartialIdx,
  hasLivePayload,
  bridgeSurface,
  surface,
}) {
  if (bridgeMsgIdx >= 0) {
    // The mount bridge is captured before the authoritative chat fetch can
    // refresh an in-memory cache. If a new turn starts in that window, the
    // captured row can be the COMPLETED answer from the previous turn. Never
    // suppress it merely because its timestamp was once a bridge candidate:
    // once live payload exists, the payload must prove that the row is the
    // same answer. Otherwise fall through to the actual trailing DB partial
    // (if any) or render the new stream on its own.
    if (!hasLivePayload
        || bridgeSurface?.hideMessage
        || bridgeSurface?.suppressStream) {
      return bridgeMsgIdx
    }
  }
  if (trailingAssistantPartialIdx < 0) return -1
  if (!hasLivePayload) return trailingAssistantPartialIdx
  return surface?.hideMessage || surface?.suppressStream
    ? trailingAssistantPartialIdx
    : -1
}


// The first mounted active row owns the scroll-anchor key for its lifetime.
// DB-first bridge answers seed from the durable row; live-first answers seed a
// synthetic turn key and must not adopt a later DB partial's key, because both
// React reconciliation and ANCHOR_AT resolve through this identity.
export function chooseActiveAssistantDataKey({
  latched,
  mirroredMsg,
  mirrorIndex,
  hasLivePayload,
  chatId,
}) {
  const mirroredKey = mirroredMsg?.role === 'assistant' && !mirroredMsg.hidden
    ? (mirroredMsg.id || `${mirroredMsg.role}-${mirroredMsg.ts ?? mirrorIndex}`)
    : null
  if (latched?.key) {
    // A DB-seeded key is valid only while that row still mirrors the active
    // answer. If surface selection releases it as unrelated, the restored
    // history row owns the durable key and the live answer needs a distinct
    // synthetic anchor. A live-first latch is intentionally retained when it
    // later adopts a related DB mirror.
    if (latched.mirrorKey && latched.mirrorKey !== mirroredKey) {
      return mirroredKey || (hasLivePayload ? `streaming-${chatId}` : latched.key)
    }
    return latched.key
  }
  return mirroredKey || `streaming-${chatId}`
}

export function findTrailingAssistantPartialIndex(messages) {
  if (!Array.isArray(messages)) return -1
  for (let i = messages.length - 1; i >= 0; i--) {
    const msg = messages[i]
    if (!msg || msg.hidden) continue
    return msg.role === 'assistant' ? i : -1
  }
  return -1
}


export function streamItemsToAssistantPayload(items, options) {
  const blocks = items.map(item => streamItemToBlock(item, options))
  const content = items
    .filter(i => i.type === 'text')
    .map(i => i.content)
    .filter(Boolean)
    // Text runs separated by tools/status updates are distinct assistant
    // blocks. Joining with '' collapses progress messages into "done.next";
    // preserve a paragraph break in the legacy content string while keeping
    // structured blocks unchanged.
    .join('\n\n')
  return { content, blocks }
}

// True when the live stream carries something worth sealing into its own
// assistant message. A steer can land before the assistant emitted any real
// output — the only buffered item is an empty or whitespace-only text token.
// Sealing that produces a stray empty assistant bubble sitting before the
// steered user row, which reads as an orphaned fragment. A single real token
// ("I ") IS renderable and stays; only the empty/whitespace case is dropped.
// Any non-text block (tool/question/error) is always renderable.
export function streamItemsHaveRenderableContent(items) {
  if (!Array.isArray(items) || items.length === 0) return false
  return items.some(item => {
    if (item?.type === 'text') return !!String(item.content || '').trim()
    return true
  })
}

export function carryQuestionAnswers(blocks, existingBlocks = []) {
  const existingAnswersByKey = new Map()
  for (const block of existingBlocks) {
    if (block?.type === 'question' && block.answers) {
      existingAnswersByKey.set(questionKey(block), block.answers)
    }
  }
  if (existingAnswersByKey.size === 0) return blocks

  return blocks.map(block => {
    if (block.type !== 'question' || block.answers) return block
    const carried = existingAnswersByKey.get(questionKey(block))
    return carried ? { ...block, answers: carried } : block
  })
}

export function promoteAssistantStream(messages, { items, bridgeTs = null }) {
  if (!Array.isArray(items) || items.length === 0) return messages
  const { content, blocks } = streamItemsToAssistantPayload(items)

  const bridgeIdx = bridgeTs == null
    ? -1
    : messages.findIndex(m => m?.role === 'assistant' && m.ts === bridgeTs)
  const bridgedMsg = bridgeIdx >= 0 ? messages[bridgeIdx] : null

  if (bridgedMsg) {
    const merged = {
      ...bridgedMsg,
      content,
      blocks: carryQuestionAnswers(blocks, bridgedMsg.blocks || []),
    }
    return [
      ...messages.slice(0, bridgeIdx),
      merged,
      ...messages.slice(bridgeIdx + 1),
    ]
  }

  return [...messages, { role: 'assistant', content, blocks }]
}
