/* One answerability rule shared by the reply shell and block renderer. */
import { isDurableRestartOffer } from './restartCard.js'

export function blockAnswerable(block, { msg, isLastMsg, liveQuestionId, onQuestionAnswer }) {
  const durableRestart = isDurableRestartOffer(block?.platform_action)
  return !!(
    onQuestionAnswer
    && msg.role === 'assistant'
    && block?.type === 'question'
    && !block.answers
    && (
      durableRestart
      || (isLastMsg && liveQuestionId && block.question_id === liveQuestionId)
    )
  )
}
