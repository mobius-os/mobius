/**
 * Rules for the sheet a credits charge raises over the running app, where it
 * waits for the owner's Pay. Kept here so they are tested rather than eyeballed.
 */

/**
 * How long Pay stays off after its sheet appears. An app's service
 * chooses when to raise a charge, so the sheet can land under a tap meant for
 * the app; browsers hold their own permission prompts back the same way.
 */
export const PRESS_ARM_MS = 700

/**
 * Whether a press on Pay counts as the owner's answer.
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
 * the app agreed to pay when that is higher. Credits read as the server wrote
 * them (`price`, `max_price`).
 */
export function paymentAmount(payment) {
  const maxMicro = Number(payment?.max_price_micro) || 0
  const priceMicro = payment?.price_micro == null ? maxMicro : Number(payment.price_micro)
  return {
    figure: payment?.price || payment?.max_price,
    ceiling: maxMicro > priceMicro ? payment?.max_price : null,
  }
}

/**
 * Why an answer to a payment did not go through, in the server's words when
 * it gave any. The payment is no longer waiting, so this is all the owner
 * learns of it.
 */
export function answerRefusal(body) {
  const detail = body?.detail
  const message = typeof detail === 'string' ? detail : detail?.message
  return message || 'That could not be saved. Try again.'
}

/**
 * Whether an app's contract may spend the owner's credits. Only such an app
 * can have a payment waiting, so the shell reads waiting payments for no
 * other app.
 */
export function paysWithCredits(contract) {
  const targets = contract?.data?.spend_credits
  return Array.isArray(targets) && targets.length > 0
}

/** How often a page that hears no payment events reads them, while on screen. */
export const WAITING_PAYMENTS_POLL_MS = 5_000

/**
 * How an app's waiting payments stay current. The shell hears each new one as
 * a system event (`pushed`), so it reads them on that event and on a return to
 * the tab. A page without that stream (an app on its own page,
 * /apps/<slug>/) polls while the app is on screen instead, or a charge would
 * wait unseen until it lapsed. Polling pauses while the tab is hidden, and a
 * return to the tab reads them at once.
 */
export function waitingPaymentsRefresh({ pushed, visible }) {
  return {
    staleTime: pushed ? 30_000 : 0,
    refetchInterval: !pushed && visible ? WAITING_PAYMENTS_POLL_MS : false,
    refetchOnWindowFocus: true,
  }
}

/**
 * The shell notice for a payment an app raised, or null when the owner already
 * sees its Pay sheet. The sheet shows over the paying app only, so an app off
 * screen would otherwise wait unseen until its payment lapsed.
 */
export function waitingPaymentNotice({ appId, visibleAppIds, apps }) {
  const id = appId == null ? '' : String(appId)
  if (!id || [...(visibleAppIds || [])].some(visible => String(visible) === id)) return null
  const name = (apps || []).find(app => String(app?.id) === id)?.name || 'An app'
  return {
    message: `${name} is asking you to confirm a payment. Open it to Pay or Cancel.`,
    noticeKey: `credits-payment:${id}`,
  }
}
