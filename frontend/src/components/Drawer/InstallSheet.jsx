import { useEffect, useRef, useState } from 'react'
import { useQueryClient } from '@tanstack/react-query'
import { api, apiFetch } from '../../api/client.js'
import { appIconUrl } from '../appIcon.js'
import { appQueries } from '../../hooks/queries.js'
import useDialogFocus from '../../hooks/useDialogFocus.js'
import { loginBoundaryPath } from '../../lib/safeReturnPath.js'
import {
  androidBrowserIntentHref,
  detectInstallPlatform,
  isStandaloneDisplay,
} from '../../utils/installPlatform.js'
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
export default function InstallSheet({ app, onClose }) {
  const { id: appId, name: appName, slug: appSlug } = app
  const queryClient = useQueryClient()
  const fileRef = useRef(null)
  const cardRef = useRef(null)
  const primaryFocusRef = useRef(null)
  const [draftName, setDraftName] = useState(appName || '')
  const [iconBlob, setIconBlob] = useState(null)
  const [iconPreview, setIconPreview] = useState(null) // object URL or null
  const [submitting, setSubmitting] = useState(false)
  const [error, setError] = useState('')
  // Only Safari's Share menu can add to the iOS Home Screen, so the final
  // step is never ours to automate. What IS ours: which document is on
  // screen when the user opens that menu. THIS document is the shell, whose
  // manifest is Möbius's — adding from here installs a second Möbius, not
  // the app. So every platform, iOS Safari included, navigates to the app's
  // own page, which serves the app's manifest, name, and icon.
  //
  // The one case that cannot navigate usefully is the installed Möbius app.
  // iOS: it has no Share button, and iOS gives a PWA no supported way to hand
  // off to Safari (`x-safari-https:` is undocumented and errors on some
  // versions). Android: navigating out of the installed app's scope opens an
  // in-app Custom Tab, where Chromium never fires `beforeinstallprompt`, so
  // the install card would sit there looking broken. Both flip `handoff`: the
  // card becomes a tappable link (an intent: URI on Android, which escapes to
  // the real browser) plus a copyable address as the fallback.
  const [platform] = useState(() => detectInstallPlatform())
  const [standalone] = useState(() => isStandaloneDisplay())
  const [handoff, setHandoff] = useState(false)
  const [handoffUrl, setHandoffUrl] = useState('')
  const [copied, setCopied] = useState(false)

  // Revoke the object URL when it changes or on unmount — leaks are
  // small but the pattern should be clean.
  useEffect(() => {
    return () => {
      if (iconPreview) URL.revokeObjectURL(iconPreview)
    }
  }, [iconPreview])

  useDialogFocus({
    containerRef: cardRef,
    initialFocusRef: primaryFocusRef,
    onClose: () => { if (!submitting) onClose?.() },
  })

  // The hand-off path replaces the form after saving. Move focus into its new
  // primary action instead of leaving focus on an unmounted Continue.
  useEffect(() => {
    if (handoff) queueMicrotask(() => primaryFocusRef.current?.focus())
  }, [handoff])

  // onContinue navigates the whole document away and intentionally leaves
  // `submitting` true (the page is leaving). BFCache can restore this page
  // mid-submit, stranding the button on "Saving…"; clear it when the page is
  // hidden (entering BFCache) or restored so the spinner never freezes. (The
  // Drawer also unmounts the sheet on pageshow; this is defense-in-depth.)
  useEffect(() => {
    function reset() { setSubmitting(false) }
    window.addEventListener('pagehide', reset)
    window.addEventListener('pageshow', reset)
    return () => {
      window.removeEventListener('pagehide', reset)
      window.removeEventListener('pageshow', reset)
    }
  }, [])

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

  // The app's own page — the only document whose manifest names and icons
  // THIS app. `?install=1` opens its Add-to-Home card on arrival, and the
  // opaque one-time pass rides through the manifest into the installed app's
  // first launch so it can sign itself in.
  //
  // Minting is best-effort on purpose: if it fails the install still works,
  // it just meets the ordinary login on first launch. A failed hand-off must
  // never block putting an app on the home screen.
  const installPath = `/apps/${appSlug}/?install=1`
  async function buildInstallUrl() {
    const url = new URL(installPath, window.location.origin)
    try {
      const res = await api.auth.installPass.mint(appSlug)
      if (res.ok) {
        const data = await res.json()
        if (data?.install_pass) {
          url.searchParams.set('pass', data.install_pass)
        }
      }
    } catch { /* install without the pass */ }
    return url.href
  }

  // Copying never places even the short-lived pass on the system clipboard.
  // The pass-free fallback enters through the login boundary so a signed-out
  // browser still returns to this app rather than falling through to the shell.
  const plainHandoffPath = loginBoundaryPath(installPath)
  const plainHandoffUrl = typeof window !== 'undefined'
    ? new URL(plainHandoffPath, window.location.origin).href
    : plainHandoffPath

  async function copyLink() {
    try {
      await navigator.clipboard.writeText(plainHandoffUrl)
      setCopied(true)
    } catch {
      setCopied(false)
      setError('Could not copy — the address is written below.')
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
      // Only iOS partitions each installed web app into fresh storage. Other
      // platforms already share the browser session, so minting a pass there
      // would add credential exposure without buying a sign-in handoff.
      const url = platform.ios
        ? await buildInstallUrl()
        : new URL(installPath, window.location.origin).href
      if (standalone && (platform.ios || platform.android)) {
        // Installed-app context: navigating in place can't complete an
        // install (no Share button on iOS; an install-less Custom Tab on
        // Android). Hand the destination over instead.
        setHandoffUrl(platform.ios ? url : androidBrowserIntentHref(url))
        setSubmitting(false)
        setHandoff(true)
        return
      }
      // Same-tab navigation to the install surface. Manifest is already
      // fresh (saved above + no-cache), so the OS shows the new name.
      window.location.href = url
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
        ref={cardRef}
        className="is__card"
        role="dialog"
        aria-modal="true"
        aria-label="Add to home screen"
        tabIndex={-1}
        onClick={e => e.stopPropagation()}
      >
        {handoff ? (
          <>
            {/* Nothing is left to confirm at this step — the work is done and
                the card is now just instructions. A corner dismissal reads as
                "I'm finished reading" without competing with the action. */}
            <button
              type="button"
              className="is__close"
              aria-label="Close"
              onClick={() => onClose?.()}
            >
              ×
            </button>
            <h2 className="is__title">Add {label} to your home screen</h2>
            {platform.ios ? (
              <p className="is__hint is__hint--steps">
                Only Safari can put an app on your home screen, and you’re in
                the installed Möbius app right now. Open {label}’s own page,
                then tap <strong>Share</strong> and choose{' '}
                <strong>Add to Home Screen</strong>.
              </p>
            ) : (
              <p className="is__hint is__hint--steps">
                You’re in the installed Möbius app, and Android only offers
                installs from a real browser tab. Open {label}’s page in your
                browser, then tap <strong>Install</strong>.
              </p>
            )}

            <div className="is__handoff">
              <a
                ref={primaryFocusRef}
                className="is__btn is__btn--primary is__btn--link"
                href={handoffUrl}
                {...(platform.ios
                  ? { target: '_blank', rel: 'noopener noreferrer' }
                  : {})}
              >
                {platform.ios ? `Open ${label}’s page` : 'Open in your browser'}
              </a>
              <button
                type="button"
                className="is__btn is__btn--secondary"
                onClick={copyLink}
              >
                {copied ? 'Link copied ✓' : 'Copy link'}
              </button>
            </div>

            {error && <div className="is__error" role="alert">{error}</div>}

            {platform.ios ? (
              <p className="is__hint">
                If it opens inside Möbius rather than Safari, tap the compass
                icon to switch over. Or open Safari yourself and go to{' '}
                <span className="is__url">{plainHandoffUrl}</span>
              </p>
            ) : (
              <p className="is__hint">
                If nothing opens, copy the link and paste it into your
                browser: <span className="is__url">{plainHandoffUrl}</span>
              </p>
            )}
          </>
        ) : (
        <>
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
                appIconUrl(app, null) ||
                `/apps/${appSlug}/icon-192.png?v=${encodeURIComponent(app.updated_at || '')}`
              }
              onError={e => {
                // Fall back to the flattened manifest icon if the raw icon
                // route returns 404 (app uses the auto-generated letter icon).
                const fallback = `/apps/${appSlug}/icon-192.png?v=${encodeURIComponent(app.updated_at || '')}`
                if (e.target.src !== fallback) e.target.src = fallback
              }}
            />
            <span className="is__icon-edit" aria-hidden="true">✎</span>
          </button>

          <div className="is__fields">
            <label className="is__field-label" htmlFor="is-name">Name</label>
            <input
              ref={primaryFocusRef}
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
        </>
        )}
      </div>
    </div>
  )
}
