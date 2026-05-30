import { useEffect, useRef, useState } from 'react'
import { useQueryClient } from '@tanstack/react-query'
import { apiFetch } from '../../api/client.js'
import { appQueries } from '../../hooks/queries.js'
import './InstallSheet.css'

// Home-screen names are short; the OS truncates long ones anyway and
// `short_name` is the first 12 chars. Cap generously but keep it sane.
const MAX_NAME = 64

// Center-square-crop + downscale to a PNG before upload. The server
// (PUT /apps/{id}/icon) re-normalizes anyway, but shrinking here keeps
// us well under the 12 MB wire cap and makes the upload quick on mobile.
async function fileToSquarePng(file, size = 512) {
  const bmp = await createImageBitmap(file)
  try {
    const side = Math.min(bmp.width, bmp.height)
    const sx = (bmp.width - side) / 2
    const sy = (bmp.height - side) / 2
    const canvas = document.createElement('canvas')
    canvas.width = canvas.height = size
    const ctx = canvas.getContext('2d')
    ctx.drawImage(bmp, sx, sy, side, side, 0, 0, size, size)
    return await new Promise((resolve, reject) =>
      canvas.toBlob(b => (b ? resolve(b) : reject(new Error('encode failed'))), 'image/png'),
    )
  } finally {
    bmp.close?.()
  }
}

/**
 * InstallSheet — set the home-screen name + icon for a mini-app, in an
 * in-PWA modal, BEFORE entering the install surface. Saving first means
 * the manifest already carries the right name when we navigate to
 * `/apps/<slug>/?install=1`, so the OS install dialog shows it with no
 * reload. The standalone install page keeps its own icon picker for
 * direct (non-shell) visitors.
 */
export default function InstallSheet({ appId, appName, appSlug, appUpdatedAt, onClose }) {
  const queryClient = useQueryClient()
  const fileRef = useRef(null)
  const [draftName, setDraftName] = useState(appName || '')
  const [iconBlob, setIconBlob] = useState(null)
  const [iconPreview, setIconPreview] = useState(null) // object URL or null
  const [submitting, setSubmitting] = useState(false)
  const [error, setError] = useState('')

  // Revoke the object URL when it changes or on unmount — leaks are
  // small but the pattern should be clean.
  useEffect(() => {
    return () => {
      if (iconPreview) URL.revokeObjectURL(iconPreview)
    }
  }, [iconPreview])

  // Escape closes (unless mid-submit, where navigation is imminent).
  useEffect(() => {
    function onKey(e) {
      if (e.key === 'Escape' && !submitting) onClose?.()
    }
    document.addEventListener('keydown', onKey)
    return () => document.removeEventListener('keydown', onKey)
  }, [submitting, onClose])

  async function onPickFile(e) {
    const file = e.target.files?.[0]
    e.target.value = '' // allow re-picking the same file
    if (!file) return
    try {
      const png = await fileToSquarePng(file)
      setIconBlob(png)
      // The effect cleanup (keyed on iconPreview) revokes the previous
      // URL — don't also revoke here, to avoid a revoke racing the next
      // commit's <img src>.
      setIconPreview(URL.createObjectURL(png))
    } catch {
      setError("That image couldn't be read — try a PNG or JPEG.")
    }
  }

  async function onContinue() {
    const name = draftName.trim()
    if (!name || submitting) return
    setSubmitting(true)
    setError('')
    try {
      if (name !== appName) {
        const res = await apiFetch(`/apps/${appId}`, {
          method: 'PATCH',
          body: JSON.stringify({ name }),
        })
        if (!res.ok) throw new Error('Could not save the name.')
      }
      if (iconBlob) {
        const res = await apiFetch(`/apps/${appId}/icon`, {
          method: 'PUT',
          headers: { 'Content-Type': 'image/png' },
          body: iconBlob,
        })
        if (!res.ok) throw new Error('Could not save the icon.')
      }
      // Reflect the new name/icon in the drawer when the user returns.
      appQueries.list.invalidate(queryClient)
      // Same-tab navigation to the install surface. Manifest is already
      // fresh (saved above + no-cache), so the OS shows the new name.
      window.location.href = `/apps/${appSlug}/?install=1`
    } catch (err) {
      setError(err?.message || 'Something went wrong. Try again.')
      setSubmitting(false)
    }
  }

  const label = draftName.trim().slice(0, 12) || appName?.slice(0, 12) || appSlug

  return (
    <div
      className="is__overlay"
      onClick={() => { if (!submitting) onClose?.() }}
    >
      <div
        className="is__card"
        role="dialog"
        aria-modal="true"
        aria-label="Add to home screen"
        onClick={e => e.stopPropagation()}
      >
        <h2 className="is__title">Add to home screen</h2>

        <div className="is__row">
          <button
            type="button"
            className="is__icon-wrap"
            aria-label="Change icon"
            onClick={() => fileRef.current?.click()}
          >
            <img
              className="is__icon"
              alt=""
              src={
                iconPreview ||
                `/apps/${appSlug}/icon-192.png?v=${encodeURIComponent(appUpdatedAt || '')}`
              }
            />
            <span className="is__icon-edit" aria-hidden="true">✎</span>
          </button>

          <div className="is__fields">
            <label className="is__field-label" htmlFor="is-name">Name</label>
            <input
              id="is-name"
              className="is__name-input"
              type="text"
              value={draftName}
              maxLength={MAX_NAME}
              autoComplete="off"
              spellCheck={false}
              onChange={e => setDraftName(e.target.value)}
              onKeyDown={e => { if (e.key === 'Enter') onContinue() }}
              placeholder="App name"
            />
            <div className="is__preview">
              Home-screen label: <strong>{label}</strong>
            </div>
          </div>
        </div>

        <p className="is__hint">
          Tap the icon to upload a custom image. This name is used when you
          add the app to your home screen.
        </p>

        {error && <div className="is__error" role="alert">{error}</div>}

        <div className="is__actions">
          <button
            type="button"
            className="is__btn is__btn--secondary"
            onClick={() => onClose?.()}
            disabled={submitting}
          >
            Cancel
          </button>
          <button
            type="button"
            className="is__btn is__btn--primary"
            onClick={onContinue}
            disabled={submitting || !draftName.trim()}
          >
            {submitting ? 'Saving…' : 'Continue'}
          </button>
        </div>

        <input
          ref={fileRef}
          type="file"
          accept="image/png,image/jpeg,image/webp"
          hidden
          onChange={onPickFile}
        />
      </div>
    </div>
  )
}
