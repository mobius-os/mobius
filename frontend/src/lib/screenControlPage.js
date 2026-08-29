/* Browser-owned live-screen capture and the closed semantic command executor. */
import { BASE, getAuthHeaders } from '../api/client.js'

const INTERACTIVE_SELECTOR = [
  'button', 'a[href]', 'input:not([type="hidden"])', 'textarea', 'select',
  '[role="button"]', '[role="link"]', '[role="checkbox"]', '[role="tab"]',
  '[contenteditable="true"]', '[tabindex]:not([tabindex="-1"])',
].join(',')
const SENSITIVE_AUTOCOMPLETE = new Set([
  'current-password', 'new-password', 'one-time-code',
  'cc-number', 'cc-csc', 'cc-exp', 'cc-exp-month', 'cc-exp-year',
])
const FRAME_TIMEOUT_MS = 2500
const CAPTURE_START_TIMEOUT_MS = 10_000

let rootRefs = new Map()
let nextFrameRequest = 0
let latestOwnerInputAt = 0

if (typeof document !== 'undefined') {
  for (const type of ['pointerdown', 'keydown', 'input', 'wheel', 'touchstart']) {
    document.addEventListener(type, event => {
      if (event.isTrusted) latestOwnerInputAt = Date.now()
    }, { capture: true, passive: true })
  }
}

function concise(value, max = 180) {
  return String(value || '').replace(/\s+/g, ' ').trim().slice(0, max)
}

function visibleElement(element) {
  if (!element?.getBoundingClientRect) return false
  const rect = element.getBoundingClientRect()
  if (rect.width < 1 || rect.height < 1) return false
  const style = element.ownerDocument?.defaultView?.getComputedStyle?.(element)
  if (style?.display === 'none' || style?.visibility === 'hidden' || style?.opacity === '0') {
    return false
  }
  const view = element.ownerDocument?.defaultView
  return rect.bottom > 0 && rect.right > 0
    && rect.top < (view?.innerHeight || 0) && rect.left < (view?.innerWidth || 0)
}

function elementRole(element) {
  const explicit = concise(element.getAttribute?.('role'), 40)
  if (explicit) return explicit
  const tag = element.tagName?.toLowerCase()
  if (tag === 'a') return 'link'
  if (tag === 'button') return 'button'
  if (tag === 'textarea') return 'textbox'
  if (tag === 'select') return 'combobox'
  if (tag === 'input') {
    const type = (element.type || 'text').toLowerCase()
    if (type === 'checkbox') return 'checkbox'
    if (type === 'radio') return 'radio'
    if (['button', 'submit', 'reset'].includes(type)) return 'button'
    return 'textbox'
  }
  return 'control'
}

function labelledByText(element) {
  const ids = concise(element.getAttribute?.('aria-labelledby'), 300)
  if (!ids) return ''
  return ids.split(/\s+/).map(id => (
    element.ownerDocument?.getElementById?.(id)?.textContent || ''
  )).join(' ')
}

function elementName(element) {
  const label = element.labels?.[0]?.textContent
  return concise(
    element.getAttribute?.('aria-label')
      || labelledByText(element)
      || label
      || element.getAttribute?.('alt')
      || element.getAttribute?.('title')
      || element.getAttribute?.('placeholder')
      || element.textContent,
  )
}

function sensitiveField(element) {
  if (element?.tagName?.toLowerCase() !== 'input') return false
  return isSensitiveScreenControlField(element.type, element.autocomplete)
}

export function isSensitiveScreenControlField(type, autocomplete) {
  if (String(type || '').toLowerCase() === 'password') return true
  return String(autocomplete || '').split(/\s+/).some(
    token => SENSITIVE_AUTOCOMPLETE.has(token.toLowerCase()),
  )
}

function snapshotLocalDocument(doc = document) {
  const refs = new Map()
  const elements = []
  const nodes = Array.from(doc.querySelectorAll(INTERACTIVE_SELECTOR))
  for (const element of nodes) {
    if (!visibleElement(element) || element.closest?.('[inert]')) continue
    const ref = `e${elements.length + 1}`
    const rect = element.getBoundingClientRect()
    const item = {
      ref,
      role: elementRole(element),
      name: elementName(element),
      disabled: !!element.disabled || element.getAttribute?.('aria-disabled') === 'true',
      bounds: {
        x: Math.round(rect.x), y: Math.round(rect.y),
        width: Math.round(rect.width), height: Math.round(rect.height),
      },
    }
    if ('value' in element) {
      item.value = sensitiveField(element) ? '[masked]' : concise(element.value)
    }
    refs.set(ref, element)
    elements.push(item)
  }
  rootRefs = refs
  return elements
}

function nearestActionTarget(element) {
  return element?.closest?.(INTERACTIVE_SELECTOR) || element
}

function nativeValueSetter(element, value) {
  const tag = element?.tagName?.toLowerCase()
  const prototype = tag === 'textarea'
    ? globalThis.HTMLTextAreaElement?.prototype
    : globalThis.HTMLInputElement?.prototype
  const setter = prototype && Object.getOwnPropertyDescriptor(prototype, 'value')?.set
  if (setter) setter.call(element, value)
  else element.value = value
}

function screenInputEvent(type, options) {
  try { return new globalThis.InputEvent(type, options) }
  catch { return new globalThis.Event(type, { bubbles: true }) }
}

function typeInto(element, text, replace = true) {
  if (!element) throw new Error('No field is focused.')
  if (element.closest?.('[inert]')) throw new Error('That field is not interactive.')
  if (sensitiveField(element)) {
    throw new Error('Sensitive fields stay under your control.')
  }
  const tag = element.tagName?.toLowerCase()
  if (tag === 'input' || tag === 'textarea') {
    if (element.disabled || element.readOnly) throw new Error('The field is not editable.')
    element.focus({ preventScroll: true })
    const next = replace ? text : `${element.value || ''}${text}`
    nativeValueSetter(element, next)
    element.dispatchEvent(screenInputEvent('input', {
      bubbles: true, inputType: replace ? 'insertReplacementText' : 'insertText',
      data: text,
    }))
    element.dispatchEvent(new Event('change', { bubbles: true }))
    return
  }
  if (element.isContentEditable) {
    element.focus({ preventScroll: true })
    if (replace) element.textContent = text
    else element.append(docTextNode(element.ownerDocument, text))
    element.dispatchEvent(screenInputEvent('input', { bubbles: true, data: text }))
    return
  }
  throw new Error('The focused element is not editable.')
}

function docTextNode(doc, text) {
  return doc.createTextNode(text)
}

function scrollOwnerAt(doc, x, y) {
  let element = doc.elementFromPoint(x, y)
  while (element && element !== doc.body) {
    const style = doc.defaultView?.getComputedStyle?.(element)
    const scrollable = /(auto|scroll)/.test(`${style?.overflowY} ${style?.overflowX}`)
      && (element.scrollHeight > element.clientHeight || element.scrollWidth > element.clientWidth)
    if (scrollable) return element
    element = element.parentElement
  }
  return doc.scrollingElement || doc.documentElement
}

function dispatchPress(doc, key) {
  const target = doc.activeElement || doc.body
  const Keyboard = doc.defaultView?.KeyboardEvent || globalThis.KeyboardEvent
  target.dispatchEvent(new Keyboard('keydown', { key, bubbles: true, cancelable: true }))
  if (key === 'Enter') {
    if (target?.tagName?.toLowerCase() === 'button') target.click()
    else target?.form?.requestSubmit?.()
  } else if (key === ' ' && nearestActionTarget(target)?.click) {
    nearestActionTarget(target).click()
  }
  target.dispatchEvent(new Keyboard('keyup', { key, bubbles: true, cancelable: true }))
}

function localCommand(command, doc = document) {
  if (command.action !== 'snapshot' && Date.now() - latestOwnerInputAt < 750) {
    throw new Error('Your input took priority; inspect the screen and try again.')
  }
  const pointTarget = command.x != null && command.y != null
    ? doc.elementFromPoint(command.x, command.y)
    : null
  if (command.action === 'click') {
    const target = command.ref ? rootRefs.get(command.ref) : nearestActionTarget(pointTarget)
    if (!target || !visibleElement(target)) throw new Error('That control is no longer available.')
    if (target.closest?.('[inert]')) throw new Error('That control is not interactive.')
    if (target.disabled || target.getAttribute?.('aria-disabled') === 'true') {
      throw new Error('That control is disabled.')
    }
    target.focus?.({ preventScroll: true })
    target.click?.()
    return { clicked: command.ref || { x: command.x, y: command.y } }
  }
  if (command.action === 'type') {
    const target = command.ref ? rootRefs.get(command.ref) : doc.activeElement
    typeInto(target, command.text, command.replace !== false)
    return { typed: command.text.length }
  }
  if (command.action === 'scroll') {
    const x = command.x ?? Math.round((doc.defaultView?.innerWidth || 0) / 2)
    const y = command.y ?? Math.round((doc.defaultView?.innerHeight || 0) / 2)
    const target = scrollOwnerAt(doc, x, y)
    target.scrollBy?.({ left: command.deltaX || 0, top: command.deltaY || 0, behavior: 'auto' })
    return { scrolled: true }
  }
  if (command.action === 'press') {
    dispatchPress(doc, command.key)
    return { pressed: command.key }
  }
  throw new Error(`Unsupported local action: ${command.action}`)
}

function frameForApp(appId) {
  return Array.from(document.querySelectorAll('iframe[data-app-id]')).find(
    frame => String(frame.dataset.appId) === String(appId) && visibleElement(frame),
  ) || null
}

function requestFrame(frame, command) {
  const requestId = `screen:${Date.now().toString(36)}:${++nextFrameRequest}`
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => {
      window.removeEventListener('message', onMessage)
      reject(new Error('The app frame did not answer.'))
    }, FRAME_TIMEOUT_MS)
    function onMessage(event) {
      if (event.source !== frame.contentWindow) return
      const message = event.data
      if (message?.type !== 'moebius:screen-control-result' || message.requestId !== requestId) return
      clearTimeout(timer)
      window.removeEventListener('message', onMessage)
      if (message.ok) resolve(message.result)
      else reject(new Error(message.error || 'The app frame rejected the command.'))
    }
    window.addEventListener('message', onMessage)
    // App frames are intentionally opaque-origin. The exact contentWindow is
    // selected here and again validates the concrete parent origin, so `*` is
    // the only delivery target that can reach the sandboxed frame safely.
    frame.contentWindow?.postMessage({
      type: 'moebius:screen-control-command', requestId, command,
    }, '*')
  })
}

export function parseScreenControlFrameRef(ref) {
  const match = /^app:([^:]+):(e\d+)$/.exec(String(ref || ''))
  return match ? { appId: match[1], ref: match[2] } : null
}

export function boundedScreenCaptureSize(sourceWidth, sourceHeight, maxEdge = 2560) {
  if (!Number.isFinite(sourceWidth) || !Number.isFinite(sourceHeight)
      || sourceWidth <= 0 || sourceHeight <= 0 || maxEdge <= 0) return null
  const scale = Math.min(1, maxEdge / Math.max(sourceWidth, sourceHeight))
  return {
    width: Math.max(1, Math.round(sourceWidth * scale)),
    height: Math.max(1, Math.round(sourceHeight * scale)),
  }
}

async function snapshotPage() {
  const elements = snapshotLocalDocument()
  const frames = Array.from(document.querySelectorAll('iframe[data-app-id]')).filter(visibleElement)
  const frameResults = await Promise.allSettled(frames.map(async frame => {
    const appId = String(frame.dataset.appId)
    const rect = frame.getBoundingClientRect()
    const result = await requestFrame(frame, { action: 'snapshot' })
    return (result?.elements || []).map(item => ({
      ...item,
      ref: `app:${appId}:${item.ref}`,
      bounds: item.bounds ? {
        x: Math.round(rect.x + item.bounds.x),
        y: Math.round(rect.y + item.bounds.y),
        width: item.bounds.width,
        height: item.bounds.height,
      } : undefined,
    }))
  }))
  for (const result of frameResults) {
    if (result.status === 'fulfilled') elements.push(...result.value)
  }
  return {
    title: document.title,
    route: `${location.pathname}${location.search}${location.hash}`,
    viewport: {
      width: window.innerWidth,
      height: window.innerHeight,
      pixelRatio: window.devicePixelRatio || 1,
    },
    elements,
  }
}

async function executePageCommand(command) {
  if (command.action === 'snapshot') return snapshotPage()
  const frameRef = parseScreenControlFrameRef(command.ref)
  if (frameRef) {
    const frame = frameForApp(frameRef.appId)
    if (!frame) throw new Error('That app frame is no longer visible.')
    return requestFrame(frame, { ...command, ref: frameRef.ref })
  }
  if (command.x != null && command.y != null) {
    const target = document.elementFromPoint(command.x, command.y)
    if (target?.tagName?.toLowerCase() === 'iframe' && target.dataset.appId) {
      const rect = target.getBoundingClientRect()
      return requestFrame(target, {
        ...command,
        x: command.x - rect.x,
        y: command.y - rect.y,
      })
    }
  }
  if ((command.action === 'type' || command.action === 'press')
      && document.activeElement?.tagName?.toLowerCase() === 'iframe'
      && document.activeElement.dataset.appId) {
    return requestFrame(document.activeElement, command)
  }
  return localCommand(command)
}

export async function requestCurrentTabCapture() {
  if (!navigator.mediaDevices?.getDisplayMedia) {
    const error = new Error('Live screen sharing is not supported by this browser.')
    error.name = 'NotSupportedError'
    throw error
  }
  let stream
  let video
  try {
    stream = await navigator.mediaDevices.getDisplayMedia({
      video: { frameRate: { ideal: 2, max: 5 } },
      audio: false,
      preferCurrentTab: true,
      selfBrowserSurface: 'include',
      surfaceSwitching: 'exclude',
      monitorTypeSurfaces: 'exclude',
    })
    const track = stream.getVideoTracks()[0]
    if (!track || track.readyState === 'ended') {
      throw new Error('The shared screen ended before it was ready.')
    }
    video = document.createElement('video')
    video.muted = true
    video.playsInline = true
    video.srcObject = stream
    await new Promise((resolve, reject) => {
      let settled = false
      const finish = (callback, value) => {
        if (settled) return
        settled = true
        clearTimeout(timer)
        track.removeEventListener('ended', onEnded)
        video.onloadedmetadata = null
        video.onerror = null
        callback(value)
      }
      const onEnded = () => finish(
        reject, new Error('The shared screen ended before it was ready.'),
      )
      const timer = setTimeout(
        () => finish(reject, new Error('The shared screen did not become ready in time.')),
        CAPTURE_START_TIMEOUT_MS,
      )
      track.addEventListener('ended', onEnded, { once: true })
      video.onloadedmetadata = () => finish(resolve)
      video.onerror = () => finish(
        reject, new Error('The shared screen could not be read.'),
      )
    })
    await video.play()
    if (track.readyState === 'ended') {
      throw new Error('The shared screen ended before it was ready.')
    }
    return { stream, video }
  } catch (error) {
    stream?.getTracks?.().forEach(track => track.stop())
    if (video) video.srcObject = null
    throw error
  }
}

function captureVideoFrame(video, track) {
  if (!track || track.readyState === 'ended') {
    throw new Error('The shared screen is no longer available.')
  }
  const sourceWidth = video.videoWidth
  const sourceHeight = video.videoHeight
  if (!sourceWidth || !sourceHeight) throw new Error('The shared screen has no video frame yet.')
  const size = boundedScreenCaptureSize(sourceWidth, sourceHeight)
  const { width, height } = size
  const canvas = document.createElement('canvas')
  canvas.width = width
  canvas.height = height
  canvas.getContext('2d', { alpha: false }).drawImage(video, 0, 0, width, height)
  return {
    dataUrl: canvas.toDataURL('image/jpeg', 0.88),
    mimeType: 'image/jpeg',
    width,
    height,
  }
}

export function screenControlCommandExpired(deadlineAt, now = Date.now()) {
  const deadline = Number(deadlineAt)
  return Number.isFinite(deadline) && deadline <= now
}

export function createScreenControlClient({
  sessionId,
  expiresAt,
  capture,
  onConnected,
  onEnded,
}) {
  const controller = new AbortController()
  let stopped = false
  let stopPromise = null
  let endedNotified = false
  let expiryTimer = null
  const track = capture.stream.getVideoTracks()[0]

  function notifyEnded(reason, error) {
    if (endedNotified) return
    endedNotified = true
    onEnded?.(reason, error)
  }

  async function postResponse(commandId, outcome) {
    const response = await fetch(
      `${BASE}/api/screen-control/sessions/${encodeURIComponent(sessionId)}/responses`,
      {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', ...getAuthHeaders() },
        body: JSON.stringify({ commandId, ...outcome }),
      },
    )
    if (!response.ok && response.status !== 409 && response.status !== 404) {
      throw new Error(`Screen-control response failed (${response.status}).`)
    }
  }

  async function handleCommand(event) {
    const { commandId, deadlineAt, type: _type, ...command } = event
    if (!commandId || stopped) return
    if (screenControlCommandExpired(deadlineAt)) {
      await postResponse(commandId, {
        ok: false,
        error: 'The screen-control command expired before execution.',
      }).catch(() => {})
      return
    }
    try {
      const result = command.action === 'screenshot'
        ? captureVideoFrame(capture.video, track)
        : await executePageCommand(command)
      if (stopped) return
      await postResponse(commandId, { ok: true, result })
    } catch (error) {
      await postResponse(commandId, {
        ok: false,
        error: String(error?.message || error).slice(0, 1000),
      }).catch(() => {})
    }
  }

  async function connect() {
    try {
      const response = await fetch(
        `${BASE}/api/screen-control/sessions/${encodeURIComponent(sessionId)}/events`,
        { headers: getAuthHeaders(), signal: controller.signal },
      )
      if (!response.ok || !response.body) throw new Error('The control channel could not connect.')
      const reader = response.body.getReader()
      const decoder = new TextDecoder()
      let buffer = ''
      while (!stopped) {
        const { value, done } = await reader.read()
        if (done) break
        buffer += decoder.decode(value, { stream: true })
        let boundary
        while ((boundary = buffer.indexOf('\n\n')) !== -1) {
          const block = buffer.slice(0, boundary)
          buffer = buffer.slice(boundary + 2)
          for (const line of block.split('\n')) {
            if (!line.startsWith('data: ')) continue
            const event = JSON.parse(line.slice(6))
            if (event.type === 'screen-control-open') {
              onConnected?.()
              continue
            }
            if (event.type === 'screen-control-stop') {
              await stop({ notifyServer: false })
              notifyEnded('stopped')
              return
            }
            if (event.type === 'screen-control-command') await handleCommand(event)
          }
        }
      }
      if (!stopped) throw new Error('The control channel disconnected.')
    } catch (error) {
      if (stopped || error?.name === 'AbortError') return
      await stop({ notifyServer: false })
      notifyEnded('disconnected', error)
    }
  }

  async function stop({ notifyServer = true } = {}) {
    if (stopPromise) return stopPromise
    stopPromise = (async () => {
      stopped = true
      controller.abort()
      clearTimeout(expiryTimer)
      capture.stream.getTracks().forEach(item => item.stop())
      capture.video.srcObject = null
      if (notifyServer) {
        void fetch(
          `${BASE}/api/screen-control/sessions/${encodeURIComponent(sessionId)}`,
          {
            method: 'DELETE',
            headers: getAuthHeaders(),
            keepalive: true,
          },
        ).catch(() => {})
      }
    })()
    return stopPromise
  }

  if (track) {
    const onTrackEnded = () => {
      if (stopped) return
      void stop().finally(() => notifyEnded('stopped'))
    }
    track.addEventListener('ended', onTrackEnded, { once: true })
    // The grant can end while the session request is still in flight, before
    // this listener exists. Reconcile that already-ended state immediately.
    if (track.readyState === 'ended') {
      Promise.resolve().then(onTrackEnded)
    }
  }
  const expiryDelay = Number(expiresAt) - Date.now()
  if (Number.isFinite(expiryDelay)) {
    expiryTimer = setTimeout(() => {
      if (stopped) return
      void stop().finally(() => notifyEnded('expired'))
    }, Math.max(0, expiryDelay))
  }
  void connect()
  return { stop }
}
