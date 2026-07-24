/* Progressive drawer-row sizing keeps one continuous list without mounting it all. */

export const DRAWER_CHAT_BATCH_SIZE = 48

export function initialDrawerChatCount(total) {
  return Math.min(Math.max(0, total), DRAWER_CHAT_BATCH_SIZE)
}

export function nextDrawerChatCount(current, total) {
  const boundedTotal = Math.max(0, total)
  return Math.min(
    boundedTotal,
    Math.max(0, current) + DRAWER_CHAT_BATCH_SIZE,
  )
}

export function clampDrawerChatCount(current, total) {
  return Math.min(
    Math.max(initialDrawerChatCount(total), current),
    Math.max(0, total),
  )
}
