/* Whether a question card still asks the owner something. One check for the
   card's own controls, its answerability, and Copy, so a card never reads as
   open in one place and closed in another. */
import { isDurableRestartOffer, restartCardStatusLabel } from './restartCard.js'

/** A platform action (a restart card) that has already been acted on: the
 *  card shows its outcome instead of asking. */
export function questionActionCompleted(action) {
  return Boolean(restartCardStatusLabel(action)) && action?.status !== 'awaiting_owner'
}

/** An unanswered card the owner can still answer: the chat's open question
 *  (`liveQuestionId`, null once it is answered or the turn ends), or a
 *  durable Restart offer, which stays actionable until pressed. Answered,
 *  completed, stopped and superseded cards are closed. */
export function questionCardOpen(block, liveQuestionId) {
  return block?.type === 'question'
    && !block.answers
    && !questionActionCompleted(block.platform_action)
    && (isDurableRestartOffer(block.platform_action)
      || (!!liveQuestionId && block.question_id === liveQuestionId))
}
