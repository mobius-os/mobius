/* The access review every app goes through before it installs: a small confirmation inside the guide's
   window. It names what the app can do, and Confirm and install installs exactly what was reviewed. */
import { useLayoutEffect, useRef } from 'react'
import AppIcon from '../AppIcon.jsx'
import useDialogFocus from '../../hooks/useDialogFocus.js'

export default function WalkthroughAccessConfirm({ confirmation, app, icon, onApprove, onCancel }) {
  const dialogRef = useRef(null)
  const confirmRef = useRef(null)
  const installButtonRef = useRef(null)
  const { rows, notice } = confirmation
  // Focus starts on Confirm, Tab and the page behind are held, Escape cancels, and focus goes back to
  // the Install button when it closes. The button is looked up rather than remembered, because some
  // browsers do not focus a button when it is tapped.
  useLayoutEffect(() => {
    installButtonRef.current = [...document.querySelectorAll('.wt__card button')].find(button => button.getAttribute('aria-label') === `Install ${app.name}`) || null
  }, [app.name])
  useDialogFocus({ containerRef: dialogRef, initialFocusRef: confirmRef, restoreFocusRef: installButtonRef, onClose: onCancel })
  return <div className="wt-confirm" ref={dialogRef} role="alertdialog" aria-modal="true" aria-labelledby="wt-confirm-title">
    <div className="wt-confirm__panel">
      <header>
        <AppIcon className="wt-confirm__icon" item={{ slug: app.id, icon_url: icon }} label={app.name} size={null} />
        <h3 id="wt-confirm-title">{app.name} asks for access</h3>
      </header>
      {notice && <p className="wt-note" role="alert">{notice}</p>}
      <section aria-labelledby="wt-confirm-section">
        <p className="wt-confirm__label" id="wt-confirm-section">Privacy &amp; access</p>
        {rows.length === 0
          ? <p className="wt-confirm__empty">No special permissions requested.</p>
          : <ul className="wt-confirm__list" tabIndex={0} aria-labelledby="wt-confirm-section">
            {rows.map(row => <li className="wt-confirm__row" key={row.label}>
              <div><strong>{row.label}</strong><span>{row.summary}</span></div>
              <em className={row.tone === 'read' || row.tone === 'muted' ? 'is-read' : ''}>{row.tag}</em>
            </li>)}
          </ul>}
      </section>
      <div className="wt-confirm__actions">
        <button type="button" className="wt-btn" onClick={onCancel}>Cancel</button>
        <button type="button" ref={confirmRef} className="wt-btn wt-btn--primary" onClick={onApprove}>Confirm and install</button>
      </div>
    </div>
  </div>
}
