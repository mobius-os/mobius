import { useEffect, useLayoutEffect, useRef, useState } from 'react'

import ChatInputBar from '../ChatView/ChatInputBar.jsx'
import ComposerPopover from '../ChatView/ComposerPopover.jsx'
import {
  composerDraftRevision,
  persistComposerDraft,
  readComposerDraft,
  readComposerDraftAsync,
} from '../ChatView/composerDraft.js'
import {
  focusComposerElement,
  placeCaretAtTextEnd,
} from '../ChatView/composerFocusPolicy.js'

function composerFiles(files) {
  return (files || []).map((file, index) => ({
    ...file,
    id: file.id || `new-chat-restored-${index}-${file.name || 'file'}`,
  }))
}

/**
 * New Chat's immediate, draft-only compose surface.
 *
 * The client-minted chat id is already the final draft owner, so every edit is
 * durable before POST /chats settles. Server-bound capabilities stay disabled
 * until the real ChatView is ready; Shell keeps this surface mounted above it
 * until that destination has taken focus and re-read the latest draft.
 */
export default function NewChatLanding({
  chatId = null,
  failure = null,
  focusToken = 0,
  onComposerReady,
  onRetry,
}) {
  const inputRef = useRef(null)
  const listeningRef = useRef(false)
  const readyRef = useRef(null)
  const initialRef = useRef(null)
  if (!initialRef.current) {
    initialRef.current = chatId == null
      ? { input: '', attachments: [] }
      : readComposerDraft(chatId)
  }
  const initialAttachments = composerFiles(initialRef.current.attachments)
  const attachmentsRef = useRef(initialAttachments)
  const inputValueRef = useRef(initialRef.current.input)
  const [input, setInput] = useState(initialRef.current.input)
  const [attachments, setAttachments] = useState(initialAttachments)

  function updateInput(next) {
    const value = String(next)
    inputValueRef.current = value
    if (chatId != null) {
      persistComposerDraft(chatId, value, attachmentsRef.current)
    }
    setInput(value)
  }

  // The synchronous session/live mirror wins first paint. IndexedDB repairs a
  // reload whose session write was older, but never overwrites a newer keypress.
  useEffect(() => {
    if (chatId == null) return undefined
    let cancelled = false
    const revision = composerDraftRevision(chatId)
    readComposerDraftAsync(chatId).then(saved => {
      if (cancelled || composerDraftRevision(chatId) !== revision) return
      const nextAttachments = composerFiles(saved.attachments)
      attachmentsRef.current = nextAttachments
      setAttachments(nextAttachments)
      if (saved.input !== inputValueRef.current) {
        inputValueRef.current = saved.input
        setInput(saved.input)
      }
    }).catch(() => {})
    return () => { cancelled = true }
  }, [chatId])

  // Shell mounts this during the New Chat tap. A layout effect transfers the
  // tap's live activation from the tiny fallback lease before the browser can
  // paint or dispatch another key event.
  useLayoutEffect(() => {
    if (chatId == null || !focusToken) return
    const readyKey = `${chatId}:${focusToken}`
    if (readyRef.current === readyKey) return
    readyRef.current = readyKey
    focusComposerElement(inputRef.current)
    placeCaretAtTextEnd(inputRef.current)
    const focused = globalThis.document?.activeElement === inputRef.current
    onComposerReady?.({ chatId: String(chatId), focusToken, focused })
  }, [chatId, focusToken, onComposerReady])

  function removeAttachment(fileId) {
    const next = attachmentsRef.current.filter(file => String(file.id) !== String(fileId))
    attachmentsRef.current = next
    setAttachments(next)
    if (chatId != null) persistComposerDraft(chatId, inputValueRef.current, next)
  }

  const liveComposer = chatId != null
  return (
    <div className="chat chat--empty">
      <div className="chat__empty-wrap">
        <div className="chat__empty">
          <img className="chat__empty-glyph" src="/moebius.png" alt="" width="76" height="76" />
          <p className="chat__empty-title">What&apos;s on your mind?</p>
          {failure && (
            <>
              <p className="chat__empty-sub" role="status">
                {failure === 'offline'
                  ? 'You’re offline — your draft is safe.'
                  : 'Couldn’t start a new chat — your draft is safe.'}
              </p>
              {onRetry && (
                <button
                  type="button"
                  className="chat__empty-action"
                  onPointerDown={event => event.preventDefault()}
                  onClick={onRetry}
                >
                  Retry
                </button>
              )}
            </>
          )}
        </div>
      </div>
      {liveComposer && (
        <div className="chat__foot">
          <ChatInputBar
            chatId={chatId}
            input={input}
            onInputChange={updateInput}
            onInputIntent={() => {}}
            onSubmit={event => event?.preventDefault?.()}
            onSubmitSteer={event => event?.preventDefault?.()}
            inputRef={inputRef}
            sending={false}
            listening={false}
            listeningRef={listeningRef}
            onManualVoiceEdit={() => {}}
            onToggleVoice={() => {}}
            onStop={() => {}}
            onSteer={() => {}}
            canSteer={false}
            offline={failure === 'offline'}
            submissionBlocked
            pendingFiles={attachments}
            onRemoveFile={removeAttachment}
            leftButtons={<ComposerPopover pending />}
            attachmentsDisabled
          />
        </div>
      )}
    </div>
  )
}
