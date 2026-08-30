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

/**
 * Retire optimistic run markers only after the run was acknowledged and a
 * fresh drawer row explicitly says it is settled. A locally-started request
 * can briefly race ahead of its durable row, while a visible or owner-input
 * turn remains authoritative until its mounted stream reaches a boundary.
 */
export function withoutSettledLocalChatRuns(
  localIds,
  rows,
  { acknowledgedIds = localIds, protectedIds = new Set() } = {},
) {
  if (!(localIds instanceof Set) || !Array.isArray(rows)) return localIds
  const acknowledged = acknowledgedIds instanceof Set
    ? acknowledgedIds
    : new Set()
  const protectedLocal = protectedIds instanceof Set
    ? protectedIds
    : new Set()
  const serverSettled = new Set()
  for (const row of rows) {
    if (
      row?.id == null
      || row.running !== false
      || row.owner_input_kind != null
      || row.pending_question_id != null
    ) continue
    serverSettled.add(String(row.id))
  }

  let next = localIds
  for (const id of localIds) {
    const key = String(id)
    if (
      !acknowledged.has(key)
      || protectedLocal.has(key)
      || !serverSettled.has(key)
    ) continue
    if (next === localIds) next = new Set(localIds)
    next.delete(id)
  }
  return next
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

/**
 * Keep a committed rename ahead of drawer-list reads that began before it.
 * The guard retires only when a row carries the same committed revision (or a
 * newer one), so an older in-flight response cannot briefly resurrect the
 * first-message title after the live rename event already reached the shell.
 */
export function reconcileChatRenameGuards(rows, guards) {
  if (!Array.isArray(rows) || !(guards instanceof Map) || guards.size === 0) {
    return rows
  }

  let next = rows
  for (const [chatId, rename] of guards) {
    const row = next.find(item => String(item?.id) === String(chatId))
    if (!row) continue

    const rowUpdatedAt = typeof row.updated_at === 'string' ? row.updated_at : ''
    const renameUpdatedAt = typeof rename?.updatedAt === 'string'
      ? rename.updatedAt
      : ''
    const sameCommittedRename = row.title === rename?.title
      && (!renameUpdatedAt || rowUpdatedAt === renameUpdatedAt)
    const rowIsNewer = Boolean(renameUpdatedAt && rowUpdatedAt > renameUpdatedAt)
    if (sameCommittedRename || rowIsNewer) {
      guards.delete(chatId)
      continue
    }

    next = withChatRename(next, chatId, rename)
  }
  return next
}
