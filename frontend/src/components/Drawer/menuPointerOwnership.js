export function beginMenuPress(owner, { pointerId, action, isPrimary = true }) {
  if (!isPrimary || action == null || owner.press != null) return owner
  return { ...owner, press: { pointerId, action }, clickAction: null }
}

export function finishMenuPress(owner, { pointerId, action }) {
  if (owner.press?.pointerId !== pointerId) return owner
  const clickAction = owner.press.action === action ? action : null
  return { press: null, clickAction }
}

export function cancelMenuPress(owner, pointerId) {
  if (owner.press?.pointerId !== pointerId) return owner
  return { press: null, clickAction: null }
}

export function consumeMenuClick(owner, { detail, action }) {
  const keyboardActivation = detail === 0
  const allowed = keyboardActivation || (
    action != null && owner.clickAction === action
  )
  return {
    allowed,
    owner: { ...owner, clickAction: null },
  }
}
