import { apiFetch } from '../api/client.js'

export const DEVICE_ASSET_CACHE = 'device.asset-cache'

const CACHE_PREFIX = 'mobius-device-assets-v1'
const MIN_FREE_BYTES = 16 * 1024 * 1024
const KEY_PATTERN = /^[a-zA-Z0-9][a-zA-Z0-9._-]{0,79}$/
const SHA256_PATTERN = /^[a-f0-9]{64}$/

function capabilityError(code, message, name = 'CapabilityError') {
  const error = new Error(message)
  error.code = code
  error.name = name
  return error
}

function assertKey(value, label) {
  if (typeof value !== 'string' || !KEY_PATTERN.test(value)) {
    throw capabilityError(
      'invalid_request',
      `${label} must contain only letters, numbers, dots, dashes, and underscores.`,
      'TypeError',
    )
  }
  return value
}

function assertInteger(value, label, { min = 0, max = Number.MAX_SAFE_INTEGER } = {}) {
  if (!Number.isSafeInteger(value) || value < min || value > max) {
    throw capabilityError(
      'invalid_request',
      `${label} must be an integer between ${min} and ${max}.`,
      'TypeError',
    )
  }
  return value
}

function reviewedLimits(declaration) {
  const limits = declaration?.limits || {}
  return {
    maxBytes: assertInteger(limits.max_bytes, 'max_bytes', { min: 1 }),
    maxAssetBytes: assertInteger(limits.max_asset_bytes, 'max_asset_bytes', { min: 1 }),
    maxChunkBytes: assertInteger(limits.max_chunk_bytes, 'max_chunk_bytes', { min: 1 }),
  }
}

export function normalizeDeviceAssetPackage(input, declaration) {
  if (!input?.package || typeof input.package !== 'object'
    || Array.isArray(input.package)) {
    throw capabilityError('invalid_request', 'A device asset package is required.', 'TypeError')
  }
  const limits = reviewedLimits(declaration)
  const rawPackage = input.package
  const key = assertKey(rawPackage.key, 'Package key')
  if (!Array.isArray(rawPackage.assets) || rawPackage.assets.length < 1
    || rawPackage.assets.length > 64) {
    throw capabilityError(
      'invalid_request',
      'A package must contain between 1 and 64 assets.',
      'TypeError',
    )
  }

  const seen = new Set()
  let packageBytes = 0
  const assets = rawPackage.assets.map((asset, assetIndex) => {
    if (!asset || typeof asset !== 'object' || Array.isArray(asset)) {
      throw capabilityError('invalid_request', `Asset ${assetIndex + 1} is invalid.`, 'TypeError')
    }
    const id = assertKey(asset.id, `Asset ${assetIndex + 1} id`)
    if (seen.has(id)) {
      throw capabilityError('invalid_request', `Asset id \`${id}\` is duplicated.`, 'TypeError')
    }
    seen.add(id)
    let source
    try { source = new URL(asset.url) } catch {
      throw capabilityError('invalid_request', `Asset \`${id}\` has an invalid URL.`, 'TypeError')
    }
    if (source.protocol !== 'https:' || source.username || source.password) {
      throw capabilityError(
        'invalid_request',
        `Asset \`${id}\` must use a public HTTPS URL without credentials.`,
        'TypeError',
      )
    }
    const bytes = assertInteger(asset.bytes, `Asset \`${id}\` size`, {
      min: 1,
      max: limits.maxAssetBytes,
    })
    if (!Array.isArray(asset.chunks) || asset.chunks.length < 1
      || asset.chunks.length > 16_384) {
      throw capabilityError(
        'invalid_request',
        `Asset \`${id}\` must contain a bounded chunk manifest.`,
        'TypeError',
      )
    }
    let chunkBytes = 0
    const chunks = asset.chunks.map((chunk, chunkIndex) => {
      if (!chunk || typeof chunk !== 'object' || Array.isArray(chunk)) {
        throw capabilityError(
          'invalid_request',
          `Asset \`${id}\` chunk ${chunkIndex + 1} is invalid.`,
          'TypeError',
        )
      }
      const size = assertInteger(
        chunk.bytes,
        `Asset \`${id}\` chunk ${chunkIndex + 1} size`,
        { min: 1, max: limits.maxChunkBytes },
      )
      const sha256 = typeof chunk.sha256 === 'string'
        ? chunk.sha256.toLowerCase()
        : ''
      if (!SHA256_PATTERN.test(sha256)) {
        throw capabilityError(
          'invalid_request',
          `Asset \`${id}\` chunk ${chunkIndex + 1} needs a SHA-256 digest.`,
          'TypeError',
        )
      }
      const normalized = { index: chunkIndex, offset: chunkBytes, bytes: size, sha256 }
      chunkBytes += size
      return normalized
    })
    if (chunkBytes !== bytes) {
      throw capabilityError(
        'invalid_request',
        `Asset \`${id}\` chunks do not add up to its declared size.`,
        'TypeError',
      )
    }
    packageBytes += bytes
    return { id, url: source.href, bytes, chunks }
  })
  if (packageBytes > limits.maxBytes) {
    throw capabilityError(
      'invalid_request',
      'The device asset package exceeds its reviewed storage limit.',
      'TypeError',
    )
  }
  return { key, bytes: packageBytes, assets }
}

function utf8(value) {
  return new TextEncoder().encode(value)
}

async function sha256Hex(value, cryptoImpl = globalThis.crypto) {
  if (!cryptoImpl?.subtle) {
    throw capabilityError('unavailable', 'This browser cannot verify downloaded files.')
  }
  const digest = await cryptoImpl.subtle.digest('SHA-256', value)
  return [...new Uint8Array(digest)]
    .map((byte) => byte.toString(16).padStart(2, '0'))
    .join('')
}

async function packageFingerprint(pkg, cryptoImpl) {
  const canonical = JSON.stringify({
    key: pkg.key,
    assets: pkg.assets.map((asset) => ({
      id: asset.id,
      url: asset.url,
      bytes: asset.bytes,
      chunks: asset.chunks.map(({ bytes, sha256 }) => ({ bytes, sha256 })),
    })),
  })
  return sha256Hex(utf8(canonical), cryptoImpl)
}

function cacheName(partitionId) {
  return /^\d+$/.test(partitionId)
    ? `${CACHE_PREFIX}-app-${partitionId}`
    : `${CACHE_PREFIX}-${partitionId}`
}

function pathSegment(value) {
  return encodeURIComponent(value)
}

function packagePrefix(origin, partitionId, pkg) {
  return `${origin}/.mobius/device-assets/${pathSegment(partitionId)}/${pathSegment(pkg.key)}/`
}

function chunkUrl(origin, partitionId, pkg, fingerprint, asset, chunk) {
  return `${packagePrefix(origin, partitionId, pkg)}${fingerprint}/${pathSegment(asset.id)}/${chunk.index}`
}

function versionPrefix(origin, partitionId, pkg, fingerprint) {
  return `${packagePrefix(origin, partitionId, pkg)}${fingerprint}/`
}

function completeUrl(origin, partitionId, pkg, fingerprint) {
  return `${versionPrefix(origin, partitionId, pkg, fingerprint)}.complete`
}

async function chunkIsCached(cache, url, chunk) {
  const response = await cache.match(url)
  if (!response) return false
  const bytes = Number(response.headers.get('content-length'))
  const sha256 = response.headers.get('x-mobius-sha256')
  if (bytes === chunk.bytes && sha256 === chunk.sha256) return true
  await cache.delete(url)
  return false
}

async function packageState(cache, origin, partitionId, pkg, fingerprint) {
  let cachedBytes = 0
  let cachedChunks = 0
  let totalChunks = 0
  for (const asset of pkg.assets) {
    for (const chunk of asset.chunks) {
      totalChunks += 1
      if (await chunkIsCached(
        cache,
        chunkUrl(origin, partitionId, pkg, fingerprint, asset, chunk),
        chunk,
      )) {
        cachedBytes += chunk.bytes
        cachedChunks += 1
      }
    }
  }
  return {
    state: cachedChunks === totalChunks ? 'ready' : (cachedChunks ? 'partial' : 'missing'),
    cachedBytes,
    totalBytes: pkg.bytes,
    cachedChunks,
    totalChunks,
  }
}

async function persistence(storageManager, request) {
  if (!storageManager?.persisted) return 'best-effort'
  try {
    if (await storageManager.persisted()) return 'persistent'
    if (request && storageManager.persist && await storageManager.persist()) {
      return 'persistent'
    }
  } catch {}
  return 'best-effort'
}

async function assertSpace(storageManager, missingBytes) {
  if (!storageManager?.estimate || missingBytes <= 0) return
  let estimate
  try { estimate = await storageManager.estimate() } catch { return }
  const quota = Number(estimate?.quota)
  const usage = Number(estimate?.usage)
  if (Number.isFinite(quota) && Number.isFinite(usage)
    && quota - usage < missingBytes + Math.min(MIN_FREE_BYTES, missingBytes)) {
    throw capabilityError(
      'quota_exceeded',
      'This device does not have enough browser storage for the download.',
      'QuotaExceededError',
    )
  }
}

function entryVersionPrefix(url, origin, partitionId) {
  const root = `${origin}/.mobius/device-assets/${pathSegment(partitionId)}/`
  if (!url.startsWith(root)) return null
  const path = url.slice(root.length).split('/')
  return path.length >= 3 ? `${root}${path[0]}/${path[1]}/` : null
}

async function pruneStalePartials(cache, origin, partitionId, pkg, fingerprint) {
  const current = versionPrefix(origin, partitionId, pkg, fingerprint)
  const keys = await cache.keys()
  const urls = keys.map((request) => (
    typeof request === 'string' ? request : request.url
  ))
  const complete = new Set(urls
    .filter((url) => url.endsWith('/.complete'))
    .map((url) => url.slice(0, -'.complete'.length)))
  await Promise.all(keys.map((request, index) => {
    const prefix = entryVersionPrefix(urls[index], origin, partitionId)
    return prefix && prefix !== current && !complete.has(prefix)
      ? cache.delete(request)
      : false
  }))
}

async function cacheByteUsage(cache) {
  let bytes = 0
  for (const request of await cache.keys()) {
    const response = await cache.match(request)
    const size = Number(response?.headers?.get('content-length'))
    if (Number.isSafeInteger(size) && size > 0) bytes += size
  }
  return bytes
}

async function assertPartitionBudget(cache, maxBytes, missingBytes) {
  if (await cacheByteUsage(cache) + missingBytes > maxBytes) {
    throw capabilityError(
      'quota_exceeded',
      'This app\'s reviewed device-storage allowance is not large enough for the download.',
      'QuotaExceededError',
    )
  }
}

async function pruneOldPackageVersions(cache, origin, partitionId, pkg, fingerprint) {
  const keepPrefix = versionPrefix(origin, partitionId, pkg, fingerprint)
  const prefix = packagePrefix(origin, partitionId, pkg)
  const keys = await cache.keys()
  await Promise.all(keys.map((request) => {
    const url = typeof request === 'string' ? request : request.url
    return url.startsWith(prefix) && !url.startsWith(keepPrefix)
      ? cache.delete(request)
      : false
  }))
}

async function removePackage(cache, origin, partitionId, pkg) {
  const prefix = packagePrefix(origin, partitionId, pkg)
  const keys = await cache.keys()
  const matches = keys.filter((request) => {
    const url = typeof request === 'string' ? request : request.url
    return url.startsWith(prefix)
  })
  await Promise.all(matches.map((request) => cache.delete(request)))
  return matches.length
}

function progressValue(state, persistenceMode) {
  return { ...state, persistence: persistenceMode }
}

export function createDeviceAssetCacheProvider({
  appId,
  partitionId,
  cacheStorage = globalThis.caches,
  storageManager = globalThis.navigator?.storage,
  cryptoImpl = globalThis.crypto,
  origin = globalThis.location?.origin,
  fetchRange = async ({ appId: targetAppId, url, offset, length, signal }) => {
    const query = new URLSearchParams({
      url,
      offset: String(offset),
      length: String(length),
    })
    return apiFetch(`/apps/${targetAppId}/device-assets/relay?${query}`, {
      method: 'GET',
      signal,
      headers: { Accept: 'application/octet-stream' },
    })
  },
} = {}) {
  // Navigation state may carry the numeric app id as a canonical decimal
  // string. Normalize that host-owned identity once at this boundary so
  // browser-backed storage does not become unavailable for route-derived ids.
  const ownerAppId = Number(appId)
  const ownerPartitionId = partitionId === undefined
    ? String(ownerAppId)
    : assertKey(String(partitionId), 'Storage partition')
  return {
    version: 1,
    exclusive: true,
    onDeactivate: 'cancel',
    open({ input, declaration, channel }) {
      const controller = new AbortController()
      let settled = false
      let releaseRead = null

      const waitForReadConsumer = () => new Promise((resolve, reject) => {
        releaseRead = { resolve, reject }
      }).finally(() => { releaseRead = null })

      Promise.resolve().then(async () => {
        if (!origin || !cacheStorage?.open) {
          throw capabilityError(
            'unavailable',
            'Device asset storage is unavailable in this browser.',
          )
        }
        const operation = input.operation
        if (!['status', 'install', 'read', 'remove'].includes(operation)) {
          throw capabilityError('invalid_request', 'Unknown device asset operation.', 'TypeError')
        }
        const pkg = normalizeDeviceAssetPackage(input, declaration)
        if (operation === 'install'
          && (!Number.isSafeInteger(ownerAppId) || ownerAppId < 1)) {
          throw capabilityError(
            'unavailable',
            'This speech model can only be installed from its managing app.',
          )
        }
        const fingerprint = await packageFingerprint(pkg, cryptoImpl)
        const name = cacheName(ownerPartitionId)
        const existingNames = cacheStorage.keys ? await cacheStorage.keys() : null
        const hasPartition = existingNames === null || existingNames.includes(name)
        if (operation !== 'install' && !hasPartition) {
          const empty = {
            state: 'missing',
            cachedBytes: 0,
            totalBytes: pkg.bytes,
            cachedChunks: 0,
            totalChunks: pkg.assets.reduce(
              (total, asset) => total + asset.chunks.length,
              0,
            ),
          }
          const value = progressValue(empty, 'best-effort')
          channel.ready(value)
          if (operation === 'read') {
            throw capabilityError(
              'not_installed',
              'This device needs to download the requested files first.',
              'NotFoundError',
            )
          }
          return value
        }
        const cache = await cacheStorage.open(name)
        const persistenceMode = await persistence(storageManager, operation === 'install')
        let state = await packageState(cache, origin, ownerPartitionId, pkg, fingerprint)
        channel.ready(progressValue(state, persistenceMode))

        if (operation === 'status') return progressValue(state, persistenceMode)
        if (operation === 'remove') {
          await removePackage(cache, origin, ownerPartitionId, pkg)
          state = await packageState(cache, origin, ownerPartitionId, pkg, fingerprint)
          return progressValue(state, persistenceMode)
        }
        if (operation === 'read') {
          if (state.state !== 'ready') {
            throw capabilityError(
              'not_installed',
              'This device needs to download the requested files first.',
              'NotFoundError',
            )
          }
          for (const asset of pkg.assets) {
            for (const chunk of asset.chunks) {
              if (controller.signal.aborted) throw controller.signal.reason
              const url = chunkUrl(origin, ownerPartitionId, pkg, fingerprint, asset, chunk)
              const response = await cache.match(url)
              if (!response) {
                throw capabilityError(
                  'not_installed',
                  'A cached file is incomplete; download it again on this device.',
                  'NotFoundError',
                )
              }
              const buffer = await response.arrayBuffer()
              // Do not enqueue an entire large package into the app/worker
              // message ports at once. The consumer acknowledges each
              // transferred chunk after taking ownership, keeping the shell's
              // peak memory bounded to one reviewed chunk.
              const consumed = waitForReadConsumer()
              channel.event('chunk', {
                assetId: asset.id,
                index: chunk.index,
                offset: chunk.offset,
                totalBytes: asset.bytes,
                bytes: buffer,
              }, [buffer])
              await consumed
            }
          }
          return progressValue(state, persistenceMode)
        }

        const missingBytes = pkg.bytes - state.cachedBytes
        await pruneStalePartials(cache, origin, ownerPartitionId, pkg, fingerprint)
        await assertPartitionBudget(cache, reviewedLimits(declaration).maxBytes, missingBytes)
        await assertSpace(storageManager, missingBytes)
        let downloadedBytes = state.cachedBytes
        for (const asset of pkg.assets) {
          for (const chunk of asset.chunks) {
            if (controller.signal.aborted) throw controller.signal.reason
            const url = chunkUrl(origin, ownerPartitionId, pkg, fingerprint, asset, chunk)
            if (await chunkIsCached(cache, url, chunk)) continue
            const response = await fetchRange({
              appId: ownerAppId,
              url: asset.url,
              offset: chunk.offset,
              length: chunk.bytes,
              signal: controller.signal,
            })
            if (!response?.ok) {
              throw capabilityError(
                'download_failed',
                `The device asset source returned ${response?.status || 'an error'}.`,
              )
            }
            const upstreamTotal = response.headers.get('X-Mobius-Asset-Total')
            if (upstreamTotal !== null && upstreamTotal !== String(asset.bytes)) {
              throw capabilityError(
                'download_failed',
                'The device asset source did not match the reviewed asset size.',
              )
            }
            const buffer = await response.arrayBuffer()
            if (buffer.byteLength !== chunk.bytes) {
              throw capabilityError('download_failed', 'A downloaded chunk had the wrong size.')
            }
            if (await sha256Hex(buffer, cryptoImpl) !== chunk.sha256) {
              throw capabilityError(
                'integrity_failed',
                'A downloaded chunk failed its integrity check and was not stored.',
              )
            }
            await cache.put(url, new Response(buffer, {
              headers: {
                'Content-Length': String(chunk.bytes),
                'Content-Type': 'application/octet-stream',
                'X-Mobius-SHA256': chunk.sha256,
              },
            }))
            downloadedBytes += chunk.bytes
            channel.event('progress', {
              downloadedBytes,
              totalBytes: pkg.bytes,
              assetId: asset.id,
            })
          }
        }
        state = await packageState(cache, origin, ownerPartitionId, pkg, fingerprint)
        if (state.state !== 'ready') {
          throw capabilityError('integrity_failed', 'The device asset package is incomplete.')
        }
        await cache.put(completeUrl(origin, ownerPartitionId, pkg, fingerprint), new Response('', {
          headers: {
            'Content-Length': '0',
            'X-Mobius-Package-Complete': '1',
          },
        }))
        await pruneOldPackageVersions(cache, origin, ownerPartitionId, pkg, fingerprint)
        return progressValue(state, persistenceMode)
      }).then((result) => {
        settled = true
        channel.result(result)
      }).catch((error) => {
        settled = true
        channel.error(error)
      })

      return {
        control(action) {
          if (action === 'cancel' && !settled && !controller.signal.aborted) {
            const error = capabilityError(
              'aborted',
              'Device asset operation cancelled.',
              'AbortError',
            )
            controller.abort(error)
            releaseRead?.reject(error)
          } else if (action === 'next') {
            releaseRead?.resolve()
          }
        },
      }
    },
  }
}

export async function purgeDeviceAssetCache(appId, cacheStorage = globalThis.caches) {
  if (!Number.isSafeInteger(Number(appId)) || Number(appId) < 1 || !cacheStorage?.delete) {
    return false
  }
  return cacheStorage.delete(cacheName(String(Number(appId))))
}
