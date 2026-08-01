/* Behavioral coverage for transient dismissible surfaces (the chat image
 * viewer) driven through the real `useNavigation` hook.
 *
 * The surface owns a `kind:'dismissible'` session-history sentinel so the OS
 * back gesture closes it. The failure this locks down is engine-specific: on a
 * wedged WebKit Navigation store (iOS 18.4+) `updateCurrentEntry` throws
 * InvalidStateError, every mirror read comes back unusable, and a traversal
 * therefore arrives looking like an untagged iframe phantom. A close that
 * waited for that traversal left the viewer mounted with a dead close button.
 *
 * The engine below is a session-history double whose traversals settle only
 * when the test says so, which is what makes "the surface closed BEFORE the
 * engine answered" an observable property rather than a source-text claim.
 * It runs in the lib suite, whose loader opts `useNavigation` into the React
 * hook shim the same way `useAppIntentNavigation` and `useFileUpload` already
 * do — the established route for driving a hook with `renderHook`.
 */
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { renderHook } from '../../components/ChatView/hooks/__tests__/react-hook-shim.mjs'

const WEDGED = 'The object is in an invalid state.'

function memoryStorage() {
  const values = new Map()
  return {
    getItem: key => (values.has(key) ? values.get(key) : null),
    setItem: (key, value) => values.set(key, String(value)),
    removeItem: key => values.delete(key),
  }
}

/**
 * A session-history double.
 *
 * `wedged` makes every Navigation-store read and write throw, exactly as the
 * reported WebKit engine does. `navigationApi: false` models the older iOS
 * Safari that only has `popstate`. `back()` merely QUEUES a traversal — tests
 * call `settle()` to commit it — so the window between an explicit close and
 * the engine's answer is observable, and a traversal that never arrives can be
 * modeled by simply not settling.
 */
function sessionHistory({ wedged = false, navigationApi = true } = {}) {
  const entries = []
  let index = -1
  let queued = 0
  const navigateHandlers = new Set()
  const windowHandlers = new Map()

  const win = {
    location: { pathname: '/shell/', search: '', href: 'http://localhost/shell/' },
    innerWidth: 390,
    innerHeight: 844,
    addEventListener(type, handler) {
      if (!windowHandlers.has(type)) windowHandlers.set(type, new Set())
      windowHandlers.get(type).add(handler)
    },
    removeEventListener(type, handler) {
      windowHandlers.get(type)?.delete(handler)
    },
  }

  const history = {
    get state() { return index >= 0 ? entries[index].state : null },
    pushState(state) {
      entries.length = index + 1
      entries.push({ state, mirror: undefined })
      index = entries.length - 1
    },
    replaceState(state) {
      if (index < 0) {
        entries.push({ state, mirror: undefined })
        index = 0
      } else {
        entries[index] = { state, mirror: undefined }
      }
    },
    back() { queued += 1 },
  }

  const mirrorAt = position => ({
    getState() {
      if (wedged) throw new Error(WEDGED)
      return entries[position]?.mirror
    },
    get index() {
      if (wedged) throw new Error(WEDGED)
      return position
    },
  })

  const navigation = navigationApi ? {
    addEventListener(type, handler) {
      if (type === 'navigate') navigateHandlers.add(handler)
    },
    removeEventListener(type, handler) { navigateHandlers.delete(handler) },
    updateCurrentEntry({ state }) {
      if (wedged) throw new Error(WEDGED)
      entries[index].mirror = state
    },
    get currentEntry() {
      if (wedged) throw new Error(WEDGED)
      return mirrorAt(index)
    },
  } : null

  function commitOneTraversal() {
    if (index <= 0) return
    const target = index - 1
    if (navigationApi) {
      // `navigate` fires while the cursor is still on the source entry;
      // intercept() commits the traversal and then runs the handler, so a
      // handler reads the destination from the classic store. An
      // un-intercepted traversal commits on its own.
      let committed = false
      const commit = () => {
        if (committed) return
        committed = true
        index = target
      }
      const event = {
        navigationType: 'traverse',
        canIntercept: true,
        destination: mirrorAt(target),
        intercept({ handler }) {
          commit()
          handler()
        },
      }
      for (const handler of [...navigateHandlers]) handler(event)
      commit()
    } else {
      index = target
      for (const handler of [...(windowHandlers.get('popstate') || [])]) {
        handler({ state: history.state })
      }
    }
  }

  return {
    win,
    history,
    navigation,
    /** Commit every queued traversal — the engine finally answering. */
    settle() {
      while (queued > 0) {
        queued -= 1
        commitOneTraversal()
      }
    },
    /** A real Back gesture: the engine traverses on its own. */
    userBack() { commitOneTraversal() },
    /** An untagged entry a sandboxed app/preview iframe pushed. */
    pushIframeEntry() {
      entries.length = index + 1
      entries.push({ state: { iframe: true }, mirror: undefined })
      index = entries.length - 1
    },
    get pendingTraversals() { return queued },
    get currentKind() { return history.state?.kind ?? null },
    get currentState() { return history.state },
    get depth() { return index },
  }
}

async function mountNavigation(engine) {
  globalThis.window = engine.win
  globalThis.location = engine.win.location
  globalThis.localStorage = memoryStorage()
  globalThis.sessionStorage = memoryStorage()
  globalThis.history = engine.history
  if (engine.navigation) globalThis.navigation = engine.navigation
  else delete globalThis.navigation

  const [{ default: useNavigation }, paneModel] = await Promise.all([
    import('../useNavigation.js'),
    import('../../components/Shell/paneModel.js'),
  ])

  const ws = paneModel.seedFromFlatTabs([{ kind: 'chat', id: 'c1' }])
  const workspaceStateRef = { current: { ws, undo: null } }
  // The wedged engine reports every failed mirror write through the shared
  // client-error logger; keep that console noise out of the test output.
  const consoleError = console.error
  console.error = () => {}
  try {
    return renderHook(useNavigation, {
      workspace: ws,
      workspaceStateRef,
      dispatchWorkspace: () => {},
      visiblePaneIds: new Set(Object.keys(ws.panes)),
      blobValid: true,
      replaceImplicitBootTab: false,
      dragActiveRef: { current: false },
    })
  } finally {
    console.error = consoleError
  }
}

test('an explicit close dismisses before the engine answers, and stays closed when it does', async () => {
  const engine = sessionHistory({ wedged: true })
  const { result } = await mountNavigation(engine)
  const dismissals = []

  const entryId = result.current.openHistoryDismiss(() => dismissals.push('first'))
  assert.equal(engine.currentKind, 'dismissible', 'opening installs the back-stack sentinel')

  result.current.closeHistoryDismiss(entryId)

  // The engine has NOT answered yet: on the reported iOS build that answer can
  // arrive unreadable, or never arrive at all. The viewer must already be shut.
  assert.deepEqual(dismissals, ['first'], 'the close affordance dismisses immediately')
  assert.equal(engine.pendingTraversals, 1, 'and asks the engine to consume its sentinel')

  engine.settle()
  assert.deepEqual(dismissals, ['first'], 'the late traversal does not re-dismiss')
  assert.equal(engine.currentKind, 'base', 'the sentinel is consumed, not stranded')
})

test('closing stays available after a traversal the engine never delivers', async () => {
  const engine = sessionHistory({ wedged: true })
  const { result } = await mountNavigation(engine)
  const dismissals = []

  const first = result.current.openHistoryDismiss(() => dismissals.push('first'))
  result.current.closeHistoryDismiss(first)
  // The engine drops this traversal outright — never settled.

  const second = result.current.openHistoryDismiss(() => dismissals.push('second'))
  result.current.closeHistoryDismiss(second)

  assert.deepEqual(dismissals, ['first', 'second'],
    'a lost traversal must not leave the close affordance permanently dead')
})

test('late close bookkeeping cannot dismiss the next surface', async () => {
  const engine = sessionHistory({ wedged: true })
  const { result } = await mountNavigation(engine)
  const dismissals = []

  const first = result.current.openHistoryDismiss(() => dismissals.push('first'))
  result.current.closeHistoryDismiss(first)

  // Open another viewer before the first close's asynchronous traversal lands.
  // The engine will now physically traverse off this newer sentinel, but the
  // request is still correlated with `first` and must not close `second`.
  const second = result.current.openHistoryDismiss(() => dismissals.push('second'))
  engine.settle()

  assert.deepEqual(dismissals, ['first'])
  assert.equal(engine.currentKind, 'dismissible',
    'the still-open surface gets one live sentinel re-armed at the cursor')

  result.current.closeHistoryDismiss(second)
  engine.settle()
  assert.deepEqual(dismissals, ['first', 'second'])
  assert.equal(engine.currentKind, 'base', 'its own close consumes that sentinel once')
})

test('a back gesture dismisses the surface even when it lands on an untagged entry', async () => {
  // iOS Safari without the Navigation API, with a sandboxed iframe entry
  // sitting beneath the sentinel: the landing reads untagged, and the phantom
  // guard must not mistake our own sentinel for that iframe's entry.
  const engine = sessionHistory({ navigationApi: false })
  const { result } = await mountNavigation(engine)
  const dismissals = []

  engine.pushIframeEntry()
  result.current.openHistoryDismiss(() => dismissals.push('gesture'))
  assert.equal(engine.currentKind, 'dismissible')

  engine.userBack()

  assert.deepEqual(dismissals, ['gesture'], 'Back closes the surface')
  assert.equal(engine.currentKind, null, 'and lands on the untagged entry beneath it')

  const next = result.current.openHistoryDismiss(() => dismissals.push('next'))
  assert.equal(engine.currentState.index, 1,
    'the next sentinel derives from the tagged cursor beneath the phantom')
  result.current.closeHistoryDismiss(next)
})

test('a back gesture on a healthy engine dismisses exactly once', async () => {
  const engine = sessionHistory()
  const { result } = await mountNavigation(engine)
  const dismissals = []

  result.current.openHistoryDismiss(() => dismissals.push('gesture'))
  engine.userBack()

  assert.deepEqual(dismissals, ['gesture'])
  assert.equal(engine.currentKind, 'base')
})
