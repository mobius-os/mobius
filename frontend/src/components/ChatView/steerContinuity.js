/* Project exact post-steer text replay as one continuous assistant answer. */

import { safeSteerMarkdownCut } from './markdown/steerContinuation.js'


export function isSteeredUserMessage(message) {
  return !!(
    message
    && message.role === 'user'
    && !message.hidden
    && message.steered === true
  )
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


function soleTextBlockContent(message) {
  if (!message) return ''
  if (!Array.isArray(message.blocks) || message.blocks.length === 0) {
    return typeof message.content === 'string' ? message.content : ''
  }
  const textBlocks = message.blocks.filter(block => (
    block?.type === 'text' && typeof block.content === 'string' && block.content
  ))
  // Joining several text blocks would invent separators and could cross tool,
  // question, or activity boundaries. Exactness is more important than reach.
  return textBlocks.length === 1 ? textBlocks[0].content : ''
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
    if (!safeSteerMarkdownCut(text, prefix.length)) return null
    return text.slice(prefix.length)
  }
  // While the provider is replaying the already-visible prefix, hold that
  // provisional duplicate off-screen. If one character diverges, this branch
  // stops matching and the complete accumulated text appears unchanged.
  if (active && prefix.startsWith(text)) return ''
  return null
}


/**
 * Return a presentation-only assistant message. Stored content is never
 * rewritten: a mismatch, a settled short response, or an unsafe Markdown cut
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
  const prefix = soleTextBlockContent(sealedMessage)
  if (!prefix) return continuationMessage

  const blocks = continuationMessage.blocks
  if (Array.isArray(blocks) && blocks.length > 0) {
    const textIndex = firstContinuationTextBlock(blocks)
    if (textIndex < 0) return continuationMessage
    const text = String(blocks[textIndex]?.content || '')
    const projected = projectedText(prefix, text, { active })
    if (projected == null) return continuationMessage
    const nextBlocks = blocks.slice()
    nextBlocks[textIndex] = { ...blocks[textIndex], content: projected }
    return {
      ...continuationMessage,
      content: projected,
      blocks: nextBlocks,
    }
  }

  const text = String(continuationMessage.content || '')
  const projected = projectedText(prefix, text, { active })
  if (projected == null) return continuationMessage
  return { ...continuationMessage, content: projected }
}


/** Apply the exact replay projection to settled transcript rows. */
export function projectSettledSteerContinuations(messages) {
  if (!Array.isArray(messages)) return []
  return messages.map((message, index) => {
    if (message?.role !== 'assistant') return message
    return projectSteerContinuationMessage(
      sealedAssistantBeforeSteer(messages, index),
      message,
    )
  })
}
