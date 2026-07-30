import { useEffect, useRef, useState, useSyncExternalStore } from 'react'
import { apiFetch } from '../../api/client.js'
import useDialogFocus from '../../hooks/useDialogFocus.js'
import {
  getInstallPromptSnapshot,
  requestInstall,
  subscribeInstallPrompt,
} from '../../lib/installPrompt.js'
import {
  detectInstallPlatform,
  installCopyForPlatform,
} from '../../utils/installPlatform.js'
import {
  initiallyOpenStandaloneInstallCard,
  standaloneInstallCompleted,
} from '../../lib/standaloneBoot.js'

async function fileToSquarePng(file, size = 512) {
  const bmp = await createImageBitmap(file)
  try {
    const side = Math.min(bmp.width, bmp.height)
    const canvas = document.createElement('canvas')
    canvas.width = canvas.height = size
    canvas.getContext('2d').drawImage(
      bmp,
      (bmp.width - side) / 2,
      (bmp.height - side) / 2,
      side,
      side,
      0,
      0,
      size,
      size,
    )
    return await new Promise((resolve, reject) => canvas.toBlob(
      blob => blob ? resolve(blob) : reject(new Error('encode failed')),
      'image/png',
    ))
  } finally {
    bmp.close?.()
  }
}

function wasDismissed(slug) {
  try { return sessionStorage.getItem(`mobius:install-dismissed:${slug}`) === '1' }
  catch { return false }
}

function rememberDismissed(slug) {
  try { sessionStorage.setItem(`mobius:install-dismissed:${slug}`, '1') }
  catch { /* session storage is optional */ }
}

export default function StandaloneInstallCard({ app, forceOpen, onClose, onIconUpdated }) {
  const installState = useSyncExternalStore(
    subscribeInstallPrompt,
    getInstallPromptSnapshot,
    getInstallPromptSnapshot,
  )
  const platform = detectInstallPlatform()
  const copy = installCopyForPlatform(platform, installState === 'installed', app.name)
  const [open, setOpen] = useState(() => initiallyOpenStandaloneInstallCard({
    installState,
    forceOpen,
    dismissed: wasDismissed(app.slug),
  }))
  const [showInstructions, setShowInstructions] = useState(
    () => installState === 'manual' && forceOpen,
  )
  const [uploading, setUploading] = useState(false)
  const [message, setMessage] = useState('')
  const [iconVersion, setIconVersion] = useState(app.updated_at || '0')
  const dialogRef = useRef(null)
  const primaryRef = useRef(null)
  const fileRef = useRef(null)
  const previousInstallStateRef = useRef(installState)

  useEffect(() => {
    if (forceOpen) setOpen(true)
  }, [forceOpen])

  useEffect(() => {
    const previous = previousInstallStateRef.current
    previousInstallStateRef.current = installState
    if (standaloneInstallCompleted(previous, installState)) setOpen(true)
  }, [installState])

  useDialogFocus({
    containerRef: dialogRef,
    initialFocusRef: primaryRef,
    onClose: () => close('dismiss'),
    open,
  })

  function close(reason) {
    if (reason !== 'installed') rememberDismissed(app.slug)
    setOpen(false)
    onClose?.()
  }

  async function chooseIcon(event) {
    const file = event.target.files?.[0]
    event.target.value = ''
    if (!file) return
    setUploading(true)
    setMessage('')
    try {
      const png = await fileToSquarePng(file)
      const response = await apiFetch(`/apps/${app.id}/icon`, {
        method: 'PUT',
        headers: { 'Content-Type': 'image/png' },
        body: png,
      })
      if (!response.ok) throw new Error(`icon upload ${response.status}`)
      const version = String(Date.now())
      setIconVersion(version)
      setMessage('Icon updated')
      onIconUpdated?.(version)
    } catch {
      setMessage("That image couldn't be saved. Try a PNG or JPEG.")
    } finally {
      setUploading(false)
    }
  }

  async function install() {
    if (installState === 'installed') {
      close('installed')
      return
    }
    if (installState !== 'ready') {
      if (showInstructions) {
        close('instructions-read')
        return
      }
      setShowInstructions(true)
      return
    }
    const result = await requestInstall()
    if (result.outcome !== 'accepted') setShowInstructions(true)
  }

  if (!open) return null

  return (
    <div className="standalone-install__backdrop" onClick={() => close('backdrop')}>
      <section
        ref={dialogRef}
        className="standalone-install"
        role="dialog"
        aria-modal="true"
        aria-labelledby="standalone-install-title"
        onClick={event => event.stopPropagation()}
      >
        {installState === 'installed' ? (
          <>
            <div className="standalone-install__success" aria-hidden="true">✓</div>
            <h1 id="standalone-install-title">{app.name} is on your home screen</h1>
            <button
              ref={primaryRef}
              className="standalone-install__primary"
              type="button"
              onClick={() => close('installed')}
            >
              Got it
            </button>
          </>
        ) : (
          <>
            <div className="standalone-install__identity">
              <button
                className="standalone-install__icon-button"
                type="button"
                aria-label="Change app icon"
                disabled={uploading}
                onClick={() => fileRef.current?.click()}
              >
                <img
                  src={`/apps/${encodeURIComponent(app.slug)}/icon-192.png?v=${encodeURIComponent(iconVersion)}`}
                  alt=""
                />
                <span aria-hidden="true">✎</span>
              </button>
              <div>
                <h1 id="standalone-install-title">Install {app.name}</h1>
                <p>Keep it one tap away, without opening the Möbius workspace first.</p>
              </div>
            </div>
            <p className="standalone-install__hint">
              Tap the icon to customise it, or keep the current one.
            </p>
            {showInstructions && (
              <div className="standalone-install__instructions" role="status">
                <strong>{copy.summary}</strong>
                <span>{copy.body}</span>
              </div>
            )}
            {message && <div className="standalone-install__message" role="status">{message}</div>}
            <div className="standalone-install__actions">
              <button type="button" onClick={() => close('later')}>Maybe later</button>
              <button
                ref={primaryRef}
                className="standalone-install__primary"
                type="button"
                onClick={install}
              >
                {installState === 'ready' ? 'Install' : (showInstructions ? 'Got it' : copy.ctaLabel)}
              </button>
            </div>
            <input
              ref={fileRef}
              type="file"
              accept="image/png,image/jpeg,image/webp"
              hidden
              onChange={chooseIcon}
            />
          </>
        )}
      </section>
    </div>
  )
}
