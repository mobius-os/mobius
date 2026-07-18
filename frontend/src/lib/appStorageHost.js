const RPC_METHODS = new Map([
  ['get', 'get'],
  ['getText', 'getText'],
  ['getBlob', 'getBlob'],
  ['durableWrite', 'durableWrite'],
  ['remove', 'remove'],
  ['list', 'list'],
  ['pendingCount', 'pendingCount'],
  ['getWithVersion', 'getWithVersion'],
  ['queueSignals', '_queueSignals'],
  ['pendingSignalCount', '_pendingSignalCount'],
  ['drainSignals', '_drainSignals'],
  ['drain', '_drain'],
])

const SUBSCRIBE_METHODS = {
  json: 'subscribe',
  text: 'subscribeText',
  blob: 'subscribeBlob',
}

const MAX_SUBSCRIPTIONS_PER_SOURCE = 256
const MAX_SUBSCRIPTION_ID_LENGTH = 160
const MAX_SUBSCRIPTION_PATH_LENGTH = 2048

function identityKey(identity) {
  if (!identity) return null
  return `${identity.appId}:${identity.appInstanceId || 'legacy'}`
}

function invalidCredentialError() {
  const error = new Error('App storage is waiting for its scoped credential.')
  error.code = 'storage_identity_unavailable'
  return error
}

/**
 * Owns one shell-realm storage runtime for an app installation. The runtime is
 * keyed by both numeric app id and token nonce: a rotated installation can
 * never inherit a ready or in-flight runtime from the previous identity.
 *
 * Frame subscriptions are desired state. They survive a legitimate token
 * refresh, reattach after an installation-identity transition, and are removed
 * synchronously when their exact contentWindow detaches.
 */
export function createAppStorageHost({
  appId,
  getCurrentToken,
  getToken,
  tokenIdentity,
  createStorage,
  send,
  onDeadLetter = null,
}) {
  const expectedAppId = String(appId)
  const subscriptions = new Map()
  let ready = null
  let creating = null
  let destroyed = false

  function currentIdentity() {
    const identity = tokenIdentity(getCurrentToken())
    if (!identity || String(identity.appId) !== expectedAppId) return null
    return {
      appId: expectedAppId,
      appInstanceId: identity.appInstanceId || null,
      key: identityKey(identity),
    }
  }

  function safeDestroy(storage) {
    try { storage?._destroy?.() } catch {}
  }

  function detachRecord(record) {
    const detach = record.detach
    record.detach = null
    record.entry = null
    try { detach?.() } catch {}
  }

  function retireReady() {
    const entry = ready
    if (!entry) return
    ready = null
    for (const sourceSubscriptions of subscriptions.values()) {
      for (const record of sourceSubscriptions.values()) {
        if (record.entry === entry) detachRecord(record)
      }
    }
    try { entry.detachDeadLetter?.() } catch {}
    safeDestroy(entry.storage)
  }

  function detachSource(source) {
    const sourceSubscriptions = subscriptions.get(source)
    if (!sourceSubscriptions) return
    subscriptions.delete(source)
    for (const record of sourceSubscriptions.values()) detachRecord(record)
  }

  function post(source, message) {
    try {
      const delivered = send(source, message)
      if (delivered === false) detachSource(source)
      return delivered !== false
    } catch {
      detachSource(source)
      return false
    }
  }

  function attachRecord(record, entry) {
    if (destroyed || ready !== entry || record.detach) return
    const current = subscriptions.get(record.source)?.get(record.id)
    if (current !== record) return
    const method = SUBSCRIBE_METHODS[record.kind]
    const detach = entry.storage?.[method]?.(record.path, (value) => {
      if (destroyed || ready !== entry) return
      if (currentIdentity()?.key !== entry.identity.key) return
      if (subscriptions.get(record.source)?.get(record.id) !== record) return
      post(record.source, {
        type: 'moebius:storage-change',
        subscriptionId: record.id,
        value,
      })
    })
    if (typeof detach !== 'function') {
      throw new Error(`mobius.storage: ${method} is unavailable in the shell host`)
    }
    record.detach = detach
    record.entry = entry
  }

  function attachAll(entry) {
    for (const sourceSubscriptions of subscriptions.values()) {
      for (const record of sourceSubscriptions.values()) attachRecord(record, entry)
    }
  }

  async function build(identity) {
    const scopedToken = async (options = {}) => {
      const token = await getToken(options)
      const received = tokenIdentity(token)
      return identityKey(received) === identity.key ? token : null
    }
    const storage = await createStorage({
      appId: expectedAppId,
      appInstanceId: identity.appInstanceId,
      getToken: scopedToken,
    })
    if (!storage || typeof storage !== 'object') {
      throw new Error('mobius.storage: shell host did not create a storage runtime')
    }
    const latest = currentIdentity()
    if (destroyed || latest?.key !== identity.key) {
      safeDestroy(storage)
      return null
    }
    retireReady()
    const entry = { identity, storage, detachDeadLetter: null }
    ready = entry
    if (typeof storage.onDeadLetter === 'function' && typeof onDeadLetter === 'function') {
      entry.detachDeadLetter = storage.onDeadLetter((payload) => {
        if (!destroyed && ready === entry
            && currentIdentity()?.key === entry.identity.key) onDeadLetter(payload)
      })
    }
    try {
      attachAll(entry)
    } catch (error) {
      if (ready === entry) retireReady()
      throw error
    }
    return entry
  }

  async function ensureStorage() {
    for (;;) {
      if (destroyed) throw new Error('mobius.storage: shell host is detached')
      const identity = currentIdentity()
      if (!identity) {
        retireReady()
        throw invalidCredentialError()
      }
      if (ready?.identity.key === identity.key) return ready.storage
      if (ready) retireReady()
      if (creating) {
        await creating.promise.catch(() => {})
        continue
      }
      const task = build(identity)
      const creation = { identity, promise: task }
      creating = creation
      try {
        const entry = await task
        if (entry) return entry.storage
      } finally {
        if (creating === creation) creating = null
      }
    }
  }

  async function subscribe(source, id, path, kind) {
    if (!source || (typeof source !== 'object' && typeof source !== 'function')) {
      throw new Error('mobius.storage: subscription source is invalid')
    }
    if (typeof id !== 'string' || !id || id.length > MAX_SUBSCRIPTION_ID_LENGTH) {
      throw new Error('mobius.storage: subscription id is invalid')
    }
    if (typeof path !== 'string' || !path || path.length > MAX_SUBSCRIPTION_PATH_LENGTH) {
      throw new Error('mobius.storage: subscription path is invalid')
    }
    if (!SUBSCRIBE_METHODS[kind]) {
      throw new Error('mobius.storage: subscription kind is invalid')
    }
    let sourceSubscriptions = subscriptions.get(source)
    if (!sourceSubscriptions) {
      sourceSubscriptions = new Map()
      subscriptions.set(source, sourceSubscriptions)
    }
    const prior = sourceSubscriptions.get(id)
    if (!prior && sourceSubscriptions.size >= MAX_SUBSCRIPTIONS_PER_SOURCE) {
      throw new Error('mobius.storage: too many subscriptions in one frame')
    }
    if (prior) detachRecord(prior)
    const record = { source, id, path, kind, detach: null, entry: null }
    sourceSubscriptions.set(id, record)
    try {
      const storage = await ensureStorage()
      const entry = ready
      if (entry?.storage === storage && sourceSubscriptions.get(id) === record) {
        attachRecord(record, entry)
      }
      return true
    } catch (error) {
      if (sourceSubscriptions.get(id) === record) {
        sourceSubscriptions.delete(id)
        if (!sourceSubscriptions.size) subscriptions.delete(source)
      }
      detachRecord(record)
      throw error
    }
  }

  function unsubscribe(source, id) {
    const sourceSubscriptions = subscriptions.get(source)
    const record = sourceSubscriptions?.get(id)
    if (!record) return false
    sourceSubscriptions.delete(id)
    if (!sourceSubscriptions.size) subscriptions.delete(source)
    detachRecord(record)
    return true
  }

  async function handleRpc(source, method, args = []) {
    if (method === 'subscribe') {
      return subscribe(source, args[0], args[1], args[2])
    }
    if (method === 'unsubscribe') return unsubscribe(source, args[0])
    const storageMethod = RPC_METHODS.get(method)
    if (!storageMethod) {
      const error = new Error('mobius.storage: shell method is not allowed')
      error.code = 'storage_method_denied'
      throw error
    }
    const storage = await ensureStorage()
    const fn = storage[storageMethod]
    if (typeof fn !== 'function') {
      throw new Error(`mobius.storage: ${method} is unavailable in the shell host`)
    }
    return fn(...(Array.isArray(args) ? args : []))
  }

  async function reconcile() {
    if (!currentIdentity()) {
      retireReady()
      return null
    }
    if (!ready && !creating && subscriptions.size === 0) return null
    return ensureStorage()
  }

  async function drain() {
    if (!ready && !creating) return null
    const storage = await ensureStorage()
    return storage._drain?.()
  }

  function destroy() {
    if (destroyed) return
    destroyed = true
    for (const source of [...subscriptions.keys()]) detachSource(source)
    retireReady()
  }

  return {
    handleRpc,
    reconcile,
    drain,
    detachSource,
    destroy,
  }
}

export const _appStorageHostLimits = {
  MAX_SUBSCRIPTIONS_PER_SOURCE,
  MAX_SUBSCRIPTION_ID_LENGTH,
  MAX_SUBSCRIPTION_PATH_LENGTH,
}
