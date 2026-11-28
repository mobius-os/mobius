import DependencyBacklinks from './DependencyBacklinks.jsx'
import { InlineMarkdown, StandardMarkdown } from './markdown/BlockRenderer.jsx'
import { INTERACTIVE_TARGET } from '../../lib/interactiveTarget.js'
import { ChevronRight } from '@openai/apps-sdk-ui/components/Icon'
import { LocalAnswersContext } from './localAnswersContext.js'
import { RequestEntryActionsContext } from './requestEntryActionsContext.js'
import { useCallback, useContext, useEffect, useId, useLayoutEffect, useRef, useState } from 'react'
import './QuestionCard.css'
import {
  clearQuestionDraft,
  clearRequestListDrafts,
  questionDraftKey,
  readQuestionDraft,
  readRequestListDraft,
  writeQuestionDraft,
  writeRequestListDraft,
} from './questionDraft.js'
import { autoGrowTextarea, textareaUsesNativeSizing } from './composerTextareaSizing.js'
import { placeCaretAtTextEnd } from './composerFocusPolicy.js'
import { isInlineEditorSubmit, isPlainTextPasteShortcut } from './composerShortcuts.js'
import {
  assistantClipboardText,
  insertClipboardText,
  queueClipboardTextUndoably,
} from './markdownClipboard.js'
import { isTouchPrimary } from '../../lib/pointerPrimary.js'
import {
  pointerSelectionChangedWithin,
  textSelectionSnapshot,
} from '../../lib/selectableTextControl.js'
import { getOnlineSnapshot } from '../../lib/connectivityStore.js'
import useFileUpload from './useFileUpload.js'
import FileChips from './FileChips.jsx'
import Attachments from './Attachments.jsx'
import { pastedFiles, filePasteNeedsDefaultPrevented } from './pasteUpload.js'
import { Paperclip } from '@openai/apps-sdk-ui/components/Icon'
import { ORPHANED_FILE_GROUP, fileBelongsToQuestion, fileQuestionTag, restoredQuestionFiles, resolveQuestionAnswer, questionOptionSubmission, unansweredQuestions } from './questionSubmission.js'
import {
  isRestartCardAction,
  restartCardSelectedOptions,
  restartCardStatusDetail,
  restartCardStatusLabel,
} from './restartCard.js'
import { questionActionCompleted } from './questionOpen.js'
import { KEY_ALLOW } from './keyRequestCardState.js'
import { earlyAnswerHint } from '../SettingsView/useKeyAnswerGuard.js'
import KeyRequestDetails from './KeyRequestDetails.jsx'
import { KeyPasteField } from '../SettingsView/KeyRequest.jsx'
import {
  APPROVAL,
  QUESTION,
  RESTART,
  SECRET,
  entryKind,
  entryState,
  mergedEntryStates,
  restartPressSelection,
  sharedAnswerValues,
  shownEntries,
} from './requestEntries.js'
import { RestartEntry, SecretEntry } from './RequestEntries.jsx'


const CUSTOM_ANSWER_MAX_HEIGHT = 180
// The server's bound on one answer's files; checked here so the owner hears
// it when attaching rather than as a failed submit.
const MAX_ANSWER_FILES = 20


// A refused list-card submission gets fixed copy; server text never reaches
// the card. The refusal's code says which copy applies.
function listCardRefusalMessage(error) {
  const status = Number(error?.status)
  if (error?.code === 'card_out_of_date') {
    return 'A question on this card changed while you were answering. The updated card appears when the agent finishes this step.'
  }
  if (status === 410) return 'This card was replaced by a newer one. What you entered here is kept there.'
  if (status === 409) return 'Möbius couldn’t accept these answers as sent. Check your choices and submit again.'
  return ''
}


function resizeCustomAnswer(textarea) {
  autoGrowTextarea(textarea, CUSTOM_ANSWER_MAX_HEIGHT)
}


export function CustomAnswerArea({
  answered,
  canSubmit,
  disabled,
  label,
  placeholder,
  onChange,
  onSubmitShortcut,
  onPasteFiles,
  questionTextId,
  value,
}) {
  const id = useId()
  const textareaRef = useRef(null)
  const plainPasteRef = useRef(false)
  const pendingCaretRef = useRef(null)

  useLayoutEffect(() => {
    const textarea = textareaRef.current
    resizeCustomAnswer(textarea)
    const caret = pendingCaretRef.current
    pendingCaretRef.current = null
    if (caret !== null) {
      try { textarea?.setSelectionRange(caret, caret) } catch { /* detached */ }
    }
  }, [value])

  // Paste behaves as in the message composer: files attach, copied Möbius
  // text keeps its Markdown (plain with Cmd/Ctrl+Shift+V), and the insertion
  // stays undoable where the browser allows it.
  function handlePaste(e) {
    const preferPlainText = plainPasteRef.current
    plainPasteRef.current = false
    if (answered) return
    const files = onPasteFiles ? pastedFiles(e.clipboardData) : []
    if (files.length) {
      if (filePasteNeedsDefaultPrevented(e.clipboardData, files)) e.preventDefault()
      onPasteFiles(files)
      return
    }
    const text = assistantClipboardText(e.clipboardData, preferPlainText)
    if (text === null) return
    e.preventDefault()
    const { selectionStart, selectionEnd } = e.currentTarget
    const insertControlled = () => {
      const next = insertClipboardText(value, selectionStart, selectionEnd, text)
      pendingCaretRef.current = next.caret
      onChange(next.value)
    }
    if (!queueClipboardTextUndoably(e.currentTarget, text, insertControlled)) insertControlled()
  }

  // The measured fallback also reacts to width: wrapping can add lines without
  // changing the answer value when a pane or device rotates.
  useEffect(() => {
    const textarea = textareaRef.current
    if (
      !textarea
      || textareaUsesNativeSizing()
      || typeof ResizeObserver === 'undefined'
    ) return undefined
    let lastWidth = -1
    const observer = new ResizeObserver(() => {
      const width = textarea.clientWidth
      if (width === lastWidth) return
      lastWidth = width
      resizeCustomAnswer(textarea)
    })
    observer.observe(textarea)
    return () => observer.disconnect()
  }, [])

  return (
    <textarea
      ref={textareaRef}
      className="qcard__input"
      data-chat-scroll-region
      data-chat-inline-editor="question-answer"
      id={id}
      aria-label={label || 'Custom answer for:'}
      aria-labelledby={label ? undefined : `${id} ${questionTextId}`}
      placeholder={answered ? 'No custom answer' : (placeholder || 'Or type your own answer…')}
      autoComplete="off"
      rows={1}
      wrap="soft"
      value={value}
      onChange={e => onChange(e.target.value)}
      onPaste={handlePaste}
      onKeyUp={() => { plainPasteRef.current = false }}
      onFocus={e => placeCaretAtTextEnd(e.currentTarget)}
      readOnly={answered}
      disabled={disabled && !answered}
      onKeyDown={e => {
        plainPasteRef.current = isPlainTextPasteShortcut(e)
        // Let Enter stay a newline until every question is answered, so a
        // half-filled grouped card can still take multi-line custom text.
        if (!canSubmit) return
        if (isInlineEditorSubmit(e, { isTouchPrimary: isTouchPrimary() })) {
          e.preventDefault()
          onSubmitShortcut(e.currentTarget.closest('.qcard'))
        }
      }}
    />
  )
}


export default function QuestionCard({
  chatId,
  questions,
  questionId,
  answeredMap,
  neededBy = [],
  onInternalNav,
  platformAction,
  // An agent's key ask (KeyQuestionCard.jsx): the card answers the vault first.
  keyCard = null,
  submittedOptions,
  attachments,
  onAnswer,
  onPrepareAnswer,
  onCancelAnswer,
  disabled,
  // A request-list card (block.input_requests): each question is one
  // independent owner-input request, identified across republication by its
  // `id`. The owner may answer some and leave the rest open.
  inputRequests = false,
  // block.request_states: what a list card's restart and secret entries
  // settled on their own, keyed by request id.
  requestStates,
  // Press Restart now on one entry; defaults to the chat's entry actions.
  onRestartEntry,
  // Callback ref that publishes this card's node to the "Möbius asked you
  // something — tap to answer" offscreen observer. Set only by the surface
  // rendering the answerable tail question, and only while the card is still
  // unanswered — the cue exists to send the owner back to a card that is
  // blocking the turn, and a submitted card no longer is. Because a live→
  // durable surface handoff remounts this component, the observer's target
  // has to come from here (the node's own render) rather than a lookup.
  pendingCardRef,
}) {
  const draftKey = questionDraftKey(chatId, questionId, questions)
  const cardId = useId()
  // Ordinary cards keep one draft per card. A request-list card keeps one per
  // request (see readRequestListDraft), so a republished request restores the
  // owner's half-typed input. Only an answerable list card reads or writes
  // them: a superseded card must neither echo nor overwrite the draft its
  // successor now owns, so a card first shown inactive loads them once it
  // becomes answerable.
  const requestDraftsLoadedRef = useRef(false)
  const initialDraftRef = useRef(null)
  if (initialDraftRef.current === null) {
    if (!inputRequests) {
      initialDraftRef.current = { ...readQuestionDraft(draftKey), updated: [] }
    } else if (!disabled && !answeredMap) {
      requestDraftsLoadedRef.current = true
      initialDraftRef.current = readRequestListDraft(chatId, questions)
    } else {
      initialDraftRef.current = { answers: {}, otherTexts: {}, files: [], updated: [] }
    }
  }
  const [answers, setAnswers] = useState(() => initialDraftRef.current.answers)
  const [otherTexts, setOtherTexts] = useState(() => initialDraftRef.current.otherTexts)
  // Request ids whose question changed since the owner's draft was typed.
  const [updatedRequests, setUpdatedRequests] = useState(() => initialDraftRef.current.updated)
  const [submitting, setSubmitting] = useState(false)
  const [submitted, setSubmitted] = useState(null)
  const [submitError, setSubmitError] = useState('')
  // Set by a tap on Submit while answers are missing: the unanswered
  // questions' hints turn to the accent colour so the gap is easy to find.
  const [showMissing, setShowMissing] = useState(false)
  const pointerSelectionRef = useRef(null)
  const preparedSubmissionRef = useRef(null)
  const fileInputRef = useRef(null)
  const initialFilesRef = useRef(null)
  if (initialFilesRef.current === null) initialFilesRef.current = restoredQuestionFiles(initialDraftRef.current.files, questions)
  const attachTargetRef = useRef(null)
  const { files, addFiles, removeFile, clearFiles, discardFiles } = useFileUpload({ chatId, initialFiles: initialFilesRef.current })
  const readyFiles = files.filter(file => file.status === 'done')
  // The limit covers the whole card, matching the server's per-submission cap.
  function addAnswerFiles(list, question) {
    const room = MAX_ANSWER_FILES - files.length
    if (list.length > room) setSubmitError(`Attach at most ${MAX_ANSWER_FILES} files to one card.`)
    return room > 0 ? addFiles(list.slice(0, room), question) : Promise.resolve()
  }
  // Like the composer: wait for uploads in flight; a failed one shows its
  // error on the chip and is simply not sent.
  const pendingFiles = files.some(file => file.status === 'uploading')

  const localAnswers = useContext(LocalAnswersContext)
  const entryActions = useContext(RequestEntryActionsContext)
  // A state this tab saw an entry settle into before the block carries it
  // (a Restart now or sealed-submit response). The block's own record wins.
  const [localEntryStates, setLocalEntryStates] = useState({})
  const states = inputRequests ? mergedEntryStates(requestStates, localEntryStates) : {}
  const noteEntryState = (requestId, state) => {
    setLocalEntryStates(prev => ({ ...prev, [requestId]: state }))
    entryActions?.applyEntryState?.(questionId, requestId, state)
  }
  const localAnswer = (localAnswers || []).find(record => (
    String(record.chatId) === String(chatId)
    && record.body?.question_id === questionId
  ))
  const actionStatusLabel = restartCardStatusLabel(platformAction)
  const actionStatusDetail = restartCardStatusDetail(platformAction)
  // A ready-boot receipt is independent of the offered action. Keep an
  // unanswered button and written reply available without manufacturing an
  // owner answer or bypassing the server's continuation hold.
  const completedAction = questionActionCompleted(platformAction)
  const answered = Boolean(submitted) || !!answeredMap || completedAction
  const locallyQueued = !answered && Boolean(localAnswer)
  const selectionLocked = answered || locallyQueued
  // The choice hint, paperclip, and Submit slot. A card first shown inactive
  // (history; a stopped or superseded question opened later) stays compact.
  // Once this mount has shown the card answerable, Stop or supersession only
  // makes those controls inert: removing them collapsed the card by ~80px
  // under the owner's eyes, and at the chat bottom the transcript slid down.
  // The flag only ever latches on, so an interrupted render can at worst keep
  // the inert controls; it can never hide them from a live card.
  const shownAnswerableRef = useRef(!disabled)
  if (!disabled) shownAnswerableRef.current = true
  const showsAnswerControls = answered || shownAnswerableRef.current
  const attachLocked = selectionLocked || submitting || disabled
  const displayAnswers = answeredMap || localAnswer?.body?.answers || submitted?.answers || {}
  const grouped = questions.length > 1
  const restartAction = isRestartCardAction(platformAction)
  const writtenRestartAction = restartAction && platformAction?.version === 2
  const respondedRestartAction = (
    writtenRestartAction && platformAction?.status === 'responded'
  )
  // A key card takes only its choices: no typed reply and no files.
  const choicesOnly = Boolean(platformAction || keyCard)
  const chosenKeyOption = keyCard
    ? (questions[0]?.options || []).find(option => option.label === answers[questions[0]?.question])?.id
    : null
  // A list card no longer answerable (submitted, superseded, or copied into a
  // branch) keeps only the entries answered or settled on it. The others were
  // never answered here: they come back on the next card.
  const listClosed = inputRequests && (answered || (disabled && !locallyQueued))
  const visibleQuestions = inputRequests
    ? shownEntries(questions, { answers: displayAnswers, states, closed: listClosed })
    : questions
  const shownGrouped = visibleQuestions.length > 1
  // Restart and secret entries settle on their own; questions, approvals and
  // a written reply to a restart go through the shared Submit. An entry
  // already settled takes no further part.
  const settledIds = inputRequests
    ? questions.filter(q => q.id && entryState(states, q)).map(q => q.id).join('\n')
    : ''
  const sharedQuestions = inputRequests
    ? questions.filter(q => entryKind(q) !== SECRET && !entryState(states, q))
    : questions
  const openEntryCount = inputRequests
    ? questions.filter(q => !entryState(states, q)).length
    : questions.length
  const onlyRestartReplies = inputRequests && sharedQuestions.length > 0
    && sharedQuestions.every(q => entryKind(q) === RESTART)
  const pressRestart = onRestartEntry || entryActions?.pressRestartEntry

  // ChatView is keyed by chat, so switching away remounts this card. Keep an
  // unsubmitted selection in the same per-tab cache as composer drafts; the
  // owner can inspect another chat and return without rebuilding their answer.
  // Only a committed answer clears its draft. `disabled` is intentionally NOT
  // a clearing signal: during an offline reconnect the live/persisted render
  // sources can briefly hand off through a disabled card. Clearing there used
  // to erase the owner's selection precisely when the network returned.
  useEffect(() => {
    if (inputRequests) return
    if (answered) {
      clearQuestionDraft(draftKey)
      // Files still held here were never sent from this tab. The server keeps
      // any another tab's answer claimed, so discarding them all is safe.
      if (files.length) discardFiles()
      return
    }
    writeQuestionDraft(draftKey, { answers, otherTexts, files })
  }, [inputRequests, draftKey, answers, otherTexts, files, answered, discardFiles])

  // A list card first shown inactive (e.g. before the open-card marker
  // arrived) picks up its requests' drafts once it becomes answerable.
  useEffect(() => {
    if (!inputRequests || requestDraftsLoadedRef.current || disabled || answered) return
    requestDraftsLoadedRef.current = true
    const draft = readRequestListDraft(chatId, questions)
    setAnswers(prev => ({ ...draft.answers, ...prev }))
    setOtherTexts(prev => ({ ...draft.otherTexts, ...prev }))
    setUpdatedRequests(draft.updated)
  }, [inputRequests, disabled, answered, chatId, questions])

  // Answered requests never reappear, so their drafts are cleared; requests
  // left open keep theirs for the card that republishes them. A list card
  // writes only while it is the answerable one.
  const answeredRequestIds = inputRequests && answered
    ? questions.filter(q => q.id && displayAnswers[q.question]).map(q => q.id).join('\n')
    : ''
  useEffect(() => {
    if (!inputRequests) return
    // An entry settled on its own (Restart now, a sealed submission) is never
    // offered again, so its reply draft goes with it. A secret entry never
    // has a draft: its values exist only in its form until submitted.
    const settled = settledIds ? settledIds.split('\n') : []
    if (answered) {
      clearRequestListDrafts(chatId, [...(answeredRequestIds ? answeredRequestIds.split('\n') : []), ...settled])
      if (files.length) discardFiles()
      return
    }
    if (disabled || !requestDraftsLoadedRef.current) return
    if (settled.length) clearRequestListDrafts(chatId, settled)
    // A Restart now choice is never kept: a later card must not come back
    // with the restart already chosen.
    writeRequestListDraft(
      chatId,
      questions.filter(q => entryKind(q) !== SECRET && !settled.includes(q.id)),
      { answers: sharedAnswerValues(questions, answers), otherTexts, files, updated: updatedRequests },
    )
  }, [inputRequests, chatId, questions, answers, otherTexts, files, updatedRequests, answered, answeredRequestIds, settledIds, disabled, discardFiles])

  // What the shared Submit carries: choosing Restart now is not an answer.
  const sharedAnswers = inputRequests ? sharedAnswerValues(questions, answers) : answers
  const unanswered = unansweredQuestions(sharedQuestions, sharedAnswers, otherTexts, readyFiles)
  const answeredCount = sharedQuestions.length - unanswered.length
  // On a list card "all answered" means nothing open is left behind, so an
  // open secret or unpressed restart keeps a submission partial.
  const allAnswered = inputRequests
    ? answeredCount === openEntryCount
    : unanswered.length === 0
  // A request-list card submits whatever is answered; the rest stay open.
  const enoughAnswered = inputRequests ? answeredCount > 0 : allAnswered
  const partialSubmit = inputRequests && answeredCount > 0 && !allAnswered
  const selectedOptions = restartCardSelectedOptions(
    platformAction,
    questions,
    answers,
  )
  const canSubmit = enoughAnswered && !pendingFiles && (!restartAction || selectedOptions !== null)
    && (!keyCard || ['pending', 'denied', 'allowed', 'gone'].includes(keyCard.state) && keyCard.ready(chosenKeyOption))
  // Incomplete cards stay tappable to reveal the first missing answer. Pending
  // uploads alone still disable submission until their durable IDs arrive.
  const unansweredLook = !enoughAnswered && !pendingFiles && !restartAction
    && !selectionLocked
  const needsAnswers = unansweredLook && !disabled && !submitting
  // Quiet until a tap: the card shows no progress count up front. A partial
  // list submission says what it sends in its button (submitLabel).
  let submitHint = ''
  if (needsAnswers && showMissing) {
    submitHint = inputRequests
      ? 'Answer at least one question to submit'
      : 'Answer each question before submitting'
  }

  function showFirstUnanswered(questionCard) {
    setShowMissing(true)
    // A restart has no answer to point at; lead to something answerable.
    const index = questions.indexOf(
      unanswered.find(q => entryKind(q) !== RESTART) || unanswered[0],
    )
    const node = questionCard?.querySelector(`[data-question-index="${index}"]`)
    if (!node) return
    const reduce = window.matchMedia?.('(prefers-reduced-motion: reduce)')?.matches
    node.scrollIntoView?.({ block: 'center', behavior: reduce ? 'auto' : 'smooth' })
    node.querySelector('.qcard__opt-choice[tabindex="0"], textarea')?.focus({ preventScroll: true })
  }

  function selectOption(question, label) {
    if (selectionLocked || disabled) return
    const q = questions.find(qq => qq.question === question)
    if (!q?.multiSelect) {
      setOtherTexts(prev => ({ ...prev, [question]: '' }))
    }
    setAnswers(prev => {
      if (q?.multiSelect) {
        const current = prev[question] || []
        const arr = Array.isArray(current) ? current : [current]
        const next = arr.includes(label)
          ? arr.filter(l => l !== label)
          : [...arr, label]
        return { ...prev, [question]: next }
      }
      return { ...prev, [question]: label }
    })
  }

  function setOtherText(question, text) {
    setOtherTexts(prev => ({ ...prev, [question]: text }))
    setAnswers(prev => {
      const q = questions.find(qq => qq.question === question)
      if (q?.multiSelect) {
        const current = prev[question] || []
        const arr = Array.isArray(current) ? current : [current]
        const withoutOther = arr.filter(label => label !== '__other__')
        return {
          ...prev,
          [question]: text.trim()
            ? [...withoutOther, '__other__']
            : withoutOther,
        }
      }
      return {
        ...prev,
        [question]: text.trim()
          ? '__other__'
          : (prev[question] === '__other__' ? '' : prev[question]),
      }
    })
  }

  const cancelPreparedSubmission = useCallback(() => {
    const prepared = preparedSubmissionRef.current
    preparedSubmissionRef.current = null
    if (prepared) onCancelAnswer?.(prepared)
  }, [onCancelAnswer])

  useEffect(() => () => cancelPreparedSubmission(), [cancelPreparedSubmission])

  useEffect(() => {
    // A remote answer, source replacement, or competing submission can make
    // the button inactive between press and click. Retire the provisional hold
    // immediately; there will be no answer handoff to release it later.
    if (selectionLocked || disabled || submitting) cancelPreparedSubmission()
  }, [selectionLocked, cancelPreparedSubmission, disabled, submitting])

  async function handleSubmit(questionCard = null, preparedSubmission = null) {
    if (!canSubmit || selectionLocked || disabled || submitting) {
      if (preparedSubmission) onCancelAnswer?.(preparedSubmission)
      return
    }
    preparedSubmissionRef.current = null
    if (keyCard) {
      // The owner's answer reaches the vault first; the card only records it.
      setSubmitError('')
      setSubmitting(true)
      try {
        await keyCard.settle(chosenKeyOption)
      } catch (error) {
        setSubmitError(error?.message || 'That answer didn’t save. Please try again.')
        setSubmitting(false)
        if (preparedSubmission) onCancelAnswer?.(preparedSubmission)
        return
      }
    }
    const resolved = {}
    // A list card sends only the requests that have an answer; a blank one is
    // omitted entirely so it stays open rather than being answered with "".
    // Only questions, approvals and written restart replies take part: a
    // secret or Restart now never travels through the answers path.
    const submittedQuestions = inputRequests
      ? sharedQuestions.filter(q => !unanswered.includes(q))
      : questions
    const optionSubmission = questionOptionSubmission(submittedQuestions, sharedAnswers)
    // `on_answer: "close"` must not end the conversation while requests stay open.
    if (inputRequests && !allAnswered) optionSubmission.closeOnlySelection = false
    const lines = submittedQuestions.map(q => {
      const own = readyFiles.filter(file => fileBelongsToQuestion(file.group, q.question, questions.length))
      const val = resolveQuestionAnswer(sharedAnswers[q.question], otherTexts[q.question])
        || (own.length ? `Attached ${own.length} file${own.length === 1 ? '' : 's'}` : '')
      resolved[q.question] = val
      const filesLine = own.length ? `\n  Files: ${own.map(file => file.name).join(', ')}` : ''
      return `- ${q.question}: ${val.replace(/\n/g, '\n  ')}${filesLine}`
    })
    const answerAttachments = readyFiles.map(({ name, size, mime_type, group }) => ({
      name, size, mime_type, ...(fileQuestionTag(group) != null ? { question: group } : {}),
    }))
    setSubmitError('')
    setSubmitting(true)
    try {
      const accepted = await onAnswer?.(
        lines.join('\n'),
        resolved,
        questionId,
        { questionCard, preparedSubmission, attachments: answerAttachments, ...optionSubmission },
      )
      // Only settle (and therefore clear the durable per-tab draft) after the
      // answer endpoint confirms that the transcript write committed.
      if (accepted === false || accepted?.status === 'locally_queued' || accepted?.status === 'locally_settled') {
        if (preparedSubmission) onCancelAnswer?.(preparedSubmission)
      } else {
        setSubmitted({ answers: resolved, attachments: answerAttachments })
        clearFiles()
      }
    } catch (error) {
      // Keep the choices and custom text intact so a transient failure is
      // immediately retryable. Keep the notice on the card too: adding an
      // assistant-looking error row after it makes the question cease to be
      // the transcript tail and disables the very retry the owner needs.
      // Only a 409 explains the answer itself (why it cannot be accepted), so
      // show its detail; every other failure keeps the friendly retry copy so
      // raw backend strings never reach the card.
      // An ordinary card keeps its existing 409 explanation; a list card
      // shows only fixed copy (listCardRefusalMessage).
      const rejectionDetail = inputRequests
        ? listCardRefusalMessage(error)
        : error?.status === 409 && typeof error?.detail === 'string'
          ? error.detail.trim()
          : ''
      setSubmitError(
        !getOnlineSnapshot()
          ? 'You’re offline. Your choice is saved — submit it when you’re back online.'
          : rejectionDetail
            || 'That answer didn’t save. Your choice is still here — please try again.',
      )
    } finally {
      setSubmitting(false)
    }
  }

  // A partial list submission counts what it sends against everything the
  // card still has open, of every kind: "Submit 1 of 3 answers" on a card
  // with a question, a secret and a restart says two stay open.
  let submitLabel = writtenRestartAction ? 'Continue' : 'Submit'
  if (partialSubmit) submitLabel = `Submit ${answeredCount} of ${openEntryCount} answers`
  if (answered) submitLabel = 'Submitted'
  if (submitting) submitLabel = 'Submitting…'
  if (locallyQueued) submitLabel = localAnswer.deliveryOutcome === 'delivered'
    ? 'Confirming answer…' : 'Queued on this device'

  // Files sit inside the answer they belong to. Card-level files sit below the
  // answers: untagged ones on a grouped card (they predate per-answer files)
  // and restored draft files whose question left the card.
  const sentAttachments = attachments || localAnswer?.body?.attachments || submitted?.attachments || []
  const fileTray = (owns, label) => choicesOnly ? null : (
    <div className="qcard__answer-files" role="group" aria-label={label}>
      {selectionLocked
        ? <Attachments attachments={sentAttachments.filter(file => owns(file.question))} chatId={chatId} />
        : <FileChips files={files.filter(file => owns(file.group))} onRemove={removeFile} chatId={chatId} disabled={submitting || disabled} />}
    </div>
  )
  const cardLevel = tag => tag === ORPHANED_FILE_GROUP || (grouped && tag == null)
  const hasCardFiles = selectionLocked
    ? sentAttachments.some(file => cardLevel(file.question))
    : files.some(file => cardLevel(file.group))

  // A list card the agent carried on past with nothing answered or settled
  // is drawn by the reply as an "Asked a question" activity row instead
  // (askedQuestionsActivity.js); entries left unanswered on a card that kept
  // answers are not listed.
  if (inputRequests && listClosed && visibleQuestions.length === 0) return null

  // A list card's title counts what it shows; a card holding more than
  // questions and approvals counts requests rather than decisions.
  const sensitiveShown = inputRequests && visibleQuestions.some(q => entryKind(q) === RESTART || entryKind(q) === SECRET)
  let groupNoun = sensitiveShown ? 'requests' : 'decisions'
  if (listClosed) groupNoun = answered ? 'answered' : 'settled here'
  const groupTitle = `${visibleQuestions.length} ${groupNoun}`
  // A list card says nothing its Submit button does not already say.
  const groupCopy = inputRequests ? '' : 'Answer each question, then submit them together.'
  const entryInteractive = !disabled && !selectionLocked
  // The shared Submit belongs to questions, approvals and restart replies. A
  // card of only secrets has none, and a restart that would be its only entry
  // sends its reply with its own Continue, as the legacy Restart card did. A
  // card closed without answers (superseded) shows no Submit slot.
  const showsSharedSubmit = !inputRequests || (listClosed
    ? answered
    : (sharedQuestions.length > 0 && !onlyRestartReplies) || locallyQueued)

  return (
    <div
      className={`qcard${shownGrouped ? ' qcard--grouped' : ''}${answered ? ' qcard--answered' : ''}`}
      data-scroll-anchor-key={draftKey}
      data-question-id={questionId}
      tabIndex={-1}
      ref={answered ? null : pendingCardRef}
      aria-disabled={disabled && !answered ? true : undefined}
      aria-label={shownGrouped ? groupTitle : undefined}
    >
      {actionStatusLabel && (
        <div className="qcard__action-status" role="status">
          {actionStatusLabel}
          {actionStatusDetail && (
            <span className="qcard__action-detail">{actionStatusDetail}</span>
          )}
        </div>
      )}
      {shownGrouped && (
        <div className="qcard__group-head">
          <div>
            <div className="qcard__group-title">{groupTitle}</div>
            {groupCopy && <div className="qcard__group-copy">{groupCopy}</div>}
          </div>
          <span className="qcard__group-count">{visibleQuestions.length}</span>
        </div>
      )}
      <div className="qcard__questions">
        {visibleQuestions.map(q => {
        const qi = questions.indexOf(q)
        const kind = inputRequests ? entryKind(q) : QUESTION
        const state = inputRequests ? entryState(states, q) : null
        const missing = showMissing && needsAnswers && unanswered.includes(q)
        if (kind === SECRET) {
          return (
            <div key={q.id ?? qi} className="qcard__q qcard__q--secret" data-question-index={qi}>
              <SecretEntry
                chatId={chatId}
                cardId={questionId}
                entry={q}
                state={state}
                interactive={entryInteractive}
                onState={noteEntryState}
              />
            </div>
          )
        }
        if (kind === RESTART) {
          const selection = restartPressSelection(q)
          const answeredText = displayAnswers[q.question] || ''
          const restartOption = (q.options || []).find(opt => selection?.[q.id]?.[0] === opt.id)
          const restartTextId = `${cardId}-q${qi}`
          return (
            <div key={q.id ?? qi} className={`qcard__q qcard__q--restart${missing ? ' qcard__q--missing' : ''}`} data-question-index={qi}>
              <RestartEntry
                entry={q}
                state={state}
                interactive={entryInteractive}
                textId={restartTextId}
                chosen={Boolean(restartOption) && answers[q.question] === restartOption.label}
                onChoose={() => restartOption && selectOption(q.question, restartOption.label)}
                onPress={pressRestart && selection
                  ? () => pressRestart({ questionId, requestId: q.id, selectedOptions: selection })
                  : undefined}
                onState={noteEntryState}
                answeredText={answeredText}
                onInternalNav={onInternalNav}
                onSubmitReply={onlyRestartReplies
                  ? questionCard => handleSubmit(questionCard, null)
                  : undefined}
                replyReady={canSubmit}
                replySubmitting={submitting}
                replyError={onlyRestartReplies && !answered && !locallyQueued ? submitError : ''}
                reply={(
                  <div className="qcard__answer-row">
                    <div className={`qcard__composer${otherTexts[q.question] || answeredText ? ' qcard__composer--active' : ''}`}>
                      <CustomAnswerArea
                        answered={selectionLocked}
                        canSubmit={canSubmit && allAnswered}
                        disabled={selectionLocked || disabled || submitting}
                        label={`Reply instead of restarting: ${q.question}`}
                        placeholder="Or tell me what you’d like to do instead…"
                        onChange={text => setOtherText(q.question, text)}
                        onSubmitShortcut={(questionCard) => {
                          if (canSubmit) handleSubmit(questionCard, null)
                        }}
                        value={selectionLocked ? answeredText : (otherTexts[q.question] || '')}
                      />
                    </div>
                  </div>
                )}
              />
            </div>
          )
        }
        const isApproval = kind === APPROVAL
        const selected = answers[q.question]
        const isMulti = q.multiSelect
        const options = q.options || []
        // Allow on a key another app saved for itself stays hidden until the
        // owner opens "use that key anyway", unless Allow is already chosen.
        const visibleOptions = writtenRestartAction
          ? options.filter(opt => opt.id === platformAction.restart_option_id)
          : keyCard?.hidesAllow && chosenKeyOption !== KEY_ALLOW
            ? options.filter(opt => opt.id !== KEY_ALLOW)
            : options
        const hasOptions = visibleOptions.length > 0
        const selectedArr = isMulti
          ? (Array.isArray(selected) ? selected : [])
          : []
        const isOtherSelected = isMulti
          ? selectedArr.includes('__other__')
          : selected === '__other__'
        const inactive = selectionLocked || disabled || submitting

        const answeredValue = displayAnswers[q.question]
          || (submitted ? resolveQuestionAnswer(answers[q.question], otherTexts[q.question]) : '')
        // A list card records which option ids were picked, so a label that
        // itself contains ", " still highlights correctly.
        const submittedIds = inputRequests && Array.isArray(submittedOptions?.[q.id])
          ? submittedOptions[q.id]
          : null
        const answeredArr = selectionLocked && isMulti
          ? (submittedIds
              ? options.filter(o => submittedIds.includes(o.id)).map(o => o.label)
              : (answeredValue ? answeredValue.split(', ').map(s => s.trim()) : []))
          : []
        const unmatchedAnswers = selectionLocked
          ? (isMulti
              ? (submittedIds ? [] : answeredArr.filter(v => !options.some(o => o.label === v)))
              : (answeredValue && !options.some(o => o.label === answeredValue)
                  ? [answeredValue]
                  : []))
          : []
        const writtenRestartResponse = Boolean(
          respondedRestartAction
          && submittedOptions
          && Object.keys(submittedOptions).length === 0
        )
        const writtenAnswer = writtenRestartResponse || !hasOptions
          ? answeredValue
          : unmatchedAnswers.join(', ')
        const answeredWithOther = writtenAnswer.length > 0
        const selectionCount = selectionLocked
          ? (isMulti ? answeredArr.length : (answeredValue ? 1 : 0))
          : selectedArr.length
        // Screen-reader names point at the rendered question, not its Markdown.
        const questionTextId = `${cardId}-q${qi}`
        const attachId = `${questionTextId}-attach`
        const showsUpdated = inputRequests && !selectionLocked
          && updatedRequests.includes(q.id)
        // A list card's question carries no title of its own (the agent's
        // header repeated the prompt); an approval keeps the label that says
        // it decides something.
        const headerText = inputRequests ? (isApproval ? 'Approval' : '') : q.header

        return (
          <div
            key={inputRequests ? (q.id ?? qi) : qi}
            className={`qcard__q${isApproval ? ' qcard__q--approval' : ''}${missing ? ' qcard__q--missing' : ''}`}
            data-question-index={qi}
          >
            {(headerText || showsUpdated) && (
              <div className="qcard__header">
                {headerText && <InlineMarkdown text={headerText} />}
                {showsUpdated && <span className="qcard__updated">Updated</span>}
              </div>
            )}
            <div className="qcard__text" id={questionTextId}><StandardMarkdown text={q.question} literalTags onInternalNav={onInternalNav} /></div>
            {keyCard && !answered && <KeyRequestDetails card={keyCard} />}
            {inputRequests && q.context && (
              <div className="qcard__context">{q.context}</div>
            )}
            {isApproval && selectionLocked && answeredWithOther && (
              <div className="qcard__entry-status" role="status">
                Replied instead
                <span className="qcard__action-detail">Your reply went to the agent as a message. This was neither approved nor declined.</span>
              </div>
            )}
            {/* Single- vs multi-select was indistinguishable until you tapped
                and watched whether a prior pick cleared. Surface it up front:
                a caption (with a live count for multi) plus a per-option glyph
                (□ checkbox for multi, ○ radio for single). */}
            {!completedAction && showsAnswerControls && hasOptions && (
              <div className="qcard__hint">
                {writtenRestartAction
                  ? platformAction.observation?.observed_at
                    ? 'Restart again, or reply below'
                    : 'Restart now, or reply below'
                  : isApproval
                  ? 'Choose one to decide'
                  : isMulti
                  ? `Select all that apply${selectionCount ? ` · ${selectionCount} selected` : ''}`
                  : 'Choose one'}
              </div>
            )}
            {/* Selection state was conveyed only by a CSS class — silent to
                screen readers. Expose it as a radiogroup (single) / group of
                checkboxes (multi) with per-option aria-checked. */}
            {!completedAction && hasOptions && <div
              className="qcard__opts"
              role={isMulti ? 'group' : 'radiogroup'}
              aria-label={isApproval ? `Decision: ${q.question}` : undefined}
              aria-labelledby={isApproval ? undefined : questionTextId}
            >
              {/* For multi-select answered state, the comma-joined value is
                  parsed above so each chosen option highlights correctly. */}
              {visibleOptions.map((opt, oi) => {
                const isChosen = selectionLocked
                  ? (isMulti ? answeredArr.includes(opt.label) : answeredValue === opt.label)
                  : false
                const isActive = selectionLocked
                  ? isChosen
                  : (isMulti ? selectedArr.includes(opt.label) : selected === opt.label)
                const dimmed = answered && !isChosen
                const descriptionId = opt.description ? `${questionTextId}-opt${oi}` : undefined
                const pick = (event) => {
                  const selectionBeforePointer = pointerSelectionRef.current
                  pointerSelectionRef.current = null
                  if (
                    event.detail !== 0
                    && pointerSelectionChangedWithin(selectionBeforePointer, event.currentTarget)
                  ) return
                  selectOption(q.question, opt.label)
                }
                return (
                  <div
                    key={oi}
                    className={`qcard__opt${isActive ? ' qcard__opt--on' : ''}${dimmed ? ' qcard__opt--dim' : ''}${inactive ? ' qcard__opt--static' : ''}`}
                    onPointerDown={inactive ? undefined : () => {
                      pointerSelectionRef.current = textSelectionSnapshot()
                    }}
                    // A tap on the explanation text picks the choice too.
                    // Its links, image previews (including the portaled
                    // lightbox) and copy buttons, and the choice itself, keep
                    // their own clicks.
                    onClick={inactive ? undefined : (event) => {
                      if (!event.currentTarget.contains(event.target)) return
                      if (event.target.closest(INTERACTIVE_TARGET)) return
                      pick(event)
                    }}
                  >
                    {/* The control holds only the formatted label, so it never
                        nests links or buttons. The explanation stays visible
                        on touch as its own content, described to assistive
                        technology through aria-describedby. */}
                    <div
                      className="qcard__opt-choice"
                      tabIndex={inactive ? undefined : 0}
                      role={isMulti ? 'checkbox' : 'radio'}
                      aria-checked={isActive}
                      aria-disabled={inactive || undefined}
                      aria-describedby={descriptionId}
                      onClick={inactive ? undefined : pick}
                      onKeyDown={inactive ? undefined : (event) => {
                        if (event.repeat || (event.key !== 'Enter' && event.key !== ' ')) return
                        event.preventDefault()
                        selectOption(q.question, opt.label)
                      }}
                    >
                      <span
                        className={`qcard__mark qcard__mark--${isMulti ? 'box' : 'radio'}`}
                        aria-hidden="true"
                      />
                      <span className="qcard__opt-label"><InlineMarkdown text={opt.label} /></span>
                    </div>
                    {opt.description && (
                      <div className="qcard__opt-desc" id={descriptionId}>
                        <StandardMarkdown text={opt.description} literalTags onInternalNav={onInternalNav} />
                      </div>
                    )}
                  </div>
                )
              })}
            </div>}
            {keyCard?.state === 'pending' && keyCard.needsValue && !answered && chosenKeyOption === KEY_ALLOW && (
              <KeyPasteField answer={keyCard.answer} disabled={inactive} />
            )}
            {/* An approval is decided only by one of its options. A reply
                written instead is sent as a message that neither approves nor
                declines and leaves the approval open; its accessible name
                says so. */}
            {isApproval && ((!selectionLocked && showsAnswerControls) || (selectionLocked && answeredWithOther)) && (
              <div className="qcard__answer-row">
                <div className={`qcard__composer${isOtherSelected || answeredWithOther ? ' qcard__composer--active' : ''}`}>
                  <CustomAnswerArea
                    answered={selectionLocked}
                    canSubmit={canSubmit && allAnswered}
                    disabled={inactive}
                    label={`Reply instead of deciding: ${q.question}`}
                    placeholder="Or type a reply…"
                    onChange={text => setOtherText(q.question, text)}
                    onSubmitShortcut={(questionCard) => {
                      if (canSubmit) handleSubmit(questionCard, null)
                    }}
                    value={selectionLocked ? writtenAnswer : (otherTexts[q.question] || '')}
                  />
                </div>
              </div>
            )}
            {!isApproval && (!completedAction || respondedRestartAction)
              && !keyCard
              && (!restartAction || writtenRestartAction) && (
              <div className="qcard__answer-row">
                {/* Each answer has its own paperclip on the left of its box.
                    It stays while the action row does, so Submit, queueing
                    and the Submitted state never move the card; once the
                    answer is locked it only stops taking files. */}
                {!choicesOnly && showsAnswerControls && (
                  <button type="button" className="qcard__attach"
                    id={attachId}
                    aria-label={grouped ? 'Attach a photo or file to your answer to:' : 'Attach a photo or file'}
                    aria-labelledby={grouped ? `${attachId} ${questionTextId}` : undefined}
                    title="Attach a photo or file" disabled={attachLocked} onClick={() => {
                      attachTargetRef.current = q.question
                      fileInputRef.current?.click()
                    }}>
                    <Paperclip width={18} height={18} aria-hidden="true" />
                  </button>
                )}
                <div className={`qcard__composer${isOtherSelected || answeredWithOther ? ' qcard__composer--active' : ''}`}>
                  {fileTray(tag => fileBelongsToQuestion(tag, q.question, questions.length), 'Files for this answer')}
                  <CustomAnswerArea
                    answered={selectionLocked}
                    // Enter stays a newline until every request is answered,
                    // so a list card never sends a partial answer by a keystroke.
                    canSubmit={canSubmit && allAnswered}
                    disabled={inactive}
                    placeholder={writtenRestartAction
                      ? 'Or tell me what you’d like to do instead…'
                      : hasOptions ? undefined : 'Type your answer…'}
                    onChange={text => setOtherText(q.question, text)}
                    onPasteFiles={platformAction || inactive ? undefined : pasted => addAnswerFiles(pasted, q.question)}
                    onSubmitShortcut={(questionCard) => {
                      if (canSubmit) handleSubmit(questionCard, null)
                    }}
                    questionTextId={questionTextId}
                    value={selectionLocked
                      ? writtenAnswer
                      : (otherTexts[q.question] || '')}
                  />
                </div>
              </div>
            )}
          </div>
        )
        })}
      </div>
      {!!neededBy.length && <details className="qcard__related-work">
        <summary>Related work<ChevronRight className="chat__panel-chevron" width={14} height={14} aria-hidden="true" /></summary>
        <DependencyBacklinks chats={neededBy} onInternalNav={onInternalNav} />
      </details>}
      {hasCardFiles && fileTray(cardLevel, 'Files for all answers')}
      {!choicesOnly && showsAnswerControls && (
        <input ref={fileInputRef} type="file" multiple className="qcard__file-input"
          disabled={attachLocked}
          aria-label="Attach files to your answer"
          onChange={e => {
            const selected = Array.from(e.target.files || [])
            e.target.value = ''
            if (!attachLocked) addAnswerFiles(selected, attachTargetRef.current)
          }} />
      )}
      {!completedAction && showsAnswerControls && showsSharedSubmit && (
        <>
          {locallyQueued && (
            <div className="qcard__queue-status" role="status">
              {localAnswer.deliveryOutcome === 'delivered'
                ? 'Your answer reached Möbius. Confirming this card…'
                : 'Your answer is saved here and will send when Möbius reconnects.'}
            </div>
          )}
          {submitError && !answered && !locallyQueued && !inputRequests && (
            <div className="qcard__submit-error" role="status">{submitError}</div>
          )}
          {/* The hint a tap on an incomplete Submit asks for is the button's
              own label, so it appears without moving anything; this copy
              only announces it. */}
          <div className="qcard__submit-hint chat__sr-only" role="status">{submitHint}</div>
          {keyCard && keyCard.guard.early !== null && !answered && (
            <div className="qcard__key-hint" role="status">{earlyAnswerHint(keyCard.guard.early)}</div>
          )}
          <button
            type="button"
            // A card stopped before it was answered keeps the quiet outline it had,
            // dimmed by :disabled, rather than turning into a dimmed primary.
            className={`qcard__submit${unansweredLook ? ' qcard__submit--incomplete' : ''}`}
            aria-disabled={needsAnswers || (keyCard && !keyCard.guard.active) || undefined}
            onPointerDown={(event) => {
              if (needsAnswers) return
              if (event.button !== 0 || event.isPrimary === false) return
              const questionCard = event.currentTarget.closest('.qcard')
              preparedSubmissionRef.current = onPrepareAnswer?.(questionCard) || null
            }}
            onPointerCancel={cancelPreparedSubmission}
            onBlur={cancelPreparedSubmission}
            onPointerLeave={(event) => {
              if (event.buttons !== 0) cancelPreparedSubmission()
            }}
            onClick={(event) => {
              if (needsAnswers) {
                showFirstUnanswered(event.currentTarget.closest('.qcard'))
                return
              }
              const prepared = preparedSubmissionRef.current
              preparedSubmissionRef.current = null
              // Submit on a key card wakes only once the card has been still
              // and untouched for a moment, as every key answer does.
              if (keyCard && !keyCard.guard.counts(event)) {
                if (prepared) onCancelAnswer?.(prepared)
                return
              }
              handleSubmit(event.currentTarget.closest('.qcard'), prepared)
            }}
            disabled={(!canSubmit && !needsAnswers) || disabled || selectionLocked || submitting}
          >
            {submitHint || submitLabel}
          </button>
          {/* A list card's refusal sits under its Submit: the button the owner
              just pressed stays where it was. */}
          {submitError && !answered && !locallyQueued && inputRequests && (
            <div className="qcard__submit-error qcard__submit-error--after" role="status">{submitError}</div>
          )}
        </>
      )}
    </div>
  )
}
