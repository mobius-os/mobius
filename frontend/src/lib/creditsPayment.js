/**
 * Rules for the sheets an app's credits raise over the running app: a charge
 * waiting for the owner's Pay, and a local app's credits question. Kept here
 * so they are tested rather than eyeballed.
 */

import { money } from './creditsWallet.js'

/**
 * How long Pay (or Allow) stays off after its sheet appears. An app's service
 * chooses when to raise a charge, so the sheet can land under a tap meant for
 * the app; browsers hold their own permission prompts back the same way.
 */
export const PRESS_ARM_MS = 700

/**
 * Whether a press on the sheet's main button counts as the owner's answer.
 *
 * A pointer press counts only when it started on the button after the button
 * armed (`armedAt`): a finger already down when the sheet appeared, lifted
 * after it armed, is not a choice the owner made. A keyboard press needs focus
 * moved to the button deliberately (the sheet focuses Cancel), so it counts
 * once the button is armed.
 */
export function pressCounts({ armedAt, pressStartedAt, keyboard }) {
  if (armedAt == null) return false
  if (keyboard) return true
  return pressStartedAt != null && pressStartedAt >= armedAt
}

/**
 * What the payment sheet shows the owner: the creator's price, and the most
 * the app agreed to pay when that is higher. A card pays in the currency the
 * credits service sells in, where one credit is one unit of that currency.
 */
export function paymentAmount(payment) {
  const maxMicro = Number(payment?.max_price_micro) || 0
  const priceMicro = payment?.price_micro == null ? maxMicro : Number(payment.price_micro)
  const byCard = payment?.method === 'card'
  // Credits read as the server wrote them (`price`, `max_price`).
  const price = byCard ? money(priceMicro / 10_000, payment?.currency) : payment?.price || payment?.max_price
  const most = byCard ? money(maxMicro / 10_000, payment?.currency) : payment?.max_price
  return {
    figure: price,
    unit: byCard ? 'by card' : 'credits',
    ceiling: maxMicro > priceMicro ? most : null,
  }
}

/**
 * Why an answer to a payment did not go through, in the server's words when
 * it gave any. A refused card payment is no longer waiting, so this is all the
 * owner learns of it.
 */
export function answerRefusal(body) {
  const detail = body?.detail
  const message = typeof detail === 'string' ? detail : detail?.message
  return message || 'That could not be saved. Try again.'
}

/**
 * Whether an app's contract asks to see, manage or spend the owner's credits.
 * Only such an app can have a payment or a credits question to show, so the
 * shell asks about neither for any other app. Mirrors the backend's
 * `app_consent.asks_for_credits`.
 */
export function asksForCredits(contract) {
  const data = contract?.data
  if (!data || typeof data !== 'object') return false
  const targets = data.spend_credits
  return data.credits_manage === true || (Array.isArray(targets) && targets.length > 0)
}
