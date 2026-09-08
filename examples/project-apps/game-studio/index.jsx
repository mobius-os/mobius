/* An example Project app uses local template IDs, never its installation slug. */
import { useState } from 'react'
export default function App() {
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)
  async function create() {
    setBusy(true); setError('')
    try {
      const template = (await window.mobius.projects.templates()).find(row => row.id === 'game')
      if (!template) throw new Error('The game template is unavailable. Check the installation.')
      await window.mobius.projects.create({ templateId: template.key, name: 'Untitled game' })
    } catch (cause) { setError(cause.message) }
    finally { setBusy(false) }
  }
  return <main style={{ padding: 24, color: 'var(--text)', fontFamily: 'var(--font)' }}>
    <h1>Game Studio</h1><p>Edit a scene in Projects and build a playable game.</p>
    <button style={{ minHeight: 44, padding: '0 16px' }} disabled={busy} onClick={create}>{busy ? 'Creating…' : 'New game'}</button>
    {error && <p role="alert">{error}</p>}
  </main>
}
