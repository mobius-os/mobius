import { isOwnerUserMessage } from './chatRuntimeState.js'
import { stripAugmentation } from './msgText.js'

/** Visible owner-authored text, oldest to newest. Attachment-only rows cannot
 * be reconstructed by the composer, so they are deliberately skipped. */
export function composerHistoryFromMessages(messages) {
  if (!Array.isArray(messages)) return []
  return messages
    .filter(isOwnerUserMessage)
    .map(message => (
      typeof message?.content === 'string'
        ? stripAugmentation(message.content)
        : ''
    ))
    .filter(content => content.trim().length > 0)
}

function hasModifier(event) {
  return !!(
    event?.altKey
    || event?.ctrlKey
    || event?.metaKey
    || event?.shiftKey
  )
}

function collapsedSelection(event) {
  const start = Number(event?.target?.selectionStart)
  const end = Number(event?.target?.selectionEnd)
  if (!Number.isInteger(start) || start !== end) return null
  return { start, end }
}

/**
 * Snapshot a non-empty draft before letting the browser perform ArrowUp.
 * Visual wrapping depends on the rendered textarea, so source newlines cannot
 * tell us whether the caret actually has another displayed line above it.
 */
export function composerHistoryNativeProbe(event, {
  history = [],
  index = null,
  value = '',
} = {}) {
  if (
    event?.key !== 'ArrowUp'
    || event.isComposing
    || event.nativeEvent?.isComposing
    || hasModifier(event)
    || index != null
    || !String(value).length
    || !Array.isArray(history)
    || history.length === 0
  ) {
    return null
  }
  const selection = collapsedSelection(event)
  if (!selection) return null
  return {
    target: event.target,
    value: String(value),
    ...selection,
  }
}

/** The native ArrowUp reached the visual top only when it left the caret
 * unchanged. Value and target checks discard stale probes after an edit. */
export function composerHistoryProbeReachedBoundary(probe, target) {
  return !!(
    probe
    && target
    && probe.target === target
    && target.value === probe.value
    && target.selectionStart === probe.start
    && target.selectionEnd === probe.end
  )
}

function mayStartHistory(event, value, nativeBoundary) {
  if (!collapsedSelection(event)) return false
  // Empty composers have no native caret movement to preserve. Non-empty
  // drafts are eligible only after the browser proves ArrowUp did not move.
  return !String(value).length || nativeBoundary
}

/**
 * Resolve an Up/Down key into the next composer-history state.
 *
 * `index=null` means the owner is editing their current draft. The first Up
 * snapshots that draft and recalls the newest sent message. Down from the
 * newest history entry restores the snapshot and leaves history mode.
 */
export function resolveComposerHistoryMove(event, {
  history = [],
  index = null,
  draft = '',
  value = '',
  nativeBoundary = false,
} = {}) {
  if (!event || (event.key !== 'ArrowUp' && event.key !== 'ArrowDown')) {
    return null
  }
  if (event.isComposing || event.nativeEvent?.isComposing || hasModifier(event)) {
    return null
  }

  const entries = Array.isArray(history) ? history : []
  if (event.key === 'ArrowUp') {
    if (entries.length === 0) return null
    if (index == null && !mayStartHistory(event, value, nativeBoundary)) return null
    const nextIndex = index == null
      ? entries.length - 1
      : Math.max(0, index - 1)
    return {
      value: entries[nextIndex],
      index: nextIndex,
      draft: index == null ? String(value) : draft,
    }
  }

  if (index == null || entries.length === 0) return null
  if (index >= entries.length - 1) {
    return {
      value: draft,
      index: null,
      draft: '',
    }
  }
  const nextIndex = index + 1
  return {
    value: entries[nextIndex],
    index: nextIndex,
    draft,
  }
}
