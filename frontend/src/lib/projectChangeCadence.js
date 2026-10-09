// Cadence for an open project's durable change-cursor poll.
//
// The cursor is the only change feed for invited editors, and it also carries
// owner-side facts the system stream does not publish (an agent run finishing
// after direct filesystem edits). So it keeps running, but it is quick only
// while the project is actually changing: each empty read stretches the wait,
// and any observed change (polled or pushed) snaps it back to the active rate.
export const PROJECT_CHANGES_ACTIVE_MS = 2_500
export const PROJECT_CHANGES_IDLE_MAX_MS = 20_000
export const PROJECT_CHANGES_FAILURE_MAX_MS = 30_000

export function nextProjectChangesDelay(previous, outcome) {
  if (outcome === 'changed') return PROJECT_CHANGES_ACTIVE_MS
  if (outcome === 'failed') {
    return Math.min(previous * 2, PROJECT_CHANGES_FAILURE_MAX_MS)
  }
  return Math.min(Math.round(previous * 1.5), PROJECT_CHANGES_IDLE_MAX_MS)
}

// A push can wake an idle poll sooner, but must not move an already pending
// poll later. Otherwise a steady stream of pushes can starve cursor-only facts.
export function createProjectChangePollTimer(clock, poll) {
  let timer = null
  let deadline = null
  return {
    schedule(wait) {
      const nextDeadline = clock.now() + wait
      if (timer !== null && deadline <= nextDeadline) return
      if (timer !== null) clock.clearTimeout(timer)
      deadline = nextDeadline
      timer = clock.setTimeout(() => {
        timer = null
        deadline = null
        poll()
      }, wait)
    },
    cancel() {
      if (timer !== null) clock.clearTimeout(timer)
      timer = null
      deadline = null
    },
  }
}

// Own the cursor request, timer, and event listeners as one lifecycle so a
// hidden tab or replaced project cannot leave a stale poll behind.
export function startProjectChangesPoll({
  projectId, readChanges, handleChanges, events, visibility, clock,
}) {
  let active = true
  let cursor = null
  let controller = null
  let delay = PROJECT_CHANGES_ACTIVE_MS
  let pushedDuringPoll = false
  let resumeWhenSettled = false
  const pollTimer = createProjectChangePollTimer(clock, () => { void poll() })
  const schedule = (wait = delay) => { if (active) pollTimer.schedule(wait) }
  const poll = async () => {
    if (!active || visibility.hidden || controller) return
    const requestController = new AbortController()
    controller = requestController
    pushedDuringPoll = false
    try {
      const establishingBaseline = cursor === null
      const payload = await readChanges(cursor, { signal: requestController.signal })
      if (!active || requestController.signal.aborted) return
      cursor = Number(payload.cursor || cursor || 0)
      // Reconcile once after the baseline arrives. This closes the gap where
      // a save lands between the first file read and the first cursor read.
      const changed = await handleChanges(
        payload.changes || [],
        !!payload.truncated || establishingBaseline,
      )
      delay = nextProjectChangesDelay(delay, changed || pushedDuringPoll ? 'changed' : 'unchanged')
    } catch (cause) {
      if (cause?.name !== 'AbortError') delay = nextProjectChangesDelay(delay, 'failed')
    } finally {
      controller = null
      if (!visibility.hidden) schedule(resumeWhenSettled ? 0 : delay)
      resumeWhenSettled = false
    }
  }
  const onLiveChange = event => {
    const detail = event?.detail
    if (String(detail?.projectId ?? '') !== String(projectId)) return
    void handleChanges(detail?.change ? [detail.change] : [], false)
    // Pushes may end with an unpublished agent-run completion. Resume the
    // active cadence, but never postpone an already pending cursor deadline.
    delay = nextProjectChangesDelay(delay, 'changed')
    if (controller) pushedDuringPoll = true
    if (!visibility.hidden && !controller) schedule()
  }
  const onVisibility = () => {
    if (visibility.hidden) {
      pollTimer.cancel()
      controller?.abort()
    } else {
      if (controller) resumeWhenSettled = true
      else schedule(0)
    }
  }
  events.addEventListener('mobius:project-change', onLiveChange)
  visibility.addEventListener('visibilitychange', onVisibility)
  void poll()
  return () => {
    active = false
    controller?.abort()
    pollTimer.cancel()
    events.removeEventListener('mobius:project-change', onLiveChange)
    visibility.removeEventListener('visibilitychange', onVisibility)
  }
}
