import { useEffect, useRef, useState } from 'react'
import { createPortal } from 'react-dom'
import { collectAndClearSecureFields } from '../ChatView/SecureInputCard.jsx'
import '../ChatView/SecureInputCard.css'
import './AppOwnerAction.css'

export default function AppOwnerAction({ action }) {
  const dialog = useRef(null), form = useRef(null)
  const [busy, setBusy] = useState(false)
  useEffect(() => {
    dialog.current.showModal()
    const node = form.current
    return () => { if (node) collectAndClearSecureFields(node, action.prompt.fields) }
  }, [action])
  const { prompt } = action
  async function submit(event) {
    event.preventDefault()
    if (busy) return
    const values = collectAndClearSecureFields(form.current, prompt.fields)
    setBusy(true)
    await action.submit(values)
  }
  return createPortal(<dialog ref={dialog} className="app-owner-action" onCancel={e => { e.preventDefault(); action.cancel() }} aria-labelledby="owner-action-title">
    <section className="secure-card">
      <p className="secure-card__foot">MÖBIUS · TRUSTED OWNER ACTION · {prompt.app_name}</p>
      <header className="secure-card__head"><span className="secure-card__lock" aria-hidden="true" /><h3 id="owner-action-title" className="secure-card__title">{prompt.title}</h3></header>
      <p className="secure-card__description">{prompt.description}</p>
      {Object.keys(prompt.context || {}).length > 0 && <details className="app-owner-action__context"><summary>Selected item</summary><dl>{Object.entries(prompt.context).map(([key, value]) => <div key={key}><dt>{key.replaceAll('_', ' ')}</dt><dd>{String(value)}</dd></div>)}</dl></details>}
      <form ref={form} className="secure-card__form" autoComplete="off" onSubmit={submit}>
        {prompt.fields.map(field => <label key={field.name} className="secure-card__field"><span>{field.label}</span><input type="text" autoComplete="off" required disabled={busy} data-secure-field={field.name} data-secure-masked={field.type !== 'text' ? 'true' : undefined}/></label>)}
        <p className="secure-card__foot">{prompt.fields.length ? 'One-time entry · your key bypasses app screens, chat and AI.' : 'Only this action is approved. No key entry is needed.'}</p>
        <div className="secure-card__actions"><button type="button" className="secure-card__cancel" onClick={action.cancel}>{busy ? 'Close' : 'Cancel'}</button><button className="secure-card__submit" disabled={busy}>{busy ? 'Working securely…' : prompt.fields.length ? 'Enter securely' : 'Confirm'}</button></div>
      </form>
    </section>
  </dialog>, document.body)
}
