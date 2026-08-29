/** Apply exact chat-row facts without re-reading the complete drawer list. */

export function withChatListRowPatch(rows, chatId, patch = {}) {
  if (!Array.isArray(rows) || chatId == null) return rows
  const id = String(chatId)
  let changed = false
  const next = rows.map(row => {
    if (String(row?.id) !== id) return row
    let rowChanged = false
    const projected = { ...row }
    for (const [field, value] of Object.entries(patch)) {
      if (value === undefined || Object.is(row[field], value)) continue
      projected[field] = value
      rowChanged = true
    }
    if (!rowChanged) return row
    changed = true
    return projected
  })
  return changed ? next : rows
}

export function withChatOwnerActivity(rows, chatId, at = new Date().toISOString()) {
  const current = rows?.find?.(row => String(row?.id) === String(chatId))
  const activityAt = typeof at === 'string' && at > (current?.activity_at || '')
    ? at
    : current?.activity_at
  return withChatListRowPatch(rows, chatId, {
    has_messages: true,
    activity_at: activityAt,
  })
}

export function withChatRunState(rows, chatId, running) {
  return withChatListRowPatch(rows, chatId, {
    running: !!running,
    // A run cannot exist without an accepted owner/app message. This exact
    // event repairs another tab's empty-row projection without a list fetch.
    ...(running ? { has_messages: true } : {}),
  })
}

export function ownerInputChangeFromEvent(event) {
  const safeEvent = event && typeof event === 'object' ? event : {}
  const hasQuestionId = Object.hasOwn(safeEvent, 'questionId')
  const inputKind = ['question', 'secure_input'].includes(safeEvent.inputKind)
    ? safeEvent.inputKind
    // Compatibility while a new frontend and the prior backend generation can
    // briefly overlap during a live platform update.
    : hasQuestionId && safeEvent.questionId ? 'question' : null
  return {
    kind: inputKind,
    ...(hasQuestionId ? { questionId: safeEvent.questionId || null } : {}),
  }
}

export function withChatOwnerInput(
  rows,
  chatId,
  { kind = null, questionId } = {},
) {
  return withChatListRowPatch(rows, chatId, {
    owner_input_kind: kind,
    ...(questionId !== undefined
      ? { pending_question_id: questionId || null }
      : {}),
  })
}

export function withChatRename(rows, chatId, { title, updatedAt, activityAt } = {}) {
  return withChatListRowPatch(rows, chatId, {
    ...(typeof title === 'string' ? { title } : {}),
    ...(typeof updatedAt === 'string' ? { updated_at: updatedAt } : {}),
    ...(typeof activityAt === 'string' ? { activity_at: activityAt } : {}),
  })
}
