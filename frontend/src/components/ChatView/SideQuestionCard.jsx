/* SideQuestionCard — the temporary `/btw` answer card above the chat box. */

import { ChatTemporary, X } from '@openai/apps-sdk-ui/components/Icon'
import { StandardMarkdown } from './markdown/BlockRenderer.jsx'
import Spinner from '../ui/Spinner.jsx'
import './SideQuestionCard.css'

/**
 * Presentational: useSideQuestion owns the request. The card says plainly
 * that nothing is in the chat until "Add to chat". Closing while the copy is
 * answering really cancels it: the hook aborts the request and the server
 * ends the fork.
 */
export default function SideQuestionCard({ sideQuestion, onAdd, onClose, onRetry }) {
  if (!sideQuestion) return null
  const { status, question, answer, error } = sideQuestion
  const answered = status === 'answered' || status === 'adding'
  return (
    <section className="side-question" aria-label="Side question" aria-busy={status === 'asking'}>
      <header className="side-question__head">
        <ChatTemporary className="side-question__icon" width={16} height={16} aria-hidden="true" />
        <span className="side-question__title">Side question</span>
        <span className="side-question__badge">Not added to this chat</span>
        <button
          type="button"
          className="side-question__close"
          onClick={onClose}
          aria-label={status === 'asking' ? 'Cancel side question' : 'Close side question'}
        >
          <X width={16} height={16} aria-hidden="true" />
        </button>
      </header>
      <p className="side-question__question">{question}</p>
      <div className="side-question__answer" role="status" aria-live="polite">
        {status === 'asking' && (
          <span className="side-question__thinking">
            <Spinner size={12} tone="accent" />
            Asking a temporary copy of this chat…
          </span>
        )}
        {answered && <StandardMarkdown text={answer} />}
        {error && <p className="side-question__error" role="alert">{error}</p>}
      </div>
      {(answered || status === 'failed') && (
        <div className="side-question__actions">
          {answered && (
            <button
              type="button"
              className="side-question__button side-question__button--primary"
              onClick={onAdd}
              disabled={status === 'adding'}
            >
              {status === 'adding' ? 'Adding…' : 'Add to chat'}
            </button>
          )}
          {status === 'failed' && sideQuestion.retryable !== false && (
            <button
              type="button"
              className="side-question__button side-question__button--primary"
              onClick={onRetry}
            >
              Try again
            </button>
          )}
          <button type="button" className="side-question__button" onClick={onClose}>
            Close
          </button>
        </div>
      )}
    </section>
  )
}
