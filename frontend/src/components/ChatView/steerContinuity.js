/* Project exact post-steer text replay as one continuous assistant answer. */

import { safeSteerMarkdownCut } from './markdown/steerContinuation.js'
import { splitSteerMarkdown, sliceMarkdownRange } from './markdown/steerMarkdownRange.js'


export function isSteeredUserMessage(message) {
  return !!(
    message
    && message.role === 'user'
    && message.steered === true
  )
}

export function assistantReplyRoot(message) {
  return message?.role === 'assistant' && typeof message.id === 'string'
    ? message.id.replace(/:assistant:[1-9][0-9]*$/, '') : null
}

export function isHiddenReplyCarrier(message, root) {
  return !!(root && isSteeredUserMessage(message) && message.hidden
    // Helper delivery names its logical Goal root, not necessarily this
    // physical attempt. The caller still requires equal explicit reply roots
    // on both sides; foreign peer/generic carriers remain boundaries.
    && (!message.source_work_id || message.source_work_id === root
      || message.kind === 'delegation_result'))
}


/** Return the sealed assistant immediately before one or more steered rows. */
export function sealedAssistantBeforeSteer(messages, continuationIndex) {
  if (!Array.isArray(messages) || !Number.isInteger(continuationIndex)) return null
  let index = continuationIndex - 1
  let sawSteer = false
  while (index >= 0 && isSteeredUserMessage(messages[index])) {
    sawSteer = true
    index -= 1
  }
  if (!sawSteer || messages[index]?.role !== 'assistant') return null
  return messages[index]
}


function terminalTextBlockContent(message) {
  if (!message) return ''
  if (!Array.isArray(message.blocks) || message.blocks.length === 0) {
    return typeof message.content === 'string' ? message.content : ''
  }
  // A repeated steer can replay the current text section, not the whole reply.
  // Never join sections or reach back past a tool/question boundary. Thinking
  // is neutral activity, just as it is before the continuation's first text.
  for (let index = message.blocks.length - 1; index >= 0; index -= 1) {
    const block = message.blocks[index]
    if (block?.type === 'thinking') continue
    return block?.type === 'text' && typeof block.content === 'string'
      ? block.content
      : ''
  }
  return ''
}


function firstContinuationTextBlock(blocks) {
  if (!Array.isArray(blocks)) return -1
  for (let index = 0; index < blocks.length; index += 1) {
    const block = blocks[index]
    if (block?.type === 'text') return index
    // Thinking before the answer is provider-neutral activity. Any other
    // visible block means prose did not actually begin the continuation.
    if (block?.type !== 'thinking') return -1
  }
  return -1
}


function projectedText(prefix, text, { active }) {
  if (!prefix || !text) return null
  if (text.startsWith(prefix)) {
    if (safeSteerMarkdownCut(text, prefix.length)) return { content: text.slice(prefix.length) }
    const ranges = splitSteerMarkdown(text, prefix.length)
    return ranges ? { content: text.slice(prefix.length), ranges } : null
  }
  // While the provider is replaying the already-visible prefix, hold that
  // provisional duplicate off-screen. If one character diverges, this branch
  // stops matching and the complete accumulated text appears unchanged.
  if (active && prefix.startsWith(text)) return { content: '' }
  return null
}


/**
 * Return a presentation-only assistant message. Stored content is never
 * rewritten: a mismatch, a settled short response, or an unmappable Markdown cut
 * returns the original object by identity.
 */
export function projectSteerContinuationMessage(
  sealedMessage,
  continuationMessage,
  { active = false } = {},
) {
  if (!sealedMessage || continuationMessage?.role !== 'assistant') {
    return continuationMessage
  }
  if (sealedMessage.id && continuationMessage.id
      && assistantReplyRoot(sealedMessage)
        !== assistantReplyRoot(continuationMessage)) return continuationMessage
  const prefix = terminalTextBlockContent(sealedMessage)
  if (!prefix) return continuationMessage

  const blocks = continuationMessage.blocks
  if (Array.isArray(blocks) && blocks.length > 0) {
    const textIndex = firstContinuationTextBlock(blocks)
    if (textIndex < 0) return continuationMessage
    const text = String(blocks[textIndex]?.content || '')
    const projected = projectedText(prefix, text, { active })
    if (projected == null) return continuationMessage
    const nextBlocks = blocks.slice()
    nextBlocks[textIndex] = {
      ...blocks[textIndex], content: projected.content,
      ...(projected.ranges ? { markdown_range: projected.ranges.after } : {}),
      // Activity offsets refer to persisted text, before this display-only cut.
      source_text_offset: (blocks[textIndex].source_text_offset || 0) + text.length - projected.content.length,
    }
    return {
      ...continuationMessage,
      content: projected.content,
      blocks: nextBlocks,
      steer_replay: { textIndex, prefix, text, sourceOffset: blocks[textIndex].source_text_offset || 0,
        ...(projected.ranges ? { prefixRange: projected.ranges.before } : {}) },
    }
  }

  const text = String(continuationMessage.content || '')
  const projected = projectedText(prefix, text, { active })
  if (projected == null) return continuationMessage
  return { ...continuationMessage, content: projected.content,
    ...(projected.ranges ? { markdown_range: projected.ranges.after } : {}),
    steer_replay: { textIndex: 0, prefix, text, sourceOffset: 0,
      ...(projected.ranges ? { prefixRange: projected.ranges.before } : {}) } }
}

/** Give the sealed prefix the same parsed formatting as its exact continuation.
 * Its text and position stay frozen; only the display context crosses the steer. */
export function projectSteerPrefixMessage(sealed, continuation) {
  const range = continuation?.steer_replay?.prefixRange
  if (!range || !sealed) return sealed
  const index = sealed.blocks?.length
    ? sealed.blocks.findLastIndex(block => block.type !== 'thinking') : 0
  const block = sealed.blocks?.length ? sealed.blocks[index] : { type: 'text', content: sealed.content }
  if (block?.type !== 'text') return sealed
  const replay = sealed.steer_replay?.textIndex === index ? sealed.steer_replay : null
  const start = replay ? replay.text.length - block.content.length : 0
  const markdownRange = sliceMarkdownRange(range, start, start + block.content.length)
  if (!markdownRange) return sealed
  // Carry the final parse backward only through this same exact text section.
  // A later tool/text section must not lend its formatting to an earlier one.
  const prefixRange = replay && range.source.startsWith(replay.text)
    ? sliceMarkdownRange(range, 0, replay.prefix.length) : null
  const context = prefixRange ? { steer_replay: { ...replay, prefixRange } } : {}
  if (!sealed.blocks?.length) return { ...sealed, ...context, markdown_range: markdownRange }
  const blocks = sealed.blocks.slice()
  blocks[index] = { ...block, markdown_range: markdownRange }
  return { ...sealed, ...context, blocks }
}

/** Update only the active replay chain; unrelated settled rows stay cached. */
export function projectActiveSteerPrefix(messages, activePrefix) {
  if (!activePrefix?.continuation?.steer_replay?.prefixRange) return messages
  let index = messages.findIndex(message => message?.id === activePrefix.id)
  if (index < 0) return messages
  const presented = messages.slice()
  let continuation = activePrefix.continuation
  while (index >= 0) {
    const message = projectSteerPrefixMessage(presented[index], continuation)
    if (message === presented[index]) break
    presented[index] = message
    if (!message.steer_replay?.prefixRange) break
    continuation = message
    index -= 1
    while (isSteeredUserMessage(messages[index])) index -= 1
  }
  return presented
}


/** Apply the exact replay projection to settled transcript rows. */
export function projectSettledSteerContinuations(messages, { preserveHidden = false } = {}) {
  if (!Array.isArray(messages)) return []
  const presented = messages.map((message, index) => {
    if (message?.role !== 'assistant') return message
    const root = assistantReplyRoot(message)
    let before = index - 1
    while (isHiddenReplyCarrier(messages[before], root)) before -= 1
    // Only defer to a proven reply group. Id-less rolling history retains its
    // existing safe suppression; a hidden carrier alone does not create one.
    if (preserveHidden && before < index - 1
        && assistantReplyRoot(messages[before]) === root) return message
    return projectSteerContinuationMessage(
      sealedAssistantBeforeSteer(messages, index),
      message,
    )
  })
  // Latest complete formatting wins all the way back through replay chains.
  for (let index = presented.length - 1; index >= 0; index -= 1) {
    const message = presented[index]
    if (!message?.steer_replay?.prefixRange) continue
    let before = index - 1
    while (isSteeredUserMessage(messages[before])) before -= 1
    presented[before] = projectSteerPrefixMessage(presented[before], message)
  }
  return presented
}
