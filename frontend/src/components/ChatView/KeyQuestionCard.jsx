/* A saved key card: the ordinary QuestionCard plus the live vault request (useKeyRequestCard.js). */
import QuestionCard from './QuestionCard.jsx'
import useKeyRequestCard from './useKeyRequestCard.js'

/**
 * The wrapper carries the key answer guard (useKeyAnswerGuard.js): it hears
 * every press and key on the card and tracks where the card sits, so Submit
 * wakes only once the card has been seen, still and untouched for a moment.
 */
export default function KeyQuestionCard({ keyRequest, ...props }) {
  const keyCard = useKeyRequestCard(keyRequest, { live: !props.answeredMap && !props.disabled })
  return (
    <div {...keyCard.guard.cardProps}>
      <QuestionCard {...props} keyCard={keyCard} />
    </div>
  )
}
