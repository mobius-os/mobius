import { useCallback, useEffect, useRef, useState } from 'react'
import {
  composerDraftRevision,
  consumeComposerHandoff,
  persistComposerDraft,
  readComposerDraft,
  readComposerDraftAsync,
  readComposerHandoff,
} from '../composerDraft.js'
import {
  clearFailedSendAttempt,
  failedSendReconciliation,
  loadFailedSendAttempt,
  saveFailedSendAttempt,
} from '../sendAttemptRecovery.js'
import { resizeComposerTextarea } from '../composerTextareaSizing.js'
import useFileUpload from '../useFileUpload.js'

function readInitialComposer(chatId, { acceptPending = true } = {}) {
  try {
    const failedAttempt = loadFailedSendAttempt(chatId)
    const handoff = acceptPending
      ? readComposerHandoff(chatId)
      : { draft: null, autoSendDraft: null }
    const pending = handoff.draft
    if (pending && failedAttempt) clearFailedSendAttempt(chatId)
    const saved = readComposerDraft(chatId)
    const input = pending || failedAttempt?.text || saved.input
    return {
      input,
      autoSend: !!input && handoff.autoSendDraft === input,
      source: pending ? 'pending' : (failedAttempt ? 'failed' : 'saved'),
      failedAttempt: pending ? null : failedAttempt,
      attachments: pending ? [] : (failedAttempt?.attachments || saved.attachments),
    }
  } catch {
    return {
      input: '',
      autoSend: false,
      source: 'empty',
      failedAttempt: null,
      attachments: [],
    }
  }
}

/**
 * Owns one chat's durable composer value, attachment draft, and ambiguous-send
 * recovery identity. Every edit updates the ref and persistent draft before
 * scheduling React state, so navigation cannot lose the latest value.
 */
export default function useComposerDraftState({ chatId, hidden, inputRef }) {
  const initialRef = useRef(null)
  if (!initialRef.current) {
    initialRef.current = readInitialComposer(chatId, { acceptPending: !hidden })
  }
  const initial = initialRef.current
  const draftAttachmentsRef = useRef(initial.attachments)
  const [input, setInputState] = useState(() => initial.input)
  const inputValueRef = useRef(input)
  inputValueRef.current = input
  const [sendFailure, setSendFailure] = useState(() => (
    initial.failedAttempt
      ? 'Möbius is checking whether your previous message reached the chat…'
      : null
  ))
  const [pendingComposerSubmit, setPendingComposerSubmit] = useState(() => (
    initial.autoSend
      ? {
          token: `stored-handoff:${chatId}`,
          text: initial.input,
          storedHandoff: true,
        }
      : null
  ))
  const submittedComposerRequestTokenRef = useRef(null)
  const failedSendAttemptRef = useRef(initial.failedAttempt)

  const setComposerInput = useCallback((nextInput) => {
    inputValueRef.current = nextInput
    persistComposerDraft(chatId, nextInput, draftAttachmentsRef.current)
    setInputState(nextInput)
  }, [chatId])

  const {
    files: pendingFiles,
    addFiles,
    removeFile,
    clearFiles,
    restoreFiles,
    releaseFiles,
  } = useFileUpload({
    chatId,
    initialFiles: initial.attachments,
    onFilesChange: nextFiles => {
      draftAttachmentsRef.current = nextFiles
      persistComposerDraft(chatId, inputValueRef.current, nextFiles)
    },
  })

  useEffect(() => {
    if (hidden) return
    if ((initial.source === 'pending' || initial.source === 'failed')
        && (initial.input || initial.attachments.length > 0)) {
      persistComposerDraft(chatId, initial.input, initial.attachments)
    }
    // An autosend handoff remains one-shot intent until the send begins below.
    // Consuming it during restoration would turn a reload while loading into a
    // silently downgraded draft.
    if (!initial.autoSend) consumeComposerHandoff(chatId, initial.input)
  }, [chatId, hidden, initial])

  useEffect(() => {
    let cancelled = false
    const revision = composerDraftRevision(chatId)
    readComposerDraftAsync(chatId).then(saved => {
      if (cancelled || composerDraftRevision(chatId) !== revision) return
      const currentFiles = draftAttachmentsRef.current
      const sameFiles = currentFiles.length === saved.attachments.length
        && currentFiles.every((file, index) => {
          const other = saved.attachments[index]
          return file?.name === other?.name
            && file?.size === other?.size
            && file?.mime_type === other?.mime_type
            && file?.status === other?.status
        })
      if (saved.input === inputValueRef.current && sameFiles) return
      inputValueRef.current = saved.input
      setInputState(saved.input)
      draftAttachmentsRef.current = saved.attachments
      restoreFiles(saved.attachments)
    }).catch(() => {})
    return () => { cancelled = true }
  }, [chatId, restoreFiles])

  const clearFailedAttempt = useCallback(() => {
    if (!failedSendAttemptRef.current) return
    failedSendAttemptRef.current = null
    clearFailedSendAttempt(chatId)
  }, [chatId])

  const rememberFailedAttempt = useCallback((attempt) => {
    failedSendAttemptRef.current = attempt
    saveFailedSendAttempt(chatId, attempt)
  }, [chatId])

  // An ambiguous POST restores the exact cid-tagged draft until server truth
  // proves where it landed. Keep the state transition beside the composer and
  // file owners so transcript reconciliation cannot clear only half the draft.
  const reconcileFailedAttempt = useCallback((
    visibleMessages,
    pendingMessages,
    { reportMissing = false } = {},
  ) => {
    const reconciliation = failedSendReconciliation(
      failedSendAttemptRef.current,
      visibleMessages,
      pendingMessages,
      { reportMissing },
    )
    if (reconciliation.status !== 'durable') {
      if (reconciliation.sendFailure) setSendFailure(reconciliation.sendFailure)
      return reconciliation.status
    }
    clearFailedAttempt()
    setComposerInput('')
    clearFiles()
    setSendFailure(null)
    return 'durable'
  }, [clearFailedAttempt, clearFiles, setComposerInput])

  const handleComposerInputChange = useCallback((nextInput) => {
    clearFailedAttempt()
    setSendFailure(null)
    setComposerInput(nextInput)
  }, [clearFailedAttempt, setComposerInput])

  const handleComposerAddFiles = useCallback((fileList) => {
    clearFailedAttempt()
    setSendFailure(null)
    return addFiles(fileList)
  }, [addFiles, clearFailedAttempt])

  const handleComposerRemoveFile = useCallback((fileId) => {
    clearFailedAttempt()
    setSendFailure(null)
    return removeFile(fileId)
  }, [clearFailedAttempt, removeFile])

  const restoreComposerText = useCallback((
    text,
    { focus = false, preserveFailedAttempt = false } = {},
  ) => {
    if (preserveFailedAttempt) setComposerInput(text)
    else handleComposerInputChange(text)
    requestAnimationFrame(() => {
      const element = inputRef.current
      if (!element) return
      resizeComposerTextarea(element, text)
      if (focus) {
        try { element.focus({ preventScroll: true }) }
        catch { element.focus() }
      }
      const end = String(text).length
      try { element.setSelectionRange(end, end) } catch {}
      element.scrollTop = element.scrollHeight
    })
  }, [handleComposerInputChange, inputRef, setComposerInput])

  const restoreDurableDraft = useCallback(() => {
    const saved = readComposerDraft(chatId)
    if (saved.input !== inputValueRef.current) {
      inputValueRef.current = saved.input
      setInputState(saved.input)
    }
    restoreFiles(saved.attachments)
  }, [chatId, restoreFiles])

  return {
    input,
    inputValueRef,
    setComposerInput,
    sendFailure,
    setSendFailure,
    pendingComposerSubmit,
    setPendingComposerSubmit,
    submittedComposerRequestTokenRef,
    failedSendAttemptRef,
    clearFailedAttempt,
    rememberFailedAttempt,
    reconcileFailedAttempt,
    pendingFiles,
    clearFiles,
    restoreFiles,
    releaseFiles,
    handleComposerInputChange,
    handleComposerAddFiles,
    handleComposerRemoveFile,
    restoreComposerText,
    restoreDurableDraft,
  }
}
