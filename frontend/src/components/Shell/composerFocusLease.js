import { focusComposerElement } from '../ChatView/composerFocusPolicy.js'

const TOUCH_PRIMARY_QUERY = '(hover: none) and (pointer: coarse)'

/**
 * Keep the software keyboard open across an async New-chat allocation. The
 * lease is focused synchronously inside the owner's tap; the real composer
 * takes that focus once its chat-bound surface mounts.
 */
export function beginTouchComposerFocusLease(el, {
  matchMediaImpl = globalThis.matchMedia,
  activeElement = globalThis.document?.activeElement,
  initialValue = '',
} = {}) {
  if (!el || activeElement === el || typeof matchMediaImpl !== 'function') return false
  if (matchMediaImpl(TOUCH_PRIMARY_QUERY)?.matches !== true) return false
  el.value = String(initialValue)
  const focused = focusComposerElement(el)
  if (focused) {
    const end = el.value.length
    try { el.setSelectionRange?.(end, end) } catch {}
  }
  return focused
}

export function releaseComposerFocusLease(el, {
  activeElement = globalThis.document?.activeElement,
} = {}) {
  if (!el) return
  if (activeElement === el && typeof el.blur === 'function') el.blur()
  el.value = ''
}

/** Settle the hidden touch buffer into one explicit composer handoff. */
export function composerFocusLeaseHandoff({
  autoSend = false,
  initialValue = '',
  leaseCandidate = null,
  leaseValue = '',
  leased = false,
  resolvedChatId,
  suppliedDraft = '',
} = {}) {
  if (suppliedDraft) {
    return {
      attachments: [],
      autoSend: !!autoSend,
      shouldStage: true,
      text: String(suppliedDraft),
    }
  }
  const sameCandidate = leaseCandidate
    && String(leaseCandidate.chatId) === String(resolvedChatId)
  return {
    attachments: sameCandidate ? leaseCandidate.draft?.attachments || [] : [],
    autoSend: false,
    shouldStage: !!leased && leaseValue !== initialValue,
    text: leased ? String(leaseValue) : '',
  }
}
