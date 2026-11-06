/**
 * Owns the chat box's history browsing: the set-aside draft, kept edits,
 * queued-message saves, and bringing the draft back after a save or resend.
 */
import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react'
import {
  COMPOSER_DRAFT_STATE,
  composerHistoryEntries,
  composerHistoryPosition,
  composerQueuedEntries,
  moveComposerHistory,
  openComposerHistoryEntry,
  returnToComposerDraft,
  setComposerEntryAside,
} from '../composerHistory.js'

/** Why a queued edit stayed in the box. Each outcome comes from the queue's
 * own save (ChatView's handleUpdatePending); 'gone' is shown as "started". */
export const QUEUED_SAVE_NOTES = Object.freeze({
  empty: 'A queued message can’t be empty.',
  confirming: 'Delivery is being confirmed. Your edit is kept here.',
  waiting: 'Reconnect to save this edit. Your changes are kept here.',
  context_pending: 'This message includes app context. It can be edited after delivery is confirmed.',
  error: 'Couldn’t save this edit. Try again.',
})

/**
 * @param {object} options
 * @param {string} options.chatId
 * @param {string} options.value        what the chat box holds now
 * @param {Array}  options.sentHistory  composerHistoryFromMessages() entries
 * @param {Array}  options.queuedMessages visible pending rows, in queue order
 * @param {(value: string) => void} options.showValue  puts text in the box
 * @param {(cid: string, text: string) => Promise<string>} options.saveQueued
 *        the queue's save; resolves to 'saved', 'gone', 'confirming',
 *        'waiting', 'context_pending', or 'error'
 * @param {{ set: (draft: string) => void, take: () => (string|null) }} [options.draftAside]
 *        the draft's durable owner: `set` keeps the set-aside draft as the
 *        stored draft while the box shows other messages, and `take` hands
 *        it back (as edited elsewhere meanwhile) when browsing ends
 */
export default function useComposerHistory({
  chatId,
  value,
  sentHistory,
  queuedMessages,
  showValue,
  saveQueued,
  draftAside,
}) {
  const entries = useMemo(
    () => composerHistoryEntries(sentHistory || [], composerQueuedEntries(queuedMessages)),
    [sentHistory, queuedMessages],
  )
  const [browse, setBrowse] = useState(COMPOSER_DRAFT_STATE)
  // Key handlers and async save outcomes run between renders, so they read
  // the latest state, value, and list through refs.
  const browseRef = useRef(browse)
  const valueRef = useRef(value)
  valueRef.current = value
  const entriesRef = useRef(entries)
  entriesRef.current = entries
  const showValueRef = useRef(showValue)
  showValueRef.current = showValue
  const saveQueuedRef = useRef(saveQueued)
  saveQueuedRef.current = saveQueued
  const draftAsideRef = useRef(draftAside)
  draftAsideRef.current = draftAside
  // Armed by a send from history with the chat that armed it; the composer
  // clearing after that send is what brings the draft back, in that chat
  // only — a refused send can leave this armed, and the next chat's empty box
  // must not inherit the old chat's draft. A send that never happens leaves
  // the box untouched, and the next edit disarms it.
  const draftReturnRef = useRef(null)

  const commit = useCallback((next) => {
    browseRef.current = next
    setBrowse(next)
  }, [])

  const apply = useCallback((transition, extra = null) => {
    if (!transition) return false
    draftReturnRef.current = null
    const wasBrowsing = browseRef.current.key != null
    const next = { ...transition.state, ...(extra || {}), note: null, saving: false }
    let value = transition.value
    // Hand the draft to its durable owner before the box changes, so the
    // browsed text is never stored as the draft, and take it back on return.
    if (!wasBrowsing && next.key != null) {
      draftAsideRef.current?.set(next.draft)
    } else if (wasBrowsing && next.key == null) {
      const kept = draftAsideRef.current?.take()
      if (kept != null) value = kept
    }
    commit(next)
    showValueRef.current?.(value)
    return true
  }, [commit])

  useEffect(() => {
    draftReturnRef.current = null
    if (browseRef.current.key != null) draftAsideRef.current?.take()
    commit(COMPOSER_DRAFT_STATE)
  }, [chatId, commit])

  useLayoutEffect(() => {
    // The arming chat must still be on screen: this layout effect sees an
    // emptied box before the chat-switch reset above runs, and restoring
    // there would write the old chat's draft into the new chat.
    if (draftReturnRef.current !== chatId || value !== '') return
    draftReturnRef.current = null
    apply(returnToComposerDraft(browseRef.current))
  }, [chatId, value, apply])

  const move = useCallback((direction) => apply(moveComposerHistory(
    browseRef.current, entriesRef.current, direction, valueRef.current,
  )), [apply])

  const open = useCallback((key) => apply(openComposerHistoryEntry(
    browseRef.current, entriesRef.current, key, valueRef.current,
  )), [apply])

  const escape = useCallback(() => apply(returnToComposerDraft(
    browseRef.current, { discardEdits: true },
  )), [apply])

  /** The owner is cancelling a queued message; if it is in the box, give the
   * box back to the draft rather than reporting it as started. The cancel can
   * still fail and leave the message queued, so the box's text stays with it
   * as an unsaved edit that reopening shows. */
  const setAside = useCallback((key) => {
    if (browseRef.current.key !== key) return false
    return apply(setComposerEntryAside(
      browseRef.current, entriesRef.current, valueRef.current,
    ))
  }, [apply])

  /** Typing, dictation, paste, or a menu changed the browsed text. The
   * position and draft stay; only a stale note and an armed return clear. */
  const noteEdit = useCallback(() => {
    draftReturnRef.current = null
    const current = browseRef.current
    if (current.note) commit({ ...current, note: null })
  }, [commit])

  const saveCurrentQueued = useCallback(async (entry, text) => {
    const key = entry.key
    commit({ ...browseRef.current, saving: true, note: null })
    let outcome = 'error'
    try {
      outcome = await saveQueuedRef.current?.(key, text.trim())
    } catch {}
    const current = browseRef.current
    if (current.key !== key) {
      if (current.saving) commit({ ...current, saving: false })
      return
    }
    if (outcome === 'saved') {
      // Further typing during the save is a new edit of the saved text.
      if (valueRef.current === text) apply(returnToComposerDraft(current))
      else commit({ ...current, saving: false })
      return
    }
    if (outcome === 'gone') {
      commit({ ...current, saving: false, started: true })
      return
    }
    commit({
      ...current,
      saving: false,
      note: QUEUED_SAVE_NOTES[outcome] || QUEUED_SAVE_NOTES.error,
    })
  }, [apply, commit])

  /**
   * Enter or the send button. Returns 'handled' when a queued message was
   * saved (or the save explained why not), otherwise 'send': the caller sends
   * the box as a new message, and the draft returns once that send clears it.
   */
  const submit = useCallback(() => {
    const current = browseRef.current
    const position = composerHistoryPosition(current, entriesRef.current)
    if (!position) return 'send'
    if (position.kind === 'queued' && position.entry) {
      if (current.saving) return 'handled'
      const text = valueRef.current
      if (!text.trim()) {
        commit({ ...current, note: QUEUED_SAVE_NOTES.empty })
        return 'handled'
      }
      if (text.trim() === position.entry.text.trim()) {
        apply(returnToComposerDraft(current))
        return 'handled'
      }
      void saveCurrentQueued(position.entry, text)
      return 'handled'
    }
    draftReturnRef.current = chatId
    return 'send'
  }, [apply, chatId, commit, saveCurrentQueued])

  const position = composerHistoryPosition(browse, entries)
  return {
    browsing: browse.key != null,
    position,
    note: browse.note || null,
    saving: !!browse.saving,
    queuedKey: position?.kind === 'queued' ? browse.key : null,
    hasEntries: entries.length > 0,
    move,
    open,
    escape,
    setAside,
    noteEdit,
    submit,
  }
}
