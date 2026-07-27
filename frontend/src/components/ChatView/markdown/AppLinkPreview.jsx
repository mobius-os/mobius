import { useEffect, useRef, useState } from 'react'
import { apiFetch } from '../../../api/client.js'

function staticArtifactDocument(source) {
  const parser = new DOMParser()
  const document = parser.parseFromString(source, 'text/html')
  document.querySelectorAll(
    'script, iframe, frame, object, embed, portal, base, link[rel="stylesheet"], meta[http-equiv="refresh"]',
  ).forEach((node) => node.remove())
  const policy = document.createElement('meta')
  policy.httpEquiv = 'Content-Security-Policy'
  policy.content = "default-src 'none'; style-src 'unsafe-inline'; img-src data: blob:; font-src data:; media-src 'none'; frame-src 'none'; connect-src 'none'"
  document.head.prepend(policy)
  return `<!doctype html>${document.documentElement.outerHTML}`
}

function longitudeToTile(value, zoom) {
  return ((Number(value) + 180) / 360) * (2 ** zoom)
}

function latitudeToTile(value, zoom) {
  const latitude = Math.max(-85.0511, Math.min(85.0511, Number(value)))
  const radians = latitude * Math.PI / 180
  return (1 - Math.asinh(Math.tan(radians)) / Math.PI) / 2 * (2 ** zoom)
}

function useNearViewport() {
  const ref = useRef(null)
  const [near, setNear] = useState(false)
  useEffect(() => {
    const node = ref.current
    if (!node) return undefined
    if (typeof IntersectionObserver === 'undefined') {
      setNear(true)
      return undefined
    }
    const observer = new IntersectionObserver(([entry]) => {
      if (entry.isIntersecting) setNear(true)
    }, { rootMargin: '240px' })
    observer.observe(node)
    return () => observer.disconnect()
  }, [])
  return [ref, near]
}

async function responseJson(path, signal) {
  const response = await apiFetch(path, { signal, timeoutMs: 8000 })
  if (!response.ok) throw new Error(`Preview read failed: ${response.status}`)
  return response.json()
}

async function artifactPreview(appId, itemId, signal) {
  const safeId = encodeURIComponent(itemId)
  const record = await responseJson(`/storage/apps/${appId}/artifacts/${safeId}.json`, signal)
  const version = Number(record?.current_version) || 1
  const response = await apiFetch(
    `/storage/apps/${appId}/versions/${safeId}/v${version}.html`,
    { signal, timeoutMs: 8000 },
  )
  if (!response.ok) throw new Error(`Artifact preview failed: ${response.status}`)
  return { type: 'artifact', html: staticArtifactDocument(await response.text()) }
}

async function mapPreview(appId, itemId, signal) {
  const record = await responseJson(
    `/storage/apps/${appId}/maps/${encodeURIComponent(itemId)}.json`,
    signal,
  )
  const center = record?.center || record?.origin
  if (!center) throw new Error('Map preview has no center.')
  const zoom = Math.max(11, Math.min(15, Math.floor(Number(record.zoom) || 15) - 2))
  const centerX = longitudeToTile(center.lon, zoom)
  const centerY = latitudeToTile(center.lat, zoom)
  const baseX = Math.floor(centerX) - 1
  const baseY = Math.floor(centerY)
  const modulus = 2 ** zoom
  const blobs = await Promise.all([0, 1, 2].map(async (index) => {
    const x = ((baseX + index) % modulus + modulus) % modulus
    const source = `https://tile.openstreetmap.org/${zoom}/${x}/${baseY}.png`
    const response = await apiFetch(`/proxy?url=${encodeURIComponent(source)}`, {
      signal,
      timeoutMs: 8000,
    })
    if (!response.ok) throw new Error(`Map tile failed: ${response.status}`)
    return response.blob()
  }))
  const tiles = blobs.map((blob) => URL.createObjectURL(blob))
  const pins = (record.places || []).map((place) => ({
    id: place.id,
    left: (longitudeToTile(place.lon, zoom) - baseX) / 3 * 100,
    top: (latitudeToTile(place.lat, zoom) - baseY) * 100,
  })).filter((pin) => pin.left >= 0 && pin.left <= 100 && pin.top >= 0 && pin.top <= 100)
  return { type: 'map', tiles, pins }
}

export default function AppLinkPreview({ appId, card, fallbackIcon }) {
  const [hostRef, near] = useNearViewport()
  const [preview, setPreview] = useState(null)

  useEffect(() => {
    if (!near || !appId) return undefined
    const controller = new AbortController()
    let ownedUrls = []
    let disposed = false
    setPreview(null)
    const load = card.kindKey === 'artifact'
      ? artifactPreview(appId, card.itemId, controller.signal)
      : card.kindKey === 'map'
        ? mapPreview(appId, card.itemId, controller.signal)
        : Promise.reject(new Error('Unsupported preview kind.'))
    load.then((next) => {
      const nextUrls = next.tiles || []
      if (disposed) {
        nextUrls.forEach((url) => URL.revokeObjectURL(url))
        return
      }
      ownedUrls = nextUrls
      setPreview(next)
    }).catch(() => {
      if (!disposed) setPreview({ type: 'fallback' })
    })
    return () => {
      disposed = true
      controller.abort()
      ownedUrls.forEach((url) => URL.revokeObjectURL(url))
    }
  }, [appId, card.itemId, card.kindKey, near])

  return (
    <span ref={hostRef} className={`md-app-card__visual md-app-card__visual--${card.app}`} aria-hidden="true">
      {preview?.type === 'artifact' && (
        <iframe title="" sandbox="" srcDoc={preview.html} tabIndex="-1" />
      )}
      {preview?.type === 'map' && (
        <span className="md-app-card__map">
          <span className="md-app-card__map-tiles">
            {preview.tiles.map((source) => <img src={source} alt="" key={source} />)}
          </span>
          {preview.pins.map((pin) => (
            <i key={pin.id} style={{ left: `${pin.left}%`, top: `${pin.top}%` }} />
          ))}
          <small>© OpenStreetMap</small>
        </span>
      )}
      {(!preview || preview.type === 'fallback') && (
        <span className="md-app-card__preview-fallback">
          <img src={fallbackIcon} alt="" loading="lazy" />
        </span>
      )}
    </span>
  )
}
