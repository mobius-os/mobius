/*
 * Pure helpers for turning live stream items into durable assistant
 * messages. ChatView owns when promotion happens; this file owns how a
 * stream becomes blocks/content and how an in-flight DB partial is replaced.
 */

import { questionKey } from './questionKey.js'

export function streamItemToBlock(item) {
  if (item.type === 'text') return { type: 'text', content: item.content }
  if (item.type === 'thinking') {
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
  if (item.type === 'error') return { type: 'error', message: item.message }
  const status = item.status === 'running' ? 'done' : item.status
  return { type: 'tool', ...item, status }
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

export function findTrailingAssistantPartialIndex(messages) {
  if (!Array.isArray(messages)) return -1
  for (let i = messages.length - 1; i >= 0; i--) {
    const msg = messages[i]
    if (!msg || msg.hidden) continue
    return msg.role === 'assistant' ? i : -1
  }
  return -1
}


export function streamItemsToAssistantPayload(items) {
  const blocks = items.map(streamItemToBlock)
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
