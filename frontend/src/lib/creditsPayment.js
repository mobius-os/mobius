/**
 * Rules for the sheet a credits charge raises over the running app, where it
 * waits for the owner's Pay. Kept here so they are tested rather than eyeballed.
 */

/**
 * How long the sheet must stay quiet before Pay arms. An app's service
 * chooses when to raise a charge, so the sheet can land under a tap meant for
 * the app; browsers hold their own permission prompts back the same way.
 */
export const PRESS_ARM_MS = 700

/** Pay's arming for a sheet that appeared at `at`: off, and quiet since then. */
export function startArming(at) {
  return { quietSince: at, armedAt: null }
}

/**
 * Pay's arming after the owner pressed or typed on the sheet at `at`. While
 * Pay is still off, any press or key restarts the quiet wait, so an owner
 * tapping on through the sheet (aiming at the app beneath it) never arms Pay
 * under a later tap. Once armed, Pay stays armed: the deliberate press itself
 * must not turn it off again.
 */
export function restartArming(arming, at) {
  return arming.armedAt != null ? arming : startArming(at)
}

/** How long Pay still waits at `at` before it arms; 0 once it may. */
export function armingWait(arming, at) {
  if (arming.armedAt != null) return 0
  return Math.max(0, arming.quietSince + PRESS_ARM_MS - at)
}

/** Pay's arming at `at`: armed once the sheet has been quiet for PRESS_ARM_MS. */
export function armPay(arming, at) {
  return arming.armedAt == null && armingWait(arming, at) === 0 ? { ...arming, armedAt: at } : arming
}

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
 * What the sheet says when an answer is refused (`body`, the 409). A payment
 * answered already (a second press, or another device) is not a failure: a
 * paid one is still charged, so the sheet must never send the owner to buy it
 * again. Only a payment that lapsed or is unknown "didn't go through".
 */
export function answerRefusalSheet(body) {
  const detail = body?.detail
  const message = answerRefusal(body)
  if (detail?.code === 'payment_already_answered') {
    const paid = detail.state === 'confirmed'
    return { title: paid ? 'Already paid' : 'Already cancelled', message, failed: false }
  }
  return { title: 'This payment didn’t go through', message, failed: true }
}

/**
 * The waiting payments (`data`, as the server lists them) without the one the
 * owner just answered, so its sheet leaves at once rather than staying armed
 * for a second press until the list is read again.
 */
export function withoutPayment(data, paymentId) {
  if (!Array.isArray(data?.payments)) return data
  const payments = data.payments.filter(payment => String(payment?.id) !== String(paymentId))
  return payments.length === data.payments.length ? data : { ...data, payments }
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
 * How often the shell reads them anyway while the app is on screen. Each
 * payment is announced once, so one lost while the event stream was down
 * would otherwise hide its Pay sheet until the payment lapsed.
 */
export const WAITING_PAYMENTS_SAFETY_POLL_MS = 30_000

/**
 * How an app's waiting payments stay current. The shell hears each new one as
 * a system event (`pushed`), so it reads them on that event and on a return to
 * the tab, plus a slow safety read while the app is on screen in case an event
 * was missed. A page without that stream (an app on its own page,
 * /apps/<slug>/) polls faster while the app is on screen instead, or a charge
 * would wait unseen until it lapsed. Polling pauses while the app is off
 * screen or the tab is hidden, and a return to the tab reads them at once.
 */
export function waitingPaymentsRefresh({ pushed, visible }) {
  let refetchInterval = false
  if (visible) refetchInterval = pushed ? WAITING_PAYMENTS_SAFETY_POLL_MS : WAITING_PAYMENTS_POLL_MS
  return {
    staleTime: pushed ? 30_000 : 0,
    refetchInterval,
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
